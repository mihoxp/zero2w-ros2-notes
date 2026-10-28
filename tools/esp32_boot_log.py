#!/usr/bin/env python3
"""Reset an ESP32 through its USB serial chip (RTS -> EN) and print the boot log.

Usage: python3 esp32_boot_log.py [/dev/ttyUSB0] [seconds]
"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 4

s = serial.Serial(port, 115200, timeout=0.2)
s.dtr = False          # keep GPIO0 high -> normal boot, not download mode
s.rts = True           # pull EN low
time.sleep(0.1)
s.rts = False          # release EN -> reset
buf = b""
end = time.time() + seconds
while time.time() < end:
    buf += s.read(4096)
s.close()
print(buf.decode(errors="replace") or "(no output)")
