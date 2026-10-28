#!/usr/bin/env python3
"""Take a color snapshot from an OV5647 on Ubuntu 22.04 without libcamera.

The unicam driver only delivers raw 10-bit packed Bayer (GBRG) from /dev/video0, so this
captures with v4l2-ctl (package v4l-utils), runs a simple software auto-exposure on the
sensor's exposure/gain controls, then demosaics (half resolution), white-balances (gray
world) and gamma-corrects with numpy.
Sensor modes: 640x480, 1296x972 (binned, full field of view), 1920x1080 (cropped), 2592x1944.
Output is half the sensor size because of the simple demosaic.
Usage: python3 camera_snapshot.py [out.jpg] [width height]
"""
import subprocess
import sys

import numpy as np
from PIL import Image

VIDEO, SUBDEV = "/dev/video0", "/dev/v4l-subdev0"
out = sys.argv[1] if len(sys.argv) > 1 else "snapshot.jpg"
W, H = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (640, 480)
RAW = "/tmp/camera_frame.raw"


def v4l2(*args):
    return subprocess.run(["v4l2-ctl", *args], check=True, capture_output=True, text=True).stdout


def set_ctrl(exposure, gain):
    v4l2("-d", SUBDEV, f"--set-ctrl=exposure={exposure},analogue_gain={gain}")


def grab():
    """Capture 3 frames (sensor settles after a control change) and return the last as 0..1 floats."""
    # Only 2 buffers: the Zero 2 W has 64 MB of CMA and a 5 MP raw frame is ~6 MB
    v4l2("-d", VIDEO, "--stream-mmap=2", "--stream-count=3", f"--stream-to={RAW}")
    stride = int(v4l2("-d", VIDEO, "--get-fmt-video").split("Bytes per Line")[1].split(":")[1].split()[0])
    data = np.fromfile(RAW, dtype=np.uint8)
    frame = data[-stride * H:].reshape(H, stride)[:, : W * 5 // 4]
    # 10-bit packed: 4 pixels in 5 bytes, the 5th byte holds the 2 low bits of each
    b = frame.reshape(H, W // 4, 5).astype(np.uint16)
    px = np.empty((H, W // 4, 4), np.uint16)
    for i in range(4):
        px[..., i] = (b[..., i] << 2) | ((b[..., 4] >> (2 * i)) & 0x3)
    return px.reshape(H, W).astype(np.float32) / 1023.0  # float32 keeps 5 MP within 414 MB RAM


v4l2("-d", SUBDEV, f"--set-subdev-fmt=pad=0,width={W},height={H},code=0x300e")
v4l2("-d", VIDEO, f"--set-fmt-video=width={W},height={H},pixelformat=pGAA")

exposure, gain = 500, 32  # gain 16 = 1x
for _ in range(8):  # software auto-exposure toward mean ~0.18 on the raw data
    set_ctrl(exposure, gain)
    raw = grab()
    mean = raw.mean()
    if 0.12 < mean < 0.25:
        break
    scale = min(max(0.18 / max(mean, 1e-3), 0.25), 4.0)
    exposure = int(min(max(exposure * scale, 4), 500))
    if exposure == 500 and scale > 1:
        gain = int(min(gain * scale, 1023))
    elif scale < 1 and gain > 16:
        gain = int(max(gain * scale, 16))
print(f"exposure={exposure} analogue_gain={gain} raw mean={raw.mean():.3f}")

# GBRG: row 0 = G B, row 1 = R G -> half-resolution RGB
g = (raw[0::2, 0::2] + raw[1::2, 1::2]) / 2
bl, rd = raw[0::2, 1::2], raw[1::2, 0::2]
rgb = np.dstack([rd, g, bl]) - np.float32(16 / 1023)  # remove black level
rgb = np.clip(rgb, 0, None)
rgb *= rgb[..., 1].mean() / np.maximum(rgb.reshape(-1, 3).mean(0), 1e-6)  # gray-world white balance
rgb = np.clip(rgb / max(np.percentile(rgb, 99), 1e-6), 0, 1) ** (1 / 2.2)
Image.fromarray((rgb * 255).astype(np.uint8)).save(out, quality=90)
print(f"saved {out} ({rgb.shape[1]}x{rgb.shape[0]})")
