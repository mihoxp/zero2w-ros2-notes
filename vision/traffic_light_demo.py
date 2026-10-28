#!/usr/bin/env python3
"""Live traffic light detection on the Zero 2 W camera.

Prints the state about twice a second and saves the latest annotated frame to
/tmp/traffic_light.jpg. Usage: python3 traffic_light_demo.py [seconds]
"""
import sys
import time
from pathlib import Path

import cv2

# Camera and detection code live in the color_vision ROS2 package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ros2_ws/src/color_vision"))

from color_vision import traffic_light  # noqa: E402
from color_vision.ov5647_camera import OV5647Camera  # noqa: E402


seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 15
cam = OV5647Camera(640, 480)
for _ in range(20):  # let exposure settle
    cam.read()

t_start = t_print = time.perf_counter()
frames, fps = 0, 0.0
stable = traffic_light.StableState(frames=3)
try:
    while time.perf_counter() - t_start < seconds:
        frame = cam.read()
        small = cv2.resize(frame, (320, 240), interpolation=cv2.INTER_AREA)  # detection at half size
        lamps = traffic_light.detect(small, min_area=12)
        current = stable.update(traffic_light.state(lamps))
        frames += 1
        now = time.perf_counter()
        if now - t_print > 0.5:
            fps = frames / (now - t_start)
            best = lamps[0] if lamps else None
            print(f"[{fps:4.1f} FPS] state={current:6s}"
                  + (f" score={best[2]:.1f} box={best[1]}" if best else ""), flush=True)
            cv2.imwrite("/tmp/traffic_light.jpg", traffic_light.annotate(small, lamps, fps))
            t_print = now
finally:
    cam.release()
