"""V7RC app (car SRV or tank SRT mode) -> /cmd_vel and camera pan-tilt bridge.

The V7RC app sends UDP packets like "SRV1500150015001500#" (car) or "SRT...#" (tank) to
port 6188: four 4-digit channels in 1000..2000 with 1500 as center. Measured on the app:
  car mode:  CH1 right stick L/R, CH2 left stick U/D; CH3/CH4 always 1500
  tank mode: CH1 left stick L/R, CH2 left stick U/D, CH3 right stick L/R, CH4 right stick U/D
So in both modes CH1 steers (-> angular.z, sign set by invert_angular) and CH2 is throttle
(-> linear.x, sign set by invert_linear). CH3/CH4 drive the camera pan-tilt (pan_tilt/cmd)
in rate mode: stick deflection = turning speed, the camera holds its angle when released.
If no valid packet arrives for `timeout` seconds the robot is stopped (the camera just holds).
"""
import math
import socket
import threading
import time

import rclpy
from geometry_msgs.msg import Twist, Vector3
from rclpy.node import Node


def stick(value, deadband, span=500.0):
    """Map 1500±span to -1..1 with a deadband around center."""
    x = max(-1.0, min(1.0, (value - 1500) / span))
    if abs(x) < deadband:
        return 0.0
    # Rescale so output starts from 0 just outside the deadband
    return (abs(x) - deadband) / (1.0 - deadband) * (1 if x > 0 else -1)


def parse_srv(msg):
    """Return (ch1, ch2, ch3, ch4) from "SRVaaaabbbbccccdddd#" (missing CH3/CH4 read as 1500), or None."""
    msg = msg.strip()
    if not msg.startswith("SRV") or len(msg) < 11:
        return None
    try:
        ch = [int(msg[i:i + 4]) for i in range(3, min(len(msg.rstrip("#")), 19), 4)]
    except ValueError:
        return None
    return tuple(ch + [1500] * (4 - len(ch)))


