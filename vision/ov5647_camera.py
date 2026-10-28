"""Continuous BGR frames from an OV5647 on Ubuntu 22.04 (unicam raw Bayer, no libcamera).

The sensor only delivers 10-bit packed GBRG ('pGAA'). In that format the first 4 of every
5 bytes are the high 8 bits of 4 pixels, so dropping the 5th byte gives an 8-bit Bayer image
for free; OpenCV then demosaics it at full resolution. Exposure/gain (software AE) and white
balance (gray world) are updated every few frames.
"""
import subprocess

import cv2
import numpy as np

SENSOR_MODES = {(640, 480), (1296, 972), (1920, 1080), (2592, 1944)}


def v4l2(*args):
    return subprocess.run(["v4l2-ctl", *args], check=True, capture_output=True, text=True).stdout


class OV5647Camera:
    def __init__(self, width=640, height=480, video="/dev/video0", subdev="/dev/v4l-subdev0",
                 target=0.35, update_every=10):
        if (width, height) not in SENSOR_MODES:
            raise ValueError(f"unsupported mode {width}x{height}, use one of {sorted(SENSOR_MODES)}")
        self.w, self.h, self.subdev = width, height, subdev
        self.target, self.update_every = target, update_every
        v4l2("-d", subdev, f"--set-subdev-fmt=pad=0,width={width},height={height},code=0x300e")
        v4l2("-d", video, f"--set-fmt-video=width={width},height={height},pixelformat=pGAA")
        self.stride = int(v4l2("-d", video, "--get-fmt-video").split("Bytes per Line")[1].split(":")[1].split()[0])

        self.cap = cv2.VideoCapture(video, cv2.CAP_V4L2)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"pGAA"))
        self.cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)  # deliver the raw buffer
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open {video}")

        self.exposure, self.gain = 500, 64  # gain 16 = 1x
        self.wb = np.array([1.0, 1.0, 1.0], np.float32)  # B, G, R gains
        self.count = 0
        self._apply()

    def _apply(self):
        v4l2("-d", self.subdev, f"--set-ctrl=exposure={self.exposure},analogue_gain={self.gain}")

    def read_raw8(self):
        ok, buf = self.cap.read()
        if not ok or buf is None:
            raise RuntimeError("camera read failed")
        frame = np.frombuffer(buf, np.uint8)[: self.stride * self.h].reshape(self.h, self.stride)
        return frame[:, : self.w * 5 // 4].reshape(self.h, self.w // 4, 5)[:, :, :4].reshape(self.h, self.w)

    def read(self):
        """Return a white-balanced BGR frame (uint8, full sensor resolution)."""
        raw = self.read_raw8()
        bgr = cv2.cvtColor(raw, cv2.COLOR_BayerGR2BGR)  # sensor GBRG == OpenCV "GR" naming
        self.count += 1
        if self.count % self.update_every == 0:
            self._auto_adjust(bgr)
        return cv2.multiply(bgr, (*self.wb.tolist(), 0))  # per-channel gains, saturating uint8

    def _auto_adjust(self, bgr):
        small = bgr[::8, ::8].astype(np.float32)
        means = small.reshape(-1, 3).mean(0)
        self.wb = (means[1] / np.maximum(means, 1)).astype(np.float32)  # gray world
        level = means[1] / 255
        if abs(level - self.target) > 0.05:
            scale = min(max(self.target / max(level, 1e-3), 0.5), 2.0)
            if scale > 1 and self.exposure >= 500:
                self.gain = int(min(self.gain * scale, 1023))
            elif scale < 1 and self.gain > 16:
                self.gain = int(max(self.gain * scale, 16))
            else:
                self.exposure = int(min(max(self.exposure * scale, 4), 500))
            self._apply()

    def release(self):
        self.cap.release()
