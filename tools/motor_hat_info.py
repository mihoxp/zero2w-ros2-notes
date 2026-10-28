#!/usr/bin/env python3
"""DFRobot DC Motor Driver HAT (DFR0592, I2C 0x10): read-only identity and status check.

Register map from DFRobot_RaspberryPi_DC_Motor.py. Nothing is written except the register pointer.
Usage: sudo python3 motor_hat_info.py
"""
import fcntl
import os

I2C_SLAVE = 0x0703
ADDR = 0x10
REG_PID, REG_PVD, REG_CTRL_MODE = 0x01, 0x02, 0x03
REG_ENCODER_EN = (0x04, 0x09)
REG_ORIENTATION = (0x0F, 0x12)
EXPECTED_PID = 0xDF

fd = os.open("/dev/i2c-1", os.O_RDWR)
fcntl.ioctl(fd, I2C_SLAVE, ADDR)


def read(reg, n=1):
    os.write(fd, bytes([reg]))
    return os.read(fd, n)


pid = read(REG_PID)[0]
print(f"PID 0x{pid:02x} ({'OK' if pid == EXPECTED_PID else 'unexpected, expected 0xdf'})")
print(f"version byte 0x{read(REG_PVD)[0]:02x}, control mode {read(REG_CTRL_MODE)[0]}")
for i in (0, 1):
    print(f"motor {i + 1}: encoder {'on' if read(REG_ENCODER_EN[i])[0] else 'off'}, "
          f"orientation {read(REG_ORIENTATION[i])[0]}")
os.close(fd)
