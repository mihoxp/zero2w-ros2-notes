"""ROS2 node: OV5647 camera -> red/yellow/green detection and AprilTag command markers.

Publishes
  traffic_light/state   std_msgs/String   "red" | "yellow" | "green" | "none" (debounced)
  color_detections      std_msgs/String   JSON list of {"color", "x", "y", "w", "h", "score"}
                                          in full-frame pixels, strongest first
  tag_command           std_msgs/String   LEFT | RIGHT | SLOW | FAST, once per sighting of a tag
                                          (again after it has been out of view for tag_repeat_after s)
  tag_detections        std_msgs/String   JSON list of {"id", "command", "cx", "cy", "size"} (pixels)
  color_vision/image/compressed  sensor_msgs/CompressedImage  annotated JPEG (publish_image:=true)

AprilTag 36h11 detection costs ~60 ms per 640x480 frame on a Zero 2 W, so it runs in its own
thread on the newest frame (at most tag_rate Hz); OpenCV releases the GIL, so colour detection
keeps its frame rate.

Parameters
  mode           "objects" (painted blocks / signs, default) or "lamp" (lit traffic light lamps)
  width, height  sensor mode (640x480 default)
  detect_scale   detection runs on a downscaled frame (0.5 -> 320x240)
  stable_frames  frames a state must persist before traffic_light/state changes
  min_score      weaker candidates are ignored
  publish_image  publish the annotated frame; image_rate limits it (Hz)
  enable_tags    detect AprilTag command markers (default true)
  tag_family     OpenCV aruco dictionary name (default DICT_APRILTAG_36h11)
  tag_commands   "id:COMMAND" entries; other IDs are reported in tag_detections only
  tag_rate       maximum tag detections per second (limits CPU use)
  tag_repeat_after  seconds a tag must be gone before its command is published again
"""
import json
import threading
import time

import cv2
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

from . import traffic_light
from .ov5647_camera import OV5647Camera