class V7rcBridge(Node):
    def __init__(self):
        super().__init__("v7rc_bridge")
        self.port = self.declare_parameter("port", 6188).value
        self.max_linear = self.declare_parameter("max_linear", 0.3).value    # m/s
        # At 1.5 rad/s (±0.13 m/s per wheel) the skid-steer wheels stall when spinning in place
        self.max_angular = self.declare_parameter("max_angular", 3.0).value  # rad/s
        self.deadband = self.declare_parameter("deadband", 0.06).value       # fraction of full stick
        self.timeout = self.declare_parameter("timeout", 0.5).value          # s without packets -> stop
        self.linear_sign = -1.0 if self.declare_parameter("invert_linear", False).value else 1.0
        self.angular_sign = -1.0 if self.declare_parameter("invert_angular", True).value else 1.0
        self.log_channels = self.declare_parameter("log_channels", False).value
        # Stick travel from center (1500) to full deflection, per channel
        self.steer_span = float(self.declare_parameter("steer_span", 500).value)
        self.throttle_span = float(self.declare_parameter("throttle_span", 500).value)
        # Spinning in place: below ~0.18 m/s per wheel the wheels cannot overcome the side scrub,
        # so a light push is lifted to this wheel speed and full push still reaches max_angular
        self.min_spin_wheel = self.declare_parameter("min_spin_wheel_speed", 0.18).value  # m/s
        self.spin_linear_limit = self.declare_parameter("spin_linear_limit", 0.03).value  # m/s
        self.track_width = self.declare_parameter("track_width", 0.172).value              # m

        # Camera pan-tilt from the two stick axes the car mode leaves free
        p = self.declare_parameter
        self.pan_tilt = p("pan_tilt_enable", True).value
        self.pan_input = p("pan_input", 3).value           # V7RC channel number (1-4)
        self.tilt_input = p("tilt_input", 4).value
        self.pan_sign = -1.0 if p("invert_pan", True).value else 1.0  # stick right (2000) = pan right (-)
        self.tilt_sign = -1.0 if p("invert_tilt", False).value else 1.0
        self.pan_tilt_rate = p("pan_tilt_rate", 60.0).value  # deg/s at full stick
        self.pan_limits = (p("pan_min", -45.0).value, p("pan_max", 45.0).value)
        self.tilt_limits = (p("tilt_min", -30.0).value, p("tilt_max", 30.0).value)
        self.cam = [0.0, 0.0]       # current pan, tilt target (deg)
        self.cam_rates = (0.0, 0.0)
        self.cam_pub = self.create_publisher(Vector3, "pan_tilt/cmd", 10)

        self.pub = self.create_publisher(Twist, "cmd_vel", 10)
        self.lock = threading.Lock()
        self.latest = None          # (linear, angular)
        self.last_rx = 0.0
        self.sender = None
        self.stopped = True

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(0.5)
        threading.Thread(target=self.rx_loop, daemon=True).start()
        self.create_timer(0.05, self.publish_loop)  # 20 Hz

        self.get_logger().info(
            f"Listening for V7RC on UDP 0.0.0.0:{self.port} "
            f"(max {self.max_linear} m/s, {self.max_angular} rad/s, stop after {self.timeout}s silence)")

    def spin_angular(self, steer):
        """Map steer (-1..1) for an in-place turn so the wheel speed is never below min_spin_wheel."""
        if steer == 0.0:
            return 0.0
        half_track = self.track_width / 2
        max_wheel = max(self.max_angular * half_track, self.min_spin_wheel)
        wheel = self.min_spin_wheel + (max_wheel - self.min_spin_wheel) * abs(steer)
        return math.copysign(wheel / half_track, steer)

    def rx_loop(self):
        while rclpy.ok():
            try:
                data, addr = self.sock.recvfrom(256)
            except socket.timeout:
                continue
            text = data.decode(errors="ignore").strip()
            if text.startswith("SRT"):  # tank mode: same channel layout, all four stick axes live
                text = "SRV" + text[3:]
            if not text.startswith("SRV") and self.log_channels:
                # Other app modes (tank SRT, expert SS8, buttons): logged only, to map their channels
                if text.startswith("SS8") and len(text) >= 19:
                    try:  # 8 channels, 2 hex digits each, 0x64..0xC8 with centre 0x96
                        vals = [int(text[i:i + 2], 16) for i in range(3, 19, 2)]
                        self.get_logger().info(f"SS8 CH1-8={vals}", throttle_duration_sec=0.5)
                    except ValueError:
                        self.get_logger().info(f"raw {text!r}", throttle_duration_sec=0.5)
                else:
                    self.get_logger().info(f"raw {text!r}", throttle_duration_sec=0.5)
                continue
            ch = parse_srv(text)
            if ch is None:
                self.get_logger().warn(
                    f"Ignored packet {data[:24]!r}; set the V7RC app to car (車輛) or tank (坦克) mode",
                    throttle_duration_sec=3.0)
                continue
            linear = self.linear_sign * stick(ch[1], self.deadband, self.throttle_span) * self.max_linear
            steer = self.angular_sign * stick(ch[0], self.deadband, self.steer_span)
            angular = self.spin_angular(steer) if abs(linear) < self.spin_linear_limit else steer * self.max_angular
            pan_rate = self.pan_sign * stick(ch[self.pan_input - 1], self.deadband) * self.pan_tilt_rate
            tilt_rate = self.tilt_sign * stick(ch[self.tilt_input - 1], self.deadband) * self.pan_tilt_rate
            if self.log_channels:
                self.get_logger().info(
                    f"CH1={ch[0]} CH2={ch[1]} CH3={ch[2]} CH4={ch[3]} -> linear {linear:+.2f} m/s, "
                    f"angular {angular:+.2f} rad/s, pan {pan_rate:+.0f} deg/s, tilt {tilt_rate:+.0f} deg/s",
                    throttle_duration_sec=0.5)
            with self.lock:
                self.latest = (linear, angular)
                self.cam_rates = (pan_rate, tilt_rate)
                self.last_rx = time.monotonic()
            if addr[0] != self.sender:
                self.sender = addr[0]
                self.get_logger().info(f"V7RC connected from {addr[0]}")

    def publish_loop(self):
        with self.lock:
            fresh = self.latest is not None and time.monotonic() - self.last_rx < self.timeout
            linear, angular = self.latest if fresh else (0.0, 0.0)
            pan_rate, tilt_rate = self.cam_rates if fresh else (0.0, 0.0)
        if self.pan_tilt and (pan_rate or tilt_rate):
            dt = 0.05  # timer period
            self.cam[0] = min(max(self.cam[0] + pan_rate * dt, self.pan_limits[0]), self.pan_limits[1])
            self.cam[1] = min(max(self.cam[1] + tilt_rate * dt, self.tilt_limits[0]), self.tilt_limits[1])
            self.cam_pub.publish(Vector3(x=self.cam[0], y=self.cam[1], z=0.0))
        if not fresh:
            if not self.stopped:
                self.get_logger().warn("No V7RC packets, stopping the robot")
                self.pub.publish(Twist())
                self.stopped = True
            return
        self.stopped = False
        msg = Twist()
        msg.linear.x = linear
        msg.angular.z = angular
        self.pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = V7rcBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.pub.publish(Twist())  # Stop on exit
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
