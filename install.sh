#!/usr/bin/env bash
# install.sh — install GS66 Center on this machine.
#
# Run this as root (sudo ./install.sh), or let install.sh re-exec itself
# through pkexec so the password prompt appears on your desktop.
#
# What it does, in order:
#   1. checks prerequisites (Secure Boot must be off, kernel headers present)
#   2. installs the out-of-tree msi-ec driver via DKMS (falls back to a
#      direct build+install if DKMS is unavailable)
#   3. configures the driver to load with debug=1 (needed for RPM + curves)
#   4. installs the daemon/CLI/GUI under /usr/local
#   5. enables + starts the systemd service, installs the resume hook
#
# Everything it writes is listed in uninstall.sh — the two are symmetric.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/usr/local
LIBDIR=$PREFIX/lib/gs66-center
BINDIR=$PREFIX/bin
DOCDIR=$PREFIX/share/doc/gs66-center
DRIVER_SRC=$HERE/third_party/msi-ec
DRIVER_VER=0.13
# dkms.conf declares PACKAGE_NAME="msi-ec", and dkms looks for its source at
# /usr/src/<PACKAGE_NAME>-<VERSION>.  The hyphen is load-bearing: the
# upstream Makefile's own dkms target uses an underscore, which is why a
# `dkms add msi-ec/0.13` can fail with "module source directory missing".
DRIVER_DST=/usr/src/msi-ec-$DRIVER_VER
KREL="$(uname -r)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- re-exec --
# Re-exec through pkexec with an ABSOLUTE path: pkexec does not inherit our
# working directory, so a relative $0 would be lost ("No such file or directory").
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
if [ "$(id -u)" -ne 0 ]; then
    log "root required — asking via pkexec (a password dialog should appear)"
    exec pkexec env DISPLAY="${DISPLAY:-:0}" \
        XAUTHORITY="${XAUTHORITY:-}" \
        WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
        XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
        "$SELF" "$@"
fi

# ------------------------------------------------------------ prereqs ------
log "checking prerequisites"

if [ ! -d "/lib/modules/$KREL/build" ]; then
    die "kernel headers for $KREL not found.
       Install them first:  dnf install kernel-devel-$KREL
       (or matching your running kernel:  uname -r)"
fi
for tool in gcc make; do
    command -v "$tool" >/dev/null || die "$tool not found — dnf install $tool"
done

# Secure Boot: an out-of-tree module will simply not load if it is on.
if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -qi enabled; then
    die "Secure Boot is ENABLED.  The out-of-tree msi-ec driver cannot load
       while it is on.  Either disable Secure Boot in the firmware setup, or
       sign the module with your own key (out of scope for this installer)."
fi

[ -d "$DRIVER_SRC" ] || die "vendored driver source missing: $DRIVER_SRC"

# ------------------------------------------------------------- packages ----
if ! command -v dkms >/dev/null; then
    log "installing dkms"
    if command -v dnf >/dev/null; then
        dnf install -y dkms || warn "dnf could not install dkms; will fall back to a direct module build"
    else
        warn "no dnf; will fall back to a direct module build"
    fi
fi

# ------------------------------------------------------------ the driver ---
log "installing msi-ec $DRIVER_VER"

install -d "$DRIVER_DST"
cp -a "$DRIVER_SRC"/. "$DRIVER_DST"/

# dkms.conf ships with a literal @VERSION@ placeholder.
sed -i "s/@VERSION@/$DRIVER_VER/g" "$DRIVER_DST/dkms.conf"
grep -q "PACKAGE_VERSION=\"$DRIVER_VER\"" "$DRIVER_DST/dkms.conf" \
    || die "failed to substitute PACKAGE_VERSION in dkms.conf"

# dkms.conf points DEST_MODULE_LOCATION at /kernel/drivers/platform/x86 —
# the very directory holding the IN-TREE msi-ec.  Left alone, DKMS would
# overwrite the stock module file.  The upstream Makefile's own install
# target uses updates/ instead, which shadows the in-tree copy by modprobe
# search precedence and leaves the original untouched (so uninstalling is a
# file delete, not a kernel-package reinstall).
sed -i 's|^DEST_MODULE_LOCATION\[0\]=.*|DEST_MODULE_LOCATION[0]="/updates/drivers/platform/x86"|' \
    "$DRIVER_DST/dkms.conf"
grep -q 'DEST_MODULE_LOCATION\[0\]="/updates/drivers/platform/x86"' \
    "$DRIVER_DST/dkms.conf" \
    || warn "could not repoint DEST_MODULE_LOCATION; DKMS may overwrite the in-tree module"

install_driver_dkms() {
    log "building via DKMS"
    # dkms reads PACKAGE_NAME/PACKAGE_VERSION out of dkms.conf, and expects
    # the source at /usr/src/<name>-<version> — which DRIVER_DST now matches.
    dkms add "msi-ec/$DRIVER_VER"
    dkms build "msi-ec/$DRIVER_VER" -k "$KREL"
    dkms install "msi-ec/$DRIVER_VER" -k "$KREL"
}

