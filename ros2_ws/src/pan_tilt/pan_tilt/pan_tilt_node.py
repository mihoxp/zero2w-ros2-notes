"""Camera pan-tilt on a SparkFun Servo pHAT (PCA9685 at I2C 0x40).

Channel 0 = pan, channel 1 = tilt (measured: pan 1.8 ms turns left, tilt 1.2 ms tilts up).
Angles are degrees from centre: pan + = left, tilt + = up.

Subscribes
  pan_tilt/cmd    geometry_msgs/Vector3   x = pan, y = tilt (degrees, absolute)
Publishes
  pan_tilt/state  geometry_msgs/Vector3   current pan/tilt after slew limiting (10 Hz)

Targets are clamped to the limits and approached at max_speed deg/s, so the camera does not
jerk. The servos are centred at start and released (no pulses) when the node exits.
"""
import fcntl
import os
import time

import rclpy
from geometry_msgs.msg import Vector3
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

I2C_SLAVE = 0x0703
MODE1, PRE_SCALE, LED0_ON_L = 0x00, 0xFE, 0x06
OSC_HZ = 25_000_000


class PCA9685:
    def __init__(self, bus=1, addr=0x40, freq=50):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)
        self.period_us = 1_000_000 / freq
        self.write(MODE1, 0x11)                                   # sleep (prescale is only writable asleep)
        self.write(PRE_SCALE, round(OSC_HZ / (4096 * freq)) - 1)
        self.write(MODE1, 0x21)                                   # wake, auto-increment, all-call
        time.sleep(0.001)
        self.write(MODE1, 0xA1)                                   # restart PWM

    def write(self, reg, *data):
        os.write(self.fd, bytes([reg, *data]))

    def pulse_us(self, channel, us):
        off = round(us / self.period_us * 4096)
        self.write(LED0_ON_L + 4 * channel, 0, 0, off & 0xFF, off >> 8)

    def release(self, channel):
        self.write(LED0_ON_L + 4 * channel, 0, 0, 0, 0x10)       # full OFF: servo goes limp

    def close(self):
        os.close(self.fd)


class Axis:
    def __init__(self, channel, center_us, us_per_deg, sign, lo, hi):
        self.channel, self.center_us, self.us_per_deg, self.sign = channel, center_us, us_per_deg, sign
        self.lo, self.hi = lo, hi
        self.angle = self.target = 0.0

    def set_target(self, deg):
        self.target = min(max(deg, self.lo), self.hi)

    def step(self, max_step):
        delta = self.target - self.angle
        self.angle += max(-max_step, min(max_step, delta))
        return self.center_us + self.sign * self.angle * self.us_per_deg


class PanTilt(Node):
    def __init__(self):
        super().__init__("pan_tilt")
        p = self.declare_parameter
        us_per_deg = p("us_per_degree", 11.1).value        # ~1000 us per 90 deg for SG90-class servos
        self.pan = Axis(p("pan_channel", 0).value, p("pan_center_us", 1500).value, us_per_deg,
                        +1, p("pan_min", -45.0).value, p("pan_max", 45.0).value)      # 1.8 ms = left
        self.tilt = Axis(p("tilt_channel", 1).value, p("tilt_center_us", 1500).value, us_per_deg,
                         -1, p("tilt_min", -30.0).value, p("tilt_max", 30.0).value)   # 1.2 ms = up
        self.max_speed = p("max_speed", 90.0).value        # deg/s
        self.rate = 50.0
        self.pca = PCA9685(p("i2c_bus", 1).value, p("i2c_address", 0x40).value)

        self.create_subscription(Vector3, "pan_tilt/cmd", self.on_cmd, 10)
        self.state_pub = self.create_publisher(Vector3, "pan_tilt/state", 10)
        self.create_timer(1.0 / self.rate, self.update)
        self.create_timer(0.1, self.publish_state)
        self.update()  # centre immediately
        self.get_logger().info(
            f"pan_tilt ready: pan ch{self.pan.channel} [{self.pan.lo}, {self.pan.hi}] deg (+ = left), "
            f"tilt ch{self.tilt.channel} [{self.tilt.lo}, {self.tilt.hi}] deg (+ = up), {self.max_speed} deg/s")

    def on_cmd(self, msg):
        self.pan.set_target(msg.x)
        self.tilt.set_target(msg.y)

    def update(self):
        step = self.max_speed / self.rate
        for axis in (self.pan, self.tilt):
            self.pca.pulse_us(axis.channel, axis.step(step))

    def publish_state(self):
        self.state_pub.publish(Vector3(x=self.pan.angle, y=self.tilt.angle, z=0.0))

    def shutdown(self):
        for axis in (self.pan, self.tilt):
            self.pca.release(axis.channel)
        self.pca.close()


def main(args=None):
    rclpy.init(args=args)
    node = PanTilt()
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
