#!/usr/bin/env python3
"""Scan an I2C bus with read probes only (no i2c-tools needed). Usage: sudo python3 i2c_scan.py [bus]"""
import fcntl
import os
import sys

I2C_SLAVE = 0x0703

bus = int(sys.argv[1]) if len(sys.argv) > 1 else 1
fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
found = []
for addr in range(0x08, 0x78):
    try:
        fcntl.ioctl(fd, I2C_SLAVE, addr)
        os.read(fd, 1)
        found.append(addr)
    except OSError:
        pass
os.close(fd)
print(f"/dev/i2c-{bus}:", " ".join(f"0x{a:02x}" for a in found) or "no devices")
