#!/usr/bin/env bash
# verify.sh - post-install live checks against the real hardware.
#
# Only performs SAFE, REVERSIBLE operations:
#   * reads (status, temps, RPM, curves)
#   * shift/fan mode round-trips (write then restore the original)
#   * Cooler Boost blip (on then immediately off)
#   * charge-threshold round-trip (write then restore)
#   * a curve write-back of the curve we just read (no net change)
#
# Nothing here leaves the machine in a state other than the one it found.
set -uo pipefail

BIN=/usr/local/bin/msi-control
EC=/sys/devices/platform/msi-ec
fail=0

# Discover the battery rather than assuming BAT1.  The daemon does exactly
# this (a hardcoded name would fail on any machine that calls it BAT0), and
# a verification script that silently reads nothing is worse than one that
# says so - every check below would then "pass" against an empty string.
BAT=""
for cand in /sys/class/power_supply/BAT*; do
    [ -e "$cand/charge_control_end_threshold" ] && BAT="$cand" && break
done

ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
bad()  { printf '\033[1;31m  ✗\033[0m %s\n' "$*"; fail=1; }
section() { printf '\n\033[1m%s\033[0m\n' "$*"; }

section "driver"
[ -d "$EC" ] && ok "msi-ec bound: $(cat $EC/fw_version 2>/dev/null)" \
             || bad "msi-ec sysfs dir missing"
[ -d "$EC/debug" ] && ok "debug interface present (RPM + curves available)" \
                   || bad "debug interface MISSING - RPM/curves unavailable"

section "daemon"
systemctl is-active --quiet msi-controld && ok "msi-controld active" \
                                        || bad "msi-controld not active"
"$BIN" wait --seconds 3 >/dev/null 2>&1 && ok "socket answering" || bad "socket not answering"

