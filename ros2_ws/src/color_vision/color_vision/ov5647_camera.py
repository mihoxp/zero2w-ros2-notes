"""Continuous BGR frames from an OV5647 on Ubuntu 22.04 (unicam raw Bayer, no libcamera).

The sensor only delivers 10-bit packed GBRG ('pGAA'). In that format the first 4 of every
5 bytes are the high 8 bits of 4 pixels, so dropping the 5th byte gives an 8-bit Bayer image
for free; OpenCV then demosaics it at full resolution. Exposure/gain (software AE) and white
balance (gray world) are updated every few frames.

Auto exposure works on the product exposure x gain in three stages, so normal light keeps the
full frame rate and darkness is met with longer exposure before noisy high gain:
  1. exposure up to the base frame length (500 lines at 640x480), gain up to slow_gain (8x)
  2. gain held at slow_gain, exposure lengthened by raising vertical blanking
     (sensor limit: exposure <= vblank + 476 lines; frame rate drops, ~10 fps at max_vblank 1000)
  3. exposure at its maximum, gain raised up to 1023
"""
import subprocess

import cv2
import numpy as np

SENSOR_MODES = {(640, 480), (1296, 972), (1920, 1080), (2592, 1944)}
MIN_VBLANK = 24
EXPOSURE_MARGIN = 476   # measured: max exposure = vertical_blanking + 476 lines (640x480 mode)
MIN_GAIN, MAX_GAIN = 16, 1023   # analogue_gain 16 = 1x


def v4l2(*args):
    return subprocess.run(["v4l2-ctl", *args], check=True, capture_output=True, text=True).stdout


class OV5647Camera:
    def __init__(self, width=640, height=480, video="/dev/video0", subdev="/dev/v4l-subdev0",
                 target=0.35, update_every=10, max_vblank=1000, slow_gain=128):
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

        self.max_vblank, self.slow_gain = max_vblank, slow_gain
        self.base_exposure = MIN_VBLANK + EXPOSURE_MARGIN
        self.exposure, self.gain, self.vblank = self.base_exposure, 64, MIN_VBLANK
        self.wb = np.array([1.0, 1.0, 1.0], np.float32)  # B, G, R gains
        self.count = 0
        self._apply()

    def _apply(self):
        # Blanking first: it sets the upper limit the exposure value is checked against
        v4l2("-d", self.subdev, f"--set-ctrl=vertical_blanking={self.vblank}")
        v4l2("-d", self.subdev, f"--set-ctrl=exposure={self.exposure},analogue_gain={self.gain}")

    def _allocate(self, light):
        """Split the wanted exposure x gain product into (exposure, gain, vblank)."""
        max_exposure = self.max_vblank + EXPOSURE_MARGIN
        light = min(max(light, 4 * MIN_GAIN), max_exposure * MAX_GAIN)
        if light <= self.base_exposure * self.slow_gain:          # stage 1: full frame rate
            exposure = min(self.base_exposure, max(4, light / MIN_GAIN))
            gain = light / exposure
        elif light <= max_exposure * self.slow_gain:              # stage 2: slow down, keep gain
            gain = self.slow_gain
            exposure = light / gain
        else:                                                     # stage 3: longest exposure, more gain
            exposure = max_exposure
            gain = light / exposure
        exposure = int(round(exposure))
        return exposure, int(min(max(round(gain), MIN_GAIN), MAX_GAIN)), max(MIN_VBLANK, exposure - EXPOSURE_MARGIN)

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
            new = self._allocate(self.exposure * self.gain * scale)
            if new != (self.exposure, self.gain, self.vblank):
                self.exposure, self.gain, self.vblank = new
                self._apply()

    def release(self):
        self.cap.release()
