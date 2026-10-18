#!/usr/bin/env python3
"""Dump the full EC space twice, ~3s apart, so tachometer bytes (which
change as fans spin) are distinguishable from static registers."""
import os, time
EC = "/sys/devices/platform/msi-ec/debug"
def rd(addr):
    with open(os.path.join(EC, "ec_get"), "w") as f:
        f.write("%02x" % addr)
    with open(os.path.join(EC, "ec_get")) as f:
        v = f.read().strip()
    return int(v, 16) if v else None
a = [rd(x) for x in range(256)]
time.sleep(3)
b = [rd(x) for x in range(256)]
print("addr  a    b   delta")
for i in range(256):
    if a[i] != b[i]:
        print("0x%02x  %3d  %3d  %+d" % (i, a[i], b[i], b[i]-a[i]))
print("--- 16-bit BE candidates (period -> RPM plausible 500..9000) ---")
for i in range(255):
    for (lo, hi) in ((a[i], a[i+1]), (b[i], b[i+1])):
        per = (lo << 8) | hi
        if 52 <= per <= 956:   # 478000/9000 .. 478000/500
            rpm = 478000 // per if per else 0
            print("0x%02x: bytes %02x %02x -> period %d -> %d RPM" % (i, lo, hi, per, rpm))
            break