section "model"
# Which laptop this is, and whether a model file describes it.  Everything
# register-backed below depends on this, so it is checked first and its
# answer decides whether the curve check can run at all.
FEATURES=$("$BIN" status --json 2>/dev/null | python3 -c '
import json, sys
try:
    st = json.load(sys.stdin)
except Exception:
    print("curve=False rpm=False mux=False supported=False")
    raise SystemExit
m = st.get("model") or {}
f = m.get("features") or {}
print("curve=%s rpm=%s mux=%s supported=%s" % (
    bool(f.get("fan_curve")), bool(f.get("rpm")), bool(f.get("mux")),
    bool(m.get("supported"))))
' 2>/dev/null) || FEATURES="curve=False rpm=False mux=False supported=False"
eval "$FEATURES"
if [ "$supported" = "True" ]; then
    ok "a model definition matches this laptop  ($FEATURES)"
else
    bad "no model definition matches this laptop - fan curve, RPM and the graphics switch are off.  Run: msi-control detect"
fi

section "status"
"$BIN" status || bad "status failed"

section "shift mode round-trip"
orig=$("$BIN" mode 2>/dev/null | head -1)
for m in eco comfort sport turbo; do
    "$BIN" mode "$m" >/dev/null 2>&1 || { bad "cannot set $m"; continue; }
    got=$("$BIN" mode 2>/dev/null | head -1)
    [ "$got" = "$m" ] && ok "shift $m -> read back $got" || bad "shift $m -> read back '$got'"
done
"$BIN" mode "$orig" >/dev/null 2>&1
ok "restored shift mode to $orig"

section "fan mode round-trip"
# Capture the original first: the shift block above restores what it found,
# and this one must too.  It used to hardcode `fan auto`, so running the
# verifier silently moved a user who had deliberately chosen "advanced" back
# onto the firmware default - the opposite of what this script's header
# promises.
orig_fan=$("$BIN" fan 2>/dev/null | head -1)
for m in auto silent advanced; do
    "$BIN" fan "$m" >/dev/null 2>&1 || { bad "cannot set $m"; continue; }
    got=$("$BIN" fan 2>/dev/null | head -1)
    [ "$got" = "$m" ] && ok "fan $m -> read back $got" || bad "fan $m -> read back '$got'"
done
"$BIN" fan "$orig_fan" >/dev/null 2>&1
ok "restored fan mode to $orig_fan"

section "cooler boost blip (<1s at max)"
"$BIN" boost on >/dev/null 2>&1
b=$("$BIN" boost 2>/dev/null | head -1)
[ "$b" = "on" ] && ok "cooler boost ON" || bad "cooler boost did not read back on"
"$BIN" boost off >/dev/null 2>&1
b=$("$BIN" boost 2>/dev/null | head -1)
[ "$b" = "off" ] && ok "cooler boost OFF" || bad "cooler boost did not read off"

section "charge threshold round-trip"
if [ -z "$BAT" ]; then
    bad "no battery exposing charge_control_end_threshold found"
    cur=""
else
    ok "battery: $(basename "$BAT")"
    cur=$(cat "$BAT/charge_control_end_threshold" 2>/dev/null)
    for pct in 80 60; do
        "$BIN" battery "$pct" >/dev/null 2>&1 || { bad "cannot set $pct"; continue; }
        got=$(cat "$BAT/charge_control_end_threshold" 2>/dev/null)
        [ "$got" = "$pct" ] && ok "charge limit $pct -> EC says $got" || bad "charge limit $pct -> EC says '$got'"
        start=$(cat "$BAT/charge_control_start_threshold" 2>/dev/null)
        # Report what the start threshold actually is; only call it out as
        # wrong if it is present and does not follow the limit-minus-10 rule.
        if [ -z "$start" ]; then
            bad "  no start threshold readable on $(basename "$BAT")"
        elif [ "$start" -eq $((pct - 10)) ]; then
            ok "  (start threshold is $start, i.e. limit-10)"
        else
            bad "  start threshold is $start, expected $((pct - 10))"
        fi
    done
fi
if [ -n "${cur:-}" ]; then
    "$BIN" battery "$cur" >/dev/null 2>&1
    ok "restored charge limit to $cur"
else
    "$BIN" battery 100 >/dev/null 2>&1; ok "restored charge limit to 100"
fi

section "fan curve write-back (re-writing what is already there)"
# Use --json rather than scraping the human formatting: the display layout
# is allowed to change, the JSON contract is not.
if [ "$curve" != "True" ]; then
    ok "skipped - no model definition for this laptop (curve registers unknown)"
elif curve=$("$BIN" curve --json 2>/dev/null); then
    read -r temps speeds < <(printf '%s' "$curve" | python3 -c '
import json,sys
c=json.load(sys.stdin)["cpu"]
print(",".join(map(str,c["temps"])), ",".join(map(str,c["speeds"])))
' 2>/dev/null) || { temps=""; speeds=""; }
    if [ -n "$temps" ] && [ -n "$speeds" ]; then
        "$BIN" curve cpu --temps "$temps" --speeds "$speeds" >/dev/null 2>&1 \
            && ok "cpu curve re-written unchanged ($temps / $speeds)" \
            || bad "cpu curve write-back failed"
    else
        bad "could not parse CPU curve JSON"
    fi
else
    bad "no CPU curve readable (daemon down, or debug=1 not set)"
fi

section "battery / RPM sanity"
"$BIN" status --json 2>/dev/null | python3 -c '
import json, sys
st = json.load(sys.stdin)
for w in ("cpu", "gpu"):
    d = st[w]
    t, f, r = d.get("temp"), d.get("fan_pct"), d.get("rpm")
    print("  %s: %s°C  fan %s%%  %s RPM" % (w, t, f, r))
    # 0 RPM is a stopped fan, which is a real answer (the EC parks them at
    # idle), not a failure - only reject values that cannot be a tach count.
    assert r is None or 0 <= r < 10000, "%s RPM implausible: %s" % (w, r)
print("  RPM values plausible")
' && ok "RPM reads are sane" || bad "RPM check failed"

echo
if [ "$fail" -eq 0 ]; then
    printf '\033[1;32mALL CHECKS PASSED\033[0m\n'
else
    printf '\033[1;31mSOME CHECKS FAILED\033[0m\n'
fi
exit $fail
