#!/usr/bin/env python3
"""Identify the third fan: dump tach trio + candidate duty regs, then
compare against Cooler Boost (all fans -> 100%)."""
import os, time
EC = "/sys/devices/platform/msi-ec/debug"
def rd(addr):
    with open(os.path.join(EC, "ec_get"), "w") as f:
        f.write("%02x" % addr)
    with open(os.path.join(EC, "ec_get")) as f:
        v = f.read().strip()
    return int(v, 16) if v else None

def tach(a):
    return (rd(a) << 8) | rd(a+1)

def snapshot(tag):
    # 3 samples, take the median-ish (last) for stability
    out = {}
    for _ in range(3):
        for a in (0xc8, 0xca, 0xcc):
            out[a] = tach(a)
        time.sleep(0.4)
    duty = {a: rd(a) for a in
            (0x71, 0x89, 0x91, 0x92, 0x99, 0x79, 0x7a, 0x7b, 0x70, 0x88)}
    print(f"[{tag}] tach: " + "  ".join(
        "0x%02x=%d→%dRPM" % (a, out[a], 478000//out[a] if out[a] else 0)
        for a in (0xc8, 0xca, 0xcc)))
    print(f"[{tag}] duty: " + "  ".join(
        "0x%02x=%3d" % (a, v) for a, v in duty.items()))
    return out, duty

print("=== baseline ===")
b_t, b_d = snapshot("base")

# Cooler boost ON -> every fan 100%
os.system("gs66-center boost on >/dev/null 2>&1")
time.sleep(2.0)
print("=== cooler boost ON ===")
c_t, c_d = snapshot("boost")
os.system("gs66-center boost off >/dev/null 2>&1")
time.sleep(1.5)

print()
print("--- duty bytes that jumped toward 100 under boost ---")
for a in sorted(set(b_d) | set(c_d)):
    if b_d.get(a) != c_d.get(a):
        print("  0x%02x: %s -> %s" % (a, b_d.get(a), c_d.get(a)))
print("--- tach that rose under boost ---")
for a in (0xc8, 0xca, 0xcc):
    print("  0x%02x: %d -> %d RPM" % (a, 478000//b_t[a] if b_t[a] else 0,
                                      478000//c_t[a] if c_t[a] else 0))
