"""Traffic light state from colour: find bright, saturated, round blobs that are red, yellow or green.

Works for lit lamps (LED model traffic lights, or a traffic light picture on a screen). Each
candidate gets a score from size, roundness and brightness; the strongest one decides the state.
"""
import cv2
import numpy as np

# OpenCV hue is 0..179
HUE_RANGES = {
    "red": [(0, 10), (165, 179)],
    "yellow": [(15, 35)],
    "green": [(45, 95)],  # LED greens lean towards cyan
}
DRAW = {"red": (0, 0, 255), "yellow": (0, 220, 255), "green": (0, 200, 0)}


def detect(bgr, min_area=30, max_area_frac=0.05, min_circularity=0.55, min_sat=90, min_val=140,
           use_hull=False, min_solidity=0.75):
    """Return lamps as [(colour, (x, y, w, h), score)], strongest first.

    use_hull: judge roundness on the convex hull (ragged edges from glare or touching objects
    otherwise drop circularity to ~0.4), and require the blob to fill `min_solidity` of its hull.
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    bright = cv2.inRange(s, min_sat, 255) & cv2.inRange(v, min_val, 255)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    max_area = max_area_frac * bgr.shape[0] * bgr.shape[1]
    lamps = []
    for colour, ranges in HUE_RANGES.items():
        mask = np.zeros_like(h)
        for lo, hi in ranges:
            mask |= cv2.inRange(h, lo, hi)
        mask &= bright
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if not min_area <= area <= max_area:
                continue
            shape = c
            if use_hull:
                shape = cv2.convexHull(c)
                hull_area = cv2.contourArea(shape)
                if not hull_area or area / hull_area < min_solidity:
                    continue
            perimeter = cv2.arcLength(shape, True)
            circularity = 4 * np.pi * cv2.contourArea(shape) / (perimeter * perimeter) if perimeter else 0
            if circularity < min_circularity:
                continue
            x, y, w, hh = cv2.boundingRect(c)
            if not 0.6 < w / hh < 1.6:
                continue
            brightness = cv2.mean(v[y:y + hh, x:x + w])[0] / 255
            score = circularity * brightness * np.sqrt(area)
            lamps.append((colour, (x, y, w, hh), float(score)))
    return sorted(lamps, key=lambda l: -l[2])


def detect_objects(bgr, min_area=30):
    """Coloured (unlit) objects such as painted blocks or signs: lower saturation/brightness limits.

    Measured on red/yellow/green blocks: yellow S 86-125, green V 79-99, which the lamp limits reject.
    """
    return detect(bgr, min_area=min_area, min_sat=70, min_val=60, use_hull=True)


MIN_SCORE = 5.0  # small lit specks (e.g. plants under an aquarium light) score ~2-3


def state(lamps, min_score=MIN_SCORE):
    return lamps[0][0] if lamps and lamps[0][2] >= min_score else "none"


class StableState:
    """Only change the reported state after it has been seen `frames` times in a row."""

    def __init__(self, frames=3):
        self.frames, self.current, self.candidate, self.count = frames, "none", "none", 0

    def update(self, new):
        if new == self.candidate:
            self.count += 1
        else:
            self.candidate, self.count = new, 1
        if self.count >= self.frames:
            self.current = new
        return self.current


def annotate(bgr, lamps, fps=None):
    out = bgr.copy()
    for colour, (x, y, w, h), score in lamps[:5]:
        cv2.rectangle(out, (x, y), (x + w, y + h), DRAW[colour], 2)
        cv2.putText(out, f"{colour} {score:.1f}", (x, max(y - 4, 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, DRAW[colour], 1)
    label = f"state: {state(lamps)}" + (f"  {fps:.1f} FPS" if fps else "")
    cv2.putText(out, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return out


if __name__ == "__main__":
    # Self-test on a synthetic traffic light: housing with one lit lamp per run
    for lit in ("red", "yellow", "green"):
        img = np.full((480, 640, 3), 60, np.uint8)
        cv2.rectangle(img, (280, 120), (360, 360), (30, 30, 30), -1)
        for i, colour in enumerate(("red", "yellow", "green")):
            c = DRAW[colour] if colour == lit else tuple(int(x * 0.25) for x in DRAW[colour])
            cv2.circle(img, (320, 160 + i * 80), 28, c, -1)
        found = detect(img)
        print(f"lit={lit:6s} -> state={state(found):6s} candidates={[(c, round(s, 1)) for c, _, s in found]}")
