#!/usr/bin/env python3
"""Log Zero 2 W power health every few seconds to a CSV that survives power loss.

Columns: time, uptime_s, throttled (hex), flags, core_v, temp_c, battery_v, battery_pct, ext_power
  throttled bits (vcgencmd get_throttled):
    0 under-voltage now      16 under-voltage has occurred since boot
    1 ARM freq capped now    17 freq capping has occurred
    2 throttled now          18 throttling has occurred
    3 soft temp limit now    19 soft temp limit has occurred
The "has occurred" bits are cleared by a reboot, so the log is what remains after a power loss.
Each line is flushed and fsynced; a line "BOOT" marks every start of the monitor.
Usage (root): power_monitor.py [csv_path] [interval_s]
"""
import fcntl
import os
import subprocess
import sys
import time

CSV = sys.argv[1] if len(sys.argv) > 1 else "/var/log/power-monitor.csv"
INTERVAL = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
FLAGS = {0: "UV", 1: "CAP", 2: "THR", 3: "TEMP", 16: "uv-seen", 17: "cap-seen", 18: "thr-seen", 19: "temp-seen"}


def vcgencmd(*args):
    out = subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=5).stdout
    return out.strip().split("=", 1)[-1]


def ups_reading():
    """UPS-Lite v1.3 (CW2015 at 0x62): (volts, percent), waking the gauge if needed."""
    try:
        fd = os.open("/dev/i2c-1", os.O_RDWR)
        fcntl.ioctl(fd, 0x0703, 0x62)

        def read(reg, n):
            os.write(fd, bytes([reg]))
            return os.read(fd, n)

        if read(0x0A, 1)[0] != 0x00:  # sleeping or quick-start bits left set
            os.write(fd, bytes([0x0A, 0x30]))
            time.sleep(0.05)
            os.write(fd, bytes([0x0A, 0x00]))
            time.sleep(2)
        v, s = read(0x02, 2), read(0x04, 2)
        os.close(fd)
        return ((v[0] << 8) | v[1]) * 0.305 / 1000, s[0] + s[1] / 256
    except OSError:
        return None, None


def ext_power():
    try:
        with open("/sys/class/gpio/gpio4/value") as f:
            return f.read().strip()
    except OSError:
        try:
            if not os.path.exists("/sys/class/gpio/gpio4"):
                with open("/sys/class/gpio/export", "w") as f:
                    f.write("4")
                time.sleep(0.1)
            with open("/sys/class/gpio/gpio4/direction", "w") as f:
                f.write("in")
            with open("/sys/class/gpio/gpio4/value") as f:
                return f.read().strip()
        except OSError:
            return ""


def main():
    new = not os.path.exists(CSV)
    with open(CSV, "a", buffering=1) as f:
        if new:
            f.write("time,uptime_s,throttled,flags,core_v,temp_c,battery_v,battery_pct,ext_power\n")
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},BOOT,,,,,,,\n")
        last_flags = None
        while True:
            try:
                thr = int(vcgencmd("get_throttled"), 16)
                core = vcgencmd("measure_volts", "core").rstrip("V")
                temp = vcgencmd("measure_temp").split("'")[0]
            except (subprocess.SubprocessError, ValueError):
                thr, core, temp = -1, "", ""
            flags = "+".join(name for bit, name in FLAGS.items() if thr >= 0 and thr & (1 << bit)) or "ok"
            bv, bp = ups_reading()
            with open("/proc/uptime") as u:
                uptime = float(u.read().split()[0])
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{uptime:.0f},0x{max(thr, 0):x},{flags},{core},{temp},"
                    f"{'' if bv is None else f'{bv:.3f}'},{'' if bp is None else f'{bp:.1f}'},{ext_power()}\n")
            f.flush()
            os.fsync(f.fileno())
            if flags != last_flags:  # state changes also go to the journal
                battery = "?" if bv is None else f"{bv:.3f}"
                print(f"power state: {flags} (throttled=0x{max(thr, 0):x}, core {core} V, battery {battery} V)",
                      flush=True)
                last_flags = flags
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