class ColorDetector(Node):
    def __init__(self):
        super().__init__("color_detector")
        # Colour and tag detection already run in two threads; OpenCV's own worker threads only
        # add overhead on the Zero 2 W (tag detection: 153 ms CPU with 4 threads vs 104 ms with 1)
        cv2.setNumThreads(1)
        p = self.declare_parameter
        self.mode = p("mode", "objects").value
        width, height = p("width", 640).value, p("height", 480).value
        self.scale = p("detect_scale", 0.5).value
        self.min_score = p("min_score", traffic_light.MIN_SCORE).value
        self.publish_image = p("publish_image", False).value
        self.image_period = 1.0 / max(p("image_rate", 2.0).value, 0.1)
        self.stable = traffic_light.StableState(p("stable_frames", 3).value)
        if self.mode not in ("objects", "lamp"):
            raise ValueError("mode must be 'objects' or 'lamp'")
        self.detect = traffic_light.detect_objects if self.mode == "objects" else traffic_light.detect

        self.state_pub = self.create_publisher(String, "traffic_light/state", 10)
        self.det_pub = self.create_publisher(String, "color_detections", 10)
        self.img_pub = self.create_publisher(CompressedImage, "color_vision/image/compressed", 1)
        self.tag_cmd_pub = self.create_publisher(String, "tag_command", 10)
        self.tag_det_pub = self.create_publisher(String, "tag_detections", 10)

        self.enable_tags = p("enable_tags", True).value
        family = p("tag_family", "DICT_APRILTAG_36h11").value
        self.tag_commands = {int(k): v.strip().upper() for k, v in (
            e.split(":", 1) for e in p("tag_commands", ["1:RIGHT", "2:SLOW", "3:FAST", "4:LEFT"]).value)}
        self.tag_period = 1.0 / max(p("tag_rate", 5.0).value, 0.5)
        self.tag_repeat_after = p("tag_repeat_after", 2.0).value
        self.tag_detector = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, family)), cv2.aruco.DetectorParameters())
        self.latest_gray = None
        self.frame_ready = threading.Condition()
        self.tag_boxes = []  # last detected tag corners and ids, for the annotated image

        self.camera = OV5647Camera(width, height)
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        if self.enable_tags:
            self.tag_thread = threading.Thread(target=self.tag_loop, daemon=True)
            self.tag_thread.start()
        self.get_logger().info(f"color_detector started: mode={self.mode}, {width}x{height}, "
                               f"detect at x{self.scale}, publish_image={self.publish_image}, "
                               f"tags={family} {self.tag_commands if self.enable_tags else 'off'}")

    def tag_loop(self):
        last_seen = {}  # tag id -> last time it was detected
        try:
            while self.running and rclpy.ok():
                t0 = time.monotonic()
                with self.frame_ready:
                    self.frame_ready.wait(timeout=1.0)
                    gray, self.latest_gray = self.latest_gray, None
                if gray is None:
                    continue
                corners, ids, _ = self.tag_detector.detectMarkers(gray)
                ids = [] if ids is None else ids.flatten().tolist()
                self.tag_boxes = list(zip(corners, ids))
                now = time.monotonic()
                report = []
                for c, tag_id in zip(corners, ids):
                    pts = c.reshape(4, 2)
                    command = self.tag_commands.get(tag_id)
                    report.append({"id": tag_id, "command": command,
                                   "cx": int(pts[:, 0].mean()), "cy": int(pts[:, 1].mean()),
                                   "size": int(cv2.norm(pts[0] - pts[2]) / 1.414)})
                    if command and now - last_seen.get(tag_id, -1e9) > self.tag_repeat_after:
                        self.get_logger().info(f"tag_command -> {command} (tag {tag_id})")
                        self.tag_cmd_pub.publish(String(data=command))
                    last_seen[tag_id] = now
                self.tag_det_pub.publish(String(data=json.dumps(report)))
                time.sleep(max(0.0, self.tag_period - (time.monotonic() - t0)))
        except Exception:
            if self.running and rclpy.ok():
                raise

    def loop(self):
        try:
            self._loop()
        except Exception:
            if self.running and rclpy.ok():
                raise
            # Shutting down: publishers become invalid while a frame is in flight

    def _loop(self):
        last_state, last_image, last_log = None, 0.0, time.monotonic()
        frames = 0
        for _ in range(20):  # let auto exposure settle before reporting
            self.camera.read()
        while self.running and rclpy.ok():
            frame = self.camera.read()
            if self.enable_tags:  # tag thread always works on the newest full-resolution frame
                with self.frame_ready:
                    self.latest_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    self.frame_ready.notify()
            small = cv2.resize(frame, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)
            found = [d for d in self.detect(small, min_area=12) if d[2] >= self.min_score]
            state = self.stable.update(traffic_light.state(found, self.min_score))
            frames += 1

            self.det_pub.publish(String(data=json.dumps([
                {"color": c, "x": int(x / self.scale), "y": int(y / self.scale),
                 "w": int(w / self.scale), "h": int(h / self.scale), "score": round(s, 1)}
                for c, (x, y, w, h), s in found])))
            if state != last_state:
                self.get_logger().info(f"traffic_light/state -> {state}")
                last_state = state
            self.state_pub.publish(String(data=state))

            now = time.monotonic()
            if self.publish_image and now - last_image >= self.image_period:
                annotated = traffic_light.annotate(small, found)
                for c, tag_id in self.tag_boxes:
                    pts = (c.reshape(4, 2) * self.scale).astype(int)
                    cv2.polylines(annotated, [pts], True, (255, 0, 255), 2)
                    label = self.tag_commands.get(tag_id, f"id {tag_id}")
                    cv2.putText(annotated, label, tuple(pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 2)
                ok, jpg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    msg = CompressedImage(format="jpeg", data=jpg.tobytes())
                    msg.header.stamp = self.get_clock().now().to_msg()
                    msg.header.frame_id = "camera"
                    self.img_pub.publish(msg)
                last_image = now
            if now - last_log >= 30:
                self.get_logger().info(f"{frames / (now - last_log):.1f} FPS")
                frames, last_log = 0, now

    def shutdown(self):
        self.running = False
        with self.frame_ready:
            self.frame_ready.notify()
        self.thread.join(timeout=2)
        if self.enable_tags:
            self.tag_thread.join(timeout=2)
        self.camera.release()


def main(args=None):
    rclpy.init(args=args)
    node = ColorDetector()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
