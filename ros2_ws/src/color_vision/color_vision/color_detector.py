"""ROS2 node: OV5647 camera -> red/yellow/green detection.

Publishes
  traffic_light/state   std_msgs/String   "red" | "yellow" | "green" | "none" (debounced)
  color_detections      std_msgs/String   JSON list of {"color", "x", "y", "w", "h", "score"}
                                          in full-frame pixels, strongest first
  color_vision/image/compressed  sensor_msgs/CompressedImage  annotated JPEG (publish_image:=true)

Parameters
  mode           "objects" (painted blocks / signs, default) or "lamp" (lit traffic light lamps)
  width, height  sensor mode (640x480 default)
  detect_scale   detection runs on a downscaled frame (0.5 -> 320x240)
  stable_frames  frames a state must persist before traffic_light/state changes
  min_score      weaker candidates are ignored
  publish_image  publish the annotated frame; image_rate limits it (Hz)
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

        self.camera = OV5647Camera(width, height)
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        self.get_logger().info(f"color_detector started: mode={self.mode}, {width}x{height}, "
                               f"detect at x{self.scale}, publish_image={self.publish_image}")

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
                ok, jpg = cv2.imencode(".jpg", traffic_light.annotate(small, found), [cv2.IMWRITE_JPEG_QUALITY, 70])
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
        self.thread.join(timeout=2)
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
