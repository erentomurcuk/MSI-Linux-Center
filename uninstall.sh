#!/usr/bin/env bash
# uninstall.sh - remove everything install.sh put on this machine.
#
# Does NOT touch: your saved profiles (/var/lib/msi-control), your config
# (/etc/msi-control), or the DKMS registration of a driver you may have
# installed from elsewhere.  Pass --purge to remove those too.
set -euo pipefail

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

LIBDIR=/usr/local/lib/msi-control
# Both spellings: the installer uses the dkms-conformant hyphen, but an
# earlier layout used an underscore - clean up whichever is present.
DRIVER_DST_HYPHEN=/usr/src/msi-ec-0.13
DRIVER_DST_UNDERSCORE=/usr/src/msi_ec-0.13
KREL="$(uname -r)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
if [ "$(id -u)" -ne 0 ]; then
    exec pkexec env DISPLAY="${DISPLAY:-:0}" XAUTHORITY="${XAUTHORITY:-}" \
        WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
        XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" "$SELF" "$@"
fi

log "stopping service"
systemctl disable --now msi-controld.service 2>/dev/null || true
rm -f /etc/systemd/system/msi-controld.service
rm -f /usr/lib/systemd/system-sleep/msi-control
systemctl daemon-reload

log "removing programs"
rm -f /usr/local/bin/msi-control /usr/local/bin/msi-control-gui
rm -rf "$LIBDIR" /usr/local/share/doc/msi-control

log "removing desktop entry + icon"
rm -f /usr/share/applications/msi-control.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/msi-control.svg
gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true

log "unloading + removing the msi-ec driver"
if lsmod | grep -q '^msi_ec'; then
    modprobe -r msi-ec 2>/dev/null || log "could not unload msi-ec (in use); it goes away at next boot"
fi
if command -v dkms >/dev/null && dkms status 2>/dev/null | grep -q 'msi[-_]ec'; then
    dkms status 2>/dev/null | awk -F'[,/ ]+' '/msi[-_]ec/ {print $1"/"$2}' \
        | while read -r ent; do
              [ -n "$ent" ] && dkms remove "$ent" -k "$KREL" 2>/dev/null || true
          done
fi
rm -f "/lib/modules/$KREL/updates/drivers/platform/x86/msi-ec.ko"
rm -rf "$DRIVER_DST_HYPHEN" "$DRIVER_DST_UNDERSCORE"
depmod -a "$KREL" 2>/dev/null || true
rm -f /etc/modprobe.d/msi-ec.conf /etc/modules-load.d/msi-ec.conf

if [ "$PURGE" -eq 1 ]; then
    log "purging state + config"
    rm -rf /var/lib/msi-control /etc/msi-control
else
    log "kept /var/lib/msi-control (profiles) and /etc/msi-control (config)"
    log "run with --purge to remove them too"
fi

log "uninstalled"
