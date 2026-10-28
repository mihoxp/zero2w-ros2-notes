#!/usr/bin/env python3
"""UPS-Lite v1.3 (CW2015 fuel gauge at I2C 0x62): battery voltage, charge and external power.

The CW2015 starts in sleep mode after power-up (MODE reg 0x0A = 0xC0). It is woken with a
quick-start pulse: MODE = 0x30, then back to 0x00 (normal). The vendor script writes 0x30 and
leaves it set, which keeps SOC at 0 % or stale. GPIO4 is HIGH while external power is plugged in.
Usage: sudo python3 ups_lite.py [--watch]
"""
import fcntl
import os
import sys
import time

I2C_SLAVE = 0x0703
ADDR = 0x62
REG_VCELL, REG_SOC, REG_MODE = 0x02, 0x04, 0x0A


def read(fd, reg, n):
    os.write(fd, bytes([reg]))  # set register pointer
    return os.read(fd, n)


def external_power():
    """GPIO4 level via sysfs; None if it cannot be read."""
    gpio = "/sys/class/gpio/gpio4"
    try:
        if not os.path.exists(gpio):
            with open("/sys/class/gpio/export", "w") as f:
                f.write("4")
            time.sleep(0.1)
        with open(f"{gpio}/direction", "w") as f:
            f.write("in")
        with open(f"{gpio}/value") as f:
            return f.read().strip() == "1"
    except OSError:
        return None


def main():
    fd = os.open("/dev/i2c-1", os.O_RDWR)
    fcntl.ioctl(fd, I2C_SLAVE, ADDR)
    if read(fd, REG_MODE, 1)[0] != 0x00:  # sleeping, or quick-start bits left set
        os.write(fd, bytes([REG_MODE, 0x30]))  # quick start
        time.sleep(0.05)
        os.write(fd, bytes([REG_MODE, 0x00]))  # back to normal so SOC is computed
        time.sleep(2)
    while True:
        v = read(fd, REG_VCELL, 2)
        soc = read(fd, REG_SOC, 2)
        volts = ((v[0] << 8) | v[1]) * 0.305 / 1000
        percent = soc[0] + soc[1] / 256
        power = {True: "plugged in", False: "on battery", None: "unknown"}[external_power()]
        print(f"battery {volts:.3f} V  {percent:.1f} %  external power: {power}", flush=True)
        if "--watch" not in sys.argv:
            break
        time.sleep(5)
    os.close(fd)


if __name__ == "__main__":
    main()
