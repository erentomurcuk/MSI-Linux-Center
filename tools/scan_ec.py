#!/usr/bin/env python3
"""Find every fan-related EC register on the MSI, by measurement.

Read-only except for the cooler-boost bit (0x98.7), which the tool sets and
clears.  Answers three questions the earlier probes could not:

  1. Which EC bytes are tachometers?  A 16-bit period register moves
     continuously; a duty register jumps.  Sample the whole space twice,
     1.5 s apart, and look for pairs whose value drifts.
  2. Which bytes does Cooler Boost actually drive?  Toggle 0x98.7 and diff
     the entire EC image; that is the authoritative list of "the fans".
  3. What is the RPM scale?  Compare the tach periods against a fan whose
     true speed we can read elsewhere (the NVIDIA GPU fan, via nvidia-smi).

Run:  pkexec env PATH=/usr/local/bin:/usr/bin:/bin python3 tools/scan_ec.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

EC = "/sys/devices/platform/msi-ec/debug"


def rd(addr: int) -> int:
    with open(os.path.join(EC, "ec_get"), "w") as f:
        f.write("%02x" % addr)
    with open(os.path.join(EC, "ec_get")) as f:
        v = f.read().strip()
    return int(v, 16) if v else 0


def wr(addr: int, val: int) -> None:
    with open(os.path.join(EC, "ec_set"), "w") as f:
        f.write("%02x=%02x" % (addr, val))


def dump() -> list:
    return [rd(a) for a in range(0x100)]


def nvidia_fan():
    """GPU fan RPM from the driver, or None if unavailable."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "-q", "-d", "FAN"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Fan Speed") and "%" in line:
            try:
                return int(line.split(":")[1].split("%")[0].strip())
            except (IndexError, ValueError):
                return None
    return None


def tach_pairs(img: list) -> list:
    """Big-endian 16-bit pairs whose period looks fan-like (< 0x1000) and
    whose bytes are non-trivial."""
    out = []
    for a in range(0x100 - 1):
        period = (img[a] << 8) | img[a + 1]
        if 0x20 < period < 0x2000:
            out.append((a, period))
    return out


def main() -> int:
    if not os.path.isdir(EC):
        print("no EC debug interface", file=sys.stderr)
        return 1

    print("=== full EC image, two samples 1.5 s apart ===")
    a = dump()
    time.sleep(1.5)
    b = dump()
    moving = [i for i in range(0x100) if a[i] != b[i]]
    print("bytes that moved on their own (live counters):")
    print("  " + ", ".join("0x%02x(%d->%d)" % (i, a[i], b[i]) for i in moving)
          or "  (none)")

    print("\n=== candidate big-endian tach pairs in sample A ===")
    for addr, period in tach_pairs(a):
        print("  0x%02x: bytes %02x %02x  period=%4d  "
              "rpm(478000/p)=%5d  rpm(60e6/p)=%6d"
              % (addr, a[addr], a[addr + 1], period,
                 478000 // period if period else 0,
                 60_000_000 // period if period else 0))

    print("\n=== cooler boost diff (entire EC) ===")
    print("  nvidia fan before: %s" % nvidia_fan())
    wr(0x98, a[0x98] | 0x80)
    time.sleep(4.0)
    c = dump()
    wr(0x98, a[0x98] & 0x7F)
    time.sleep(2.0)
    print("  nvidia fan during boost: %s" % nvidia_fan())
    diff = [i for i in range(0x100) if a[i] != c[i]]
    print("  bytes changed by cooler boost:")
    for i in diff:
        print("    0x%02x: %3d -> %3d" % (i, a[i], c[i]))

    print("\n=== tach pairs under boost ===")
    for addr, period in tach_pairs(c):
        print("  0x%02x: period=%4d  rpm(478000/p)=%5d"
              % (addr, period, 478000 // period if period else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
