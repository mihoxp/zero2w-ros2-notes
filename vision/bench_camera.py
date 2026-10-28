#!/usr/bin/env python3
"""Measure camera FPS on the Zero 2 W and save the last frame. Usage: python3 bench_camera.py [w h] [frames]"""
import sys
import time

import cv2

from ov5647_camera import OV5647Camera

w, h = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (640, 480)
n = int(sys.argv[3]) if len(sys.argv) > 3 else 60

cam = OV5647Camera(w, h)
for _ in range(20):  # let auto exposure / white balance settle
    cam.read()
t_raw = t_bgr = 0.0
for _ in range(n):
    t0 = time.perf_counter()
    raw = cam.read_raw8()
    t1 = time.perf_counter()
    bgr = cv2.cvtColor(raw, cv2.COLOR_BayerGR2BGR)
    t2 = time.perf_counter()
    t_raw += t1 - t0
    t_bgr += t2 - t1
t0 = time.perf_counter()
for _ in range(n):
    frame = cam.read()
fps = n / (time.perf_counter() - t0)
cv2.imwrite("/tmp/bench_frame.jpg", frame)
cam.release()
print(f"{w}x{h}: read+unpack {t_raw / n * 1000:.1f} ms, demosaic {t_bgr / n * 1000:.1f} ms, "
      f"full read() {fps:.1f} FPS, exposure={cam.exposure} gain={cam.gain}")
