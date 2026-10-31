"""/cmd_vel -> DFRobot DC Motor Driver HAT (DFR0592, I2C 0x10), differential drive with
encoder PI speed control.

Register map from DFRobot_RaspberryPi_DC_Motor.py:
  0x03 control mode (0 = DC motor)    0x0E PWM frequency / 50
  0x0F / 0x12 motor 1 / 2 orientation (1 = CW, 2 = CCW, 5 = STOP)
  0x10 / 0x13 motor 1 / 2 duty [integer %, tenths]
  0x04 / 0x09 encoder enable          0x07 / 0x0C encoder reduction ratio (2 bytes)
  0x05 / 0x0A encoder speed (signed 16-bit, ~output-shaft RPM with the ratio set)
The board has no closed loop of its own (same duty gives the two motors different speeds), so
speed control runs here: the open-loop duty (feedforward, max_wheel_speed at 100 %) gets a PI
correction from the encoder speed. The board only refreshes the encoder speed every ~300 ms,
so the PI gains are kept low; feedforward gives the immediate response.

Publishes wheel_speeds (geometry_msgs/Vector3: x = left, y = right, measured m/s, + = forward).
Motors stop when /cmd_vel goes quiet for `timeout` seconds and when the node exits.
"""
import fcntl
import os
import time

import rclpy
from geometry_msgs.msg import Twist, Vector3
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

I2C_SLAVE = 0x0703
REG_PID, REG_CTRL_MODE, REG_PWM_FREQ = 0x01, 0x03, 0x0E
REG_ORIENTATION = {1: 0x0F, 2: 0x12}
REG_ENC_ENABLE = {1: 0x04, 2: 0x09}
REG_ENC_SPEED = {1: 0x05, 2: 0x0A}
REG_ENC_RATIO = {1: 0x07, 2: 0x0C}
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

    def begin(self, pwm_frequency, encoder_ratio):
        pid = self.read(REG_PID)[0]
        if pid != EXPECTED_PID:
            raise RuntimeError(f"DFRobot motor HAT not found (PID 0x{pid:02x}, expected 0xdf)")
        self.write(REG_CTRL_MODE, [0])  # DC motor mode
        self.write(REG_PWM_FREQ, [int(pwm_frequency / 50)])
        for m in REG_ENC_ENABLE:
            self.write(REG_ENC_ENABLE[m], [0x01])
            self.write(REG_ENC_RATIO[m], [encoder_ratio >> 8, encoder_ratio & 0xFF])
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

    def encoder_speed(self, motor):
        b = self.read(REG_ENC_SPEED[motor], 2)
        s = (b[0] << 8) | b[1]
        return s - 0x10000 if s & 0x8000 else s

    def stop_all(self):
        for m in REG_ORIENTATION:
            self.write(REG_ORIENTATION[m], [STOP])

    def close(self):
        os.close(self.fd)


class Wheel:
    """One track: motor channel, mounting sign and PI state."""

    def __init__(self, motor, sign):
        self.motor, self.sign = motor, sign
        self.target = 0.0
        self.integral = 0.0


class MotorDriver(Node):
    def __init__(self):
        super().__init__("dfrobot_motor_driver")
        p = self.declare_parameter
        self.wheel_separation = p("wheel_separation", 0.15).value      # m, left-right wheel distance
        self.max_wheel_speed = p("max_wheel_speed", 0.5).value         # m/s at 100 % duty (feedforward)
        self.min_duty = p("min_duty", 0.0).value                       # % to overcome motor deadzone
        self.timeout = p("timeout", 0.5).value                         # s without /cmd_vel -> stop
        self.left = Wheel(p("left_motor", 1).value, -1.0 if p("invert_left", False).value else 1.0)
        self.right = Wheel(p("right_motor", 2).value, -1.0 if p("invert_right", True).value else 1.0)
        # Encoder -> m/s: register ~ output RPM x rpm_scale; CW reads negative (encoder_sign -1)
        self.use_pid = p("use_pid", True).value
        self.kp = p("kp", 50.0).value                                  # % duty per m/s of error
        self.ki = p("ki", 100.0).value                                 # % duty per (m/s x s)
        self.max_correction = p("max_correction", 30.0).value          # % duty the PI may add/remove
        self.rpm_scale = p("encoder_rpm_scale", 0.91).value            # measured: 15 RPM read as 16.5
        self.circumference = p("wheel_circumference", 0.121).value     # m per sprocket turn (estimate)
        self.encoder_sign = p("encoder_sign", -1.0).value
        self.rate = p("control_rate", 20.0).value                      # Hz
        self.hat = MotorHat(p("i2c_bus", 1).value, p("i2c_address", 0x10).value)
        self.hat.begin(p("pwm_frequency", 1000).value, p("encoder_ratio", 75).value)

        self.last_cmd = 0.0
        self.speed_pub = self.create_publisher(Vector3, "wheel_speeds", 10)
        self.create_subscription(Twist, "cmd_vel", self.on_cmd_vel, 10)
        self.create_timer(1.0 / self.rate, self.control)
        self.get_logger().info(
            f"DFRobot motor HAT ready (left=M{self.left.motor}, right=M{self.right.motor}, "
            f"{self.max_wheel_speed} m/s at 100 %, PID {'on' if self.use_pid else 'off'} "
            f"kp={self.kp} ki={self.ki}, stop after {self.timeout}s without cmd_vel)")

    def feedforward(self, wheel_speed):
        if abs(wheel_speed) < 1e-3:
            return 0.0
        d = abs(wheel_speed) / self.max_wheel_speed * 100.0
        d = self.min_duty + d * (100.0 - self.min_duty) / 100.0
        return min(d, 100.0) * (1 if wheel_speed > 0 else -1)

    def measured(self, wheel):
        """Track speed in m/s, + = forward."""
        rpm = self.encoder_sign * self.hat.encoder_speed(wheel.motor) * self.rpm_scale
        return wheel.sign * rpm / 60.0 * self.circumference

    def on_cmd_vel(self, msg):
        half = self.wheel_separation / 2
        self.left.target = msg.linear.x - msg.angular.z * half
        self.right.target = msg.linear.x + msg.angular.z * half
        self.last_cmd = time.monotonic()

    def control(self):
        if time.monotonic() - self.last_cmd > self.timeout and (self.left.target or self.right.target):
            self.left.target = self.right.target = 0.0
            self.get_logger().warn("No /cmd_vel, motors stopped")
        speeds = []
        for wheel in (self.left, self.right):
            speed = self.measured(wheel)
            speeds.append(speed)
            if abs(wheel.target) < 1e-3:
                wheel.integral = 0.0
                self.hat.drive(wheel.motor, 0)
                continue
            duty = self.feedforward(wheel.target)
            if self.use_pid:
                error = wheel.target - speed
                limit = self.max_correction / self.ki if self.ki else 0.0
                wheel.integral = min(max(wheel.integral + error / self.rate, -limit), limit)
                correction = self.kp * error + self.ki * wheel.integral
                correction = min(max(correction, -self.max_correction), self.max_correction)
                # Never let the correction reverse the commanded direction
                duty = duty + correction
                duty = max(duty, 0.0) if wheel.target > 0 else min(duty, 0.0)
            self.hat.drive(wheel.motor, wheel.sign * duty)
        self.speed_pub.publish(Vector3(x=speeds[0], y=speeds[1], z=0.0))

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
