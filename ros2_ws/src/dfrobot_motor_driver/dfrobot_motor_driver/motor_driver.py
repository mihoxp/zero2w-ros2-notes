"""/cmd_vel -> DFRobot DC Motor Driver HAT (DFR0592, I2C 0x10), open-loop differential drive.

Register map from DFRobot_RaspberryPi_DC_Motor.py:
  0x03 control mode (0 = DC motor)    0x0E PWM frequency / 50
  0x0F / 0x12 motor 1 / 2 orientation (1 = CW, 2 = CCW, 5 = STOP)
  0x10 / 0x13 motor 1 / 2 duty [integer %, tenths]
Encoders are not used: wheel speed is mapped linearly to duty, with max_wheel_speed at 100 %.
Motors stop when /cmd_vel goes quiet for `timeout` seconds and when the node exits.
"""
import fcntl
import os
import time

import rclpy
from geometry_msgs.msg import Twist
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

I2C_SLAVE = 0x0703
REG_PID, REG_CTRL_MODE, REG_PWM_FREQ = 0x01, 0x03, 0x0E
REG_ORIENTATION = {1: 0x0F, 2: 0x12}
CW, CCW, STOP = 0x01, 0x02, 0x05
EXPECTED_PID = 0xDF


class MotorHat:
    def __init__(self, bus, addr):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)

    def write(self, reg, data):
        os.write(self.fd, bytes([reg, *data]))

    def read(self, reg, n=1):
        os.write(self.fd, bytes([reg]))
        return os.read(self.fd, n)

    def begin(self, pwm_frequency):
        pid = self.read(REG_PID)[0]
        if pid != EXPECTED_PID:
            raise RuntimeError(f"DFRobot motor HAT not found (PID 0x{pid:02x}, expected 0xdf)")
        self.write(REG_CTRL_MODE, [0])  # DC motor mode
        self.write(REG_PWM_FREQ, [int(pwm_frequency / 50)])
        time.sleep(0.1)
        self.stop_all()

    def drive(self, motor, duty):
        """duty in -100..100; sign selects CW/CCW."""
        reg = REG_ORIENTATION[motor]
        if duty == 0:
            self.write(reg, [STOP])
            return
        d = min(abs(duty), 100.0)
        self.write(reg, [CW if duty > 0 else CCW])
        self.write(reg + 1, [int(d), int((d * 10) % 10)])

    def stop_all(self):
        for m in REG_ORIENTATION:
            self.write(REG_ORIENTATION[m], [STOP])

    def close(self):
        os.close(self.fd)


class MotorDriver(Node):
    def __init__(self):
        super().__init__("dfrobot_motor_driver")
        p = self.declare_parameter
        self.wheel_separation = p("wheel_separation", 0.15).value      # m, left-right wheel distance
        self.max_wheel_speed = p("max_wheel_speed", 0.5).value         # m/s at 100 % duty (estimate)
        self.min_duty = p("min_duty", 0.0).value                       # % to overcome motor deadzone
        self.timeout = p("timeout", 0.5).value                         # s without /cmd_vel -> stop
        self.left_motor = p("left_motor", 1).value
        self.right_motor = p("right_motor", 2).value
        self.left_sign = -1.0 if p("invert_left", False).value else 1.0
        self.right_sign = -1.0 if p("invert_right", True).value else 1.0  # mirrored mounting
        self.hat = MotorHat(p("i2c_bus", 1).value, p("i2c_address", 0x10).value)
        self.hat.begin(p("pwm_frequency", 1000).value)

        self.last_cmd = 0.0
        self.moving = False
        self.create_subscription(Twist, "cmd_vel", self.on_cmd_vel, 10)
        self.create_timer(0.1, self.watchdog)
        self.get_logger().info(
            f"DFRobot motor HAT ready (left=M{self.left_motor}, right=M{self.right_motor}, "
            f"{self.max_wheel_speed} m/s at 100 %, stop after {self.timeout}s without cmd_vel)")

    def duty(self, wheel_speed):
        if abs(wheel_speed) < 1e-3:
            return 0.0
        d = abs(wheel_speed) / self.max_wheel_speed * 100.0
        d = self.min_duty + d * (100.0 - self.min_duty) / 100.0
        return min(d, 100.0) * (1 if wheel_speed > 0 else -1)

    def on_cmd_vel(self, msg):
        half = self.wheel_separation / 2
        left = msg.linear.x - msg.angular.z * half
        right = msg.linear.x + msg.angular.z * half
        self.hat.drive(self.left_motor, self.left_sign * self.duty(left))
        self.hat.drive(self.right_motor, self.right_sign * self.duty(right))
        self.last_cmd = time.monotonic()
        self.moving = left != 0 or right != 0

    def watchdog(self):
        if self.moving and time.monotonic() - self.last_cmd > self.timeout:
            self.hat.stop_all()
            self.moving = False
            self.get_logger().warn("No /cmd_vel, motors stopped")

    def shutdown(self):
        self.hat.stop_all()
        self.hat.close()


def main(args=None):
    rclpy.init(args=args)
    node = MotorDriver()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):  # Ctrl+C or SIGTERM from systemd
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