install_driver_manual() {
    log "building the module directly (no DKMS)"
    make -C "/lib/modules/$KREL/build" M="$DRIVER_DST" modules
    install -d "/lib/modules/$KREL/updates/drivers/platform/x86"
    install -m 644 "$DRIVER_DST/msi-ec.ko" \
        "/lib/modules/$KREL/updates/drivers/platform/x86/msi-ec.ko"
    depmod -a "$KREL"
}

if command -v dkms >/dev/null; then
    install_driver_dkms || { warn "DKMS build failed, trying a direct build"; install_driver_manual; }
else
    install_driver_manual
fi

log "driver module configuration"
install -d /etc/modprobe.d /etc/modules-load.d
install -m 644 "$HERE/modprobe/msi-ec.conf" /etc/modprobe.d/msi-ec.conf
echo "msi-ec" > /etc/modules-load.d/msi-ec.conf

# Load it now (if not already).  The in-tree msi-ec may be bound; ours
# installs into updates/ which takes precedence, but only after a reload.
if lsmod | grep -q '^msi_ec'; then
    log "msi-ec already loaded — reloading to pick up the new build"
    modprobe -r msi-ec 2>/dev/null || warn "could not unload the in-tree msi-ec (in use); it will pick up on next boot"
fi
modprobe msi-ec || warn "modprobe msi-ec failed — will retry at boot"

# --------------------------------------------------------------- programs --
log "installing programs to $LIBDIR"
install -d "$LIBDIR" "$BINDIR" "$DOCDIR"
install -m 644 "$HERE/src/gs66_common.py" "$LIBDIR/"
install -m 644 "$HERE/src/gs66d.py"       "$LIBDIR/"
install -m 755 "$HERE/src/gs66-center"    "$BINDIR/gs66-center"
install -m 755 "$HERE/src/gs66-gui"       "$BINDIR/gs66-gui"

# The GUI's shebang uses env python3; make sure PySide6 is present.
if ! python3 -c 'import PySide6' >/dev/null 2>&1; then
    warn "PySide6 is not installed — the GUI will not start.
       Install it with:  dnf install python3-pyside6
       (the CLI and daemon work without it)"
fi

install -m 644 "$HERE/README.md" "$DOCDIR/" 2>/dev/null || true

# ---------------------------------------------------------------- config ---
install -d /etc/gs66-center /var/lib/gs66-center
if [ ! -f /etc/gs66-center/config.json ]; then
    log "writing default config"
    # The template lives in the repo (etc/config.json) so the shipped defaults
    # and the documented ones cannot drift apart.  Fall back to an inline copy
    # only if the template is somehow missing from the checkout.
    if [ -f "$HERE/etc/config.json" ]; then
        install -m 644 "$HERE/etc/config.json" /etc/gs66-center/config.json
    else
        cat > /etc/gs66-center/config.json <<'EOF'
{
  "RestoreLast": true,
  "DefaultProfile": null,
  "AllowRaw": false,
  "AllowMuxSwitch": false,
  "SocketGroup": "wheel",
  "ReapplyOnResume": true
}
EOF
        chmod 644 /etc/gs66-center/config.json
    fi
else
    log "keeping existing /etc/gs66-center/config.json"
fi

# --------------------------------------------------------------- service ---
log "enabling systemd service"
install -m 644 "$HERE/systemd/gs66-centerd.service" /etc/systemd/system/
install -m 755 "$HERE/systemd/gs66-center-sleep" /usr/lib/systemd/system-sleep/gs66-center
systemctl daemon-reload
systemctl enable gs66-centerd.service
systemctl restart gs66-centerd.service

# ---------------------------------------------------------------- desktop --
log "installing desktop entry + icon"
install -d /usr/share/applications /usr/share/icons/hicolor/scalable/apps
install -m 644 "$HERE/desktop/gs66-center.desktop" /usr/share/applications/
install -m 644 "$HERE/icons/gs66-center.svg" \
    /usr/share/icons/hicolor/scalable/apps/gs66-center.svg
gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true

# ------------------------------------------------------------------ done ---
log "waiting for the daemon"
for _ in $(seq 1 15); do
    if "$BINDIR/gs66-center" wait --seconds 1 >/dev/null 2>&1; then break; fi
done

echo
log "installed.  Checking the driver:"
if [ -d /sys/devices/platform/msi-ec ]; then
    echo "   firmware: $(cat /sys/devices/platform/msi-ec/fw_version 2>/dev/null || echo '?')"
    echo "   debug interface: $([ -d /sys/devices/platform/msi-ec/debug ] && echo yes || echo NO — RPM and fan curves will be unavailable)"
else
    warn "msi-ec did not create its sysfs directory — the driver is not bound.
       Check:  dmesg | tail -20        and:  modinfo msi-ec"
fi
echo
"$BINDIR/gs66-center" status 2>/dev/null || true
echo
log "done.  Try:  gs66-center status   ·   gs66-gui"
