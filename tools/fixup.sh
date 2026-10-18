#!/usr/bin/env bash
# One-shot privileged fixup: redeploy the corrected programs, finish DKMS
# registration (so the module rebuilds on kernel updates), restart the daemon.
set -euo pipefail
# Derive the project root from this script rather than hard-coding it: the
# tree moved to ~/Projects/GS66Control and a stale path here would make
# pkexec fail on a missing file while everything else still looked fine.
SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
LIBDIR=/usr/local/lib/gs66-center
VER=0.13
DST=/usr/src/msi-ec-$VER
KREL="$(uname -r)"

echo "==> redeploy programs"
install -m 644 "$SRC/src/gs66_common.py" "$LIBDIR/"
install -m 644 "$SRC/src/gs66d.py"       "$LIBDIR/"
install -m 755 "$SRC/src/gs66-center"    /usr/local/bin/gs66-center
install -m 755 "$SRC/src/gs66-gui"       /usr/local/bin/gs66-gui

echo "==> DKMS registration"
# Start clean so a re-run is idempotent.
if dkms status 2>/dev/null | grep -q "msi-ec/$VER"; then
    dkms remove "msi-ec/$VER" -k "$KREL" 2>/dev/null || true
fi
rm -rf "$DST" /usr/src/msi_ec-$VER
install -d "$DST"
cp -a "$SRC/third_party/msi-ec/." "$DST"/
sed -i "s/@VERSION@/$VER/g" "$DST/dkms.conf"
sed -i 's|^DEST_MODULE_LOCATION\[0\]=.*|DEST_MODULE_LOCATION[0]="/updates/drivers/platform/x86"|' "$DST/dkms.conf"
echo "--- dkms.conf in effect:"
grep -E 'PACKAGE_(NAME|VERSION)|DEST_MODULE_LOCATION' "$DST/dkms.conf"

dkms add "msi-ec/$VER"
dkms build "msi-ec/$VER" -k "$KREL"
dkms install "msi-ec/$VER" -k "$KREL"
dkms status | grep msi-ec || true

echo "==> restart daemon"
systemctl restart gs66-centerd
sleep 2
systemctl is-active gs66-centerd
echo "==> done"
