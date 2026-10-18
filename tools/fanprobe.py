#!/usr/bin/env python3
"""Identify the GS66's three fans: which tach is which, and who drives them.

The GS66 Stealth has 3 fans (LibreHardwareMonitor reports "GS66 Stealth
10SG: 3").  The EC exposes three 16-bit big-endian tachometers at
0xc8 / 0xca / 0xcc, but only two fan *curves* (cpu 0x6a/0x72, gpu
0x82/0x8a) and two duty registers are documented.  This script settles it
by experiment instead of guesswork:

  1. baseline            — all three tachs, idle
  2. cpu curve -> floor  — force the CPU curve to a near-stopped speed
  3. gpu curve -> floor  — ditto for the GPU curve
  4. cooler boost        — everything at max
  5. wide duty scan      — every EC byte that moves with the fans

Whichever tach falls in step 2 belongs to the CPU curve; whichever falls
in step 3 belongs to the GPU curve.  A tach that answers neither has no
curve of its own here (i.e. it is slaved to one of them).

Needs root (the EC debug interface).  Run:  pkexec python3 tools/fanprobe.py
Safe: writes only the fan-curve and cooler-boost registers the tool already
owns, and restores the curve it read at the start.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

EC = "/sys/devices/platform/msi-ec/debug"
# The driver's own constant, imported rather than re-typed.  This file used
# to carry `N = 21300` while its comment claimed 478000 — every reading it
# produced was ~22x too low, which is what produced the "RPM scale must be
# wrong, ~90-360 RPM is implausible" entry in TODO.md.  The scale was never
# wrong; the probe was.  With the real constant the same registers read
# ~2000-8000 RPM, which is exactly what laptop fans do.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))), "src"))
import gs66_common as core                                # noqa: E402

N = core.RPM_NUMERATOR

CURVE = {  # which: (temps regs, speeds regs) — mirrors core.CURVE_REGS
    "cpu": (list(range(0x6A, 0x70)), list(range(0x72, 0x79))),
    "gpu": (list(range(0x82, 0x88)), list(range(0x8A, 0x91))),
}
TACHS = (0xC8, 0xCA, 0xCC)


def rd(addr: int) -> int:
    with open(os.path.join(EC, "ec_get"), "w") as f:
        f.write("%02x" % addr)
    with open(os.path.join(EC, "ec_get")) as f:
        v = f.read().strip()
    return int(v, 16) if v else 0


def wr(addr: int, val: int) -> None:
    with open(os.path.join(EC, "ec_set"), "w") as f:
        f.write("%02x=%02x" % (addr, val))


def tach(a: int) -> int:
    period = (rd(a) << 8) | rd(a + 1)
    return N // period if period else 0


def period_of(a: int) -> int:
    """The raw 16-bit period, before any constant is applied.

    Printed alongside every derived RPM on purpose.  The whole "the RPM
    scale must be wrong, ~90-360 RPM is implausible" detour in TODO.md came
    from this file carrying N = 21300 instead of the driver's 478000 — a
    ~22x error that is invisible when only the derived number is shown.  With
    the period printed too, a wrong constant is one division away from being
    obvious, and the raw data stays interpretable whatever the constant is.
    """
    return (rd(a) << 8) | rd(a + 1)


def rpm() -> dict:
    return {a: tach(a) for a in TACHS}


def periods() -> dict:
    return {a: period_of(a) for a in TACHS}


def settle(seconds: float = 4.0) -> dict:
    """Let the fans spin down/up, then take the last of several samples."""
    time.sleep(seconds)
    out = rpm()
    for _ in range(3):
        time.sleep(0.6)
        out = rpm()
    return out


def cli(*args) -> None:
    subprocess.run(["gs66-center", *args], capture_output=True, text=True)


def read_curve(which: str) -> dict:
    t_regs, s_regs = CURVE[which]
    return {"temps": [rd(a) for a in t_regs],
            "speeds": [rd(a) for a in s_regs]}


def write_curve(which: str, temps, speeds) -> None:
    t_regs, s_regs = CURVE[which]
    for a, v in zip(t_regs, temps):
        wr(a, v)
    for a, v in zip(s_regs, speeds):
        wr(a, v)


def show(tag: str, r: dict, base: dict | None = None) -> None:
    per = periods()
    bits = []
    for a in TACHS:
        mark = ""
        if base and r[a] < base[a] * 0.6:
            mark = "  <-- FELL"
        elif base and r[a] > base[a] * 1.4:
            mark = "  <-- ROSE"
        # period printed beside the RPM: if N is ever wrong again, the raw
        # value is here to divide yourself rather than only a bad number.
        bits.append("0x%02x=%4d rpm (period %4d)%s"
                    % (a, r[a], per[a], mark))
    print("  [%-16s] %s" % (tag, "   ".join(bits)))


def main() -> int:
    if not os.path.isdir(EC):
        print("no EC debug interface — load msi-ec with debug=1", file=sys.stderr)
        return 1

    saved = {w: read_curve(w) for w in CURVE}
    print("current curves:")
    for w, c in saved.items():
        print("  %s temps=%s speeds=%s" % (w.upper(), c["temps"], c["speeds"]))

    # sane, quiet starting point so a "floor" actually moves something
    cli("fan", "advanced")
    quiet = {"temps": [50, 55, 60, 65, 70, 75],
             "speeds": [55, 58, 62, 66, 70, 75, 80]}
    floor = {"temps": [50, 55, 60, 65, 70, 75],
             "speeds": [10, 12, 14, 16, 18, 20, 22]}
    for w in CURVE:
        write_curve(w, quiet["temps"], quiet["speeds"])
    base = settle()
    print("\n1. baseline (both curves at a known mid speed)")
    show("baseline", base, base)

    print("\n2. CPU curve -> floor (GPU untouched)")
    write_curve("cpu", floor["temps"], floor["speeds"])
    r = settle()
    show("cpu floor", r, base)

    print("\n3. GPU curve -> floor (CPU back to mid)")
    write_curve("cpu", quiet["temps"], quiet["speeds"])
    write_curve("gpu", floor["temps"], floor["speeds"])
    r = settle()
    show("gpu floor", r, base)

    print("\n4. cooler boost (all fans max)")
    write_curve("gpu", quiet["temps"], quiet["speeds"])
    cli("boost", "on")
    r = settle(5.0)
    show("boost on", r, base)
    cli("boost", "off")
    settle(2.0)

    print("\n5. restoring original curves")
    for w, c in saved.items():
        write_curve(w, c["temps"], c["speeds"])
    cli("fan", "auto")
    settle(1.0)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
