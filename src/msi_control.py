#!/usr/bin/env python3
"""msi-control — shared core: EC register map, paths, protocol, validation.

Everything userspace knows about the MSI GS66 10SE embedded controller lives
here, in one place, so the daemon, the CLI and the GUI can never disagree
about a register address or a value range.

EC register map sources (cross-checked against each other):
  * isw's [16V1EMS1] section + MSI_ADDRESS_DEFAULT profile (YoyPa/isw),
    which carries a real EC dump from a GS66-10SE (EC 16V1EMS1).
  * teackot/msi-ec CONF_G1_3, the out-of-tree msi-ec configuration that
    explicitly lists "16V1EMS1.118 // GS66 Stealth 10SE".
  * the in-tree drivers/platform/x86/msi-ec.c charge-control semantics
    (start and end thresholds are one EC byte, always 10% apart).

Everything here is stdlib-only on purpose: the daemon runs as root and the
CLI must work in recovery shells.
"""

from __future__ import annotations

import glob
import json
import os
import re
import socket

# --------------------------------------------------------------------------
# Paths — every one overridable through the environment so the whole stack
# can be exercised against a fake sysfs tree without root (see tests/).
# --------------------------------------------------------------------------

def sysfs_root() -> str:
    return os.environ.get("MSI_SYSFS", "/sys")


def sock_path() -> str:
    return os.environ.get("MSI_SOCK", "/run/msi-controld.sock")


def config_path() -> str:
    return os.environ.get("MSI_CONFIG", "/etc/msi-control/config.json")


def state_dir() -> str:
    return os.environ.get("MSI_STATE", "/var/lib/msi-control")


def msi_ec_dir() -> str:
    return os.path.join(sysfs_root(), "devices", "platform", "msi-ec")


def power_dir() -> str:
    return os.path.join(sysfs_root(), "class", "power_supply")


# --------------------------------------------------------------------------
# EC register map for the GS66 (EC firmware family 16V1EMS1)
# --------------------------------------------------------------------------

# Fan-curve registers.  Six temperature thresholds and seven fan-speed
# levels: the fan spins at speeds[0] until the temperature passes temps[0],
# then speeds[1] … speeds[6].  Confirmed byte-for-byte against the EC dump
# published in YoyPa/isw issue #220 (GS66-10SE).
CURVE_REGS = {
    "cpu": {"temps": list(range(0x6A, 0x70)), "speeds": list(range(0x72, 0x79))},
    "gpu": {"temps": list(range(0x82, 0x88)), "speeds": list(range(0x8A, 0x91))},
}

# Tachometer: two bytes, big-endian period count.  isw derives RPM as
# 478000 // period — reproduced here verbatim because a wrong constant
# silently prints plausible-looking nonsense.
RPM_NUMERATOR = 478000
RPM_REGS = {"cpu": 0xCC, "gpu": 0xCA}

N_TEMPS = 6
N_SPEEDS = 7

# --------------------------------------------------------------------------
# Graphics mode switch (the "MUX")
#
# The G1_3 firmware can run the panel off the discrete GPU alone, or off the
# Intel iGPU with the dGPU as a render offload (Optimus).  Three places encode
# the same single fact, and all three agree on this machine:
#
#   * EC register 0x2e, bit 6 — MSI's "PXCT" bit.  1 = the panel is wired to
#     the dGPU (discrete); 0 = the iGPU drives the panel (hybrid).
#   * the UEFI variable MsiDCVarData, byte 1 — the same polarity, read by the
#     firmware at POST, so it is what decides the *next* boot.
#   * whether an Intel PCI display controller exists at all — the firmware
#     powers the iGPU down in discrete mode, so its absence is the only
#     observation of what the machine is *actually* doing right now.
#
# The mechanism is cross-checked against ElXreno/msi-gpu-switcher, which drives
# the identical trio on MSI laptops; msi-ec itself has no MUX support at all
# (its G1_3 config lists webcam/fn_win/cooler_boost/shift/fan/cpu/gpu/leds and
# nothing else), so these addresses are the only route to it.
#
# ⚠️ 0x2e bit 6, 0xd1 and that one UEFI byte are the *only* addresses outside
# the documented setting map this program will ever write, and a graphics
# switch is the only setting here that cannot be taken back without a reboot.
# Everything else stays behind AllowRaw.  See msi_controld.Service.set_graphics().
MUX_EC_ADDR = 0x2E
MUX_EC_MASK = 0x40             # bit 6: 1 = discrete, 0 = hybrid
MUX_TRIGGER_ADDR = 0xD1
MUX_TRIGGER_MASK = 0x03        # bit 0 = "apply the requested mode at POST"
MUX_TRIGGER_ARM = 0x01

MUX_VAR_NAME = "MsiDCVarData"
MUX_VAR_GUID = "dd96baaf-145e-4f56-b1cf-193256298e99"
MUX_VAR_BASENAME = f"{MUX_VAR_NAME}-{MUX_VAR_GUID}"
MUX_VAR_SKIP = 4               # efivarfs prefixes the payload with 4 attr bytes
MUX_VAR_MODE_BYTE = 1          # index of the mode byte inside the payload

MUX_HYBRID = "hybrid"
MUX_DISCRETE = "discrete"
MUX_MODES = (MUX_HYBRID, MUX_DISCRETE)
MUX_LABELS = {
    MUX_HYBRID: "Hybrid (Intel + NVIDIA)",
    MUX_DISCRETE: "Discrete (NVIDIA only)",
}

# --------------------------------------------------------------------------
# Temperature bands — ONE definition, shared by the CLI and the GUI.
#
# They used to live in both places and disagreed (the CLI called 60 °C "warm",
# the GUI called the same reading "cool"), so a colour meant different things
# depending on which front-end you were looking at.
# --------------------------------------------------------------------------

TEMP_OK_MAX = 50      # below this: cool
TEMP_WARM_MAX = 70    # below this: warm
TEMP_HOT_MAX = 85     # below this: hot, at/above: critical

# Stable band names, so a caller can pick its own colour if it needs to.
BAND_COOL, BAND_WARM, BAND_HOT, BAND_CRIT = "cool", "warm", "hot", "critical"


def temp_band(temp) -> str | None:
    """Band name for a °C reading, or None when there is no reading."""
    if temp is None:
        return None
    if temp < TEMP_OK_MAX:
        return BAND_COOL
    if temp < TEMP_WARM_MAX:
        return BAND_WARM
    if temp < TEMP_HOT_MAX:
        return BAND_HOT
    return BAND_CRIT


# --------------------------------------------------------------------------
# Human-readable setting names — used by every "what changed" summary.
# --------------------------------------------------------------------------

SETTING_LABELS = {
    "shift": "performance",
    "fan": "fan mode",
    "cooler_boost": "cooler boost",
    "charge_end": "charge limit",
    "webcam": "webcam",
    "webcam_block": "webcam block",
    "super_battery": "super battery",
    "fn_key": "Fn key",
    "win_key": "Win key",
}

# The activity form reads as a sentence, so it wants its own labels: the
# diff's "performance" is fine as "performance eco -> sport", but as a verb
# phrase "Set performance to sport" is missing the word that tells you it is
# a *mode*.  Only the keys whose diff label reads awkwardly as a verb
# subject need overriding — the rest carry over unchanged.
ACTIVITY_LABELS = {
    "shift": "performance mode",
}


def format_setting(key: str, value) -> str:
    """Render one setting value the way a human would say it."""
    if value is None:
        return "unset"
    if key == "charge_end":
        return f"{value}%"
    if key == "cooler_boost":
        return "on" if value in (True, "on") else "off"
    return str(value)


def describe_change(key: str, was, now) -> str:
    """`"charge limit 60% -> 80%"`, or `"fan mode already auto"`.

    Both front-ends render an applied change through this one function, so
    the CLI and the GUI can never describe the same event differently.
    """
    label = SETTING_LABELS.get(key, key)
    if was == now:
        return f"{label} already {format_setting(key, now)}"
    return (f"{label} {format_setting(key, was)}"
            f" -> {format_setting(key, now)}")


def describe_activity(key: str, was, now) -> str:
    """`"Set charge limit to 80%"`, `"Enabled cooler boost"`.

    `describe_change` writes a *diff* — the right shape for a log line or a
    `--json` consumer, because it says exactly what moved.  A notification
    needs the other shape: the sentence describing what the person just
    *did*.  "charge limit 60% -> 80%" answers "what changed on the machine";
    "Set charge limit to 80%" answers "what did I just do", and the second is
    what a status bar or a balloon should say.

    Both are built by the daemon from the same before/after EC read-back, so
    neither can ever describe an intention the hardware did not honour.
    """
    label = ACTIVITY_LABELS.get(key, SETTING_LABELS.get(key, key))
    if was == now:
        return f"{label.capitalize()} already {format_setting(key, now)}"
    if key == "cooler_boost":
        return ("Enabled" if now in (True, "on") else "Disabled") + \
            f" {label}"
    return f"Set {label} to {format_setting(key, now)}"


def diff_settings(before: dict, after: dict) -> list:
    """Change summaries for every key whose value moved, in a stable order."""
    out = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            out.append(describe_change(key, before.get(key), after.get(key)))
    return out


def curve_activity(which: str, moved: int) -> str:
    """`"Set CPU fan curve (3 points)"` / `"CPU fan curve unchanged"`."""
    name = which.upper()
    if moved:
        return (f"Set {name} fan curve "
                f"({moved} point{'s' if moved != 1 else ''})")
    return f"{name} fan curve unchanged"


def curve_points_changed(before: dict, after: dict) -> int:
    """How many of the 13 EC bytes in a curve a write actually moved."""
    n = 0
    for field in ("temps", "speeds"):
        b = before.get(field) or []
        a = after.get(field) or []
        n += sum(1 for x, y in zip(b, a) if x != y)
        n += abs(len(a) - len(b))
    return n


def graphics_activity(mode: str, was: str | None, now: str | None) -> str:
    """`"Switched graphics to Hybrid (Intel + NVIDIA) — reboot to apply"`.

    The graphics mode is the one setting where "done" is a lie until the
    machine restarts, so the sentence says both what was requested and what is
    still outstanding.  A no-op says so instead of implying a switch.
    """
    label = MUX_LABELS.get(mode, mode)
    if now == mode and was == mode:
        return f"Graphics mode already {label}"
    if was == mode:
        return f"Graphics mode still {label} — reboot to apply"
    return f"Switched graphics to {label} — reboot to apply"

# The driver's charge-control byte at 0xef: end = raw - 0x80, start = end - 10.
CHARGE_MIN = 20
CHARGE_MAX = 100

# Ranges we are willing to write into EC fan-curve bytes.
TEMP_MIN, TEMP_MAX = 10, 100
SPEED_MIN, SPEED_MAX = 0, 100

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

# Built-in profiles.  Curves are sane defaults derived from isw's MSI
# section; "factory" is not here — it is captured from the EC on first run
# so the original firmware curve can always be restored.
#
# ⚠️ A profile carries a `charge_end` ONLY when setting a charge limit is the
# point of that profile.  "silent" / "balanced" / "gaming" deliberately omit
# it, so applying one changes nothing about charging — because the limit is
# the one setting a person chooses for *battery health* rather than for
# performance, and silently pushing it back to 100% (which these three used
# to do) threw away a limit the user had deliberately picked.  The journal
# showed it happening as `applied profile balanced: charge limit 60% -> 100%`.
# The rule is uniform and worth keeping: a profile that does not mention a
# charge limit does not touch one.  "battery" is the exception because
# preserving the battery IS that profile's purpose.
BUILTIN_PROFILES = {
    "silent": {
        "shift": "eco",
        "fan": "silent",
        "cooler_boost": False,
    },
    "balanced": {
        "shift": "comfort",
        "fan": "auto",
        "cooler_boost": False,
    },
    "gaming": {
        "shift": "sport",
        "fan": "advanced",
        "cooler_boost": False,
        "curve": {
            "cpu": {"temps": [58, 63, 68, 73, 78, 83],
                    "speeds": [0, 50, 60, 65, 70, 80, 85]},
            "gpu": {"temps": [53, 58, 63, 68, 73, 83],
                    "speeds": [0, 55, 65, 70, 80, 85, 90]},
        },
    },
    "battery": {
        "shift": "eco",
        "fan": "auto",
        "cooler_boost": False,
        "charge_end": 60,
    },
}

DEFAULT_CONFIG = {
    # Re-apply the last known state when the daemon starts (boot), so fan
    # mode / shift mode / charge limit survive reboots.
    "RestoreLast": True,
    # Explicit profile to apply at boot; wins over RestoreLast when set.
    "DefaultProfile": None,
    # Expose the raw EC peek/poke (`msi-control raw`).  Off by default:
    # an unvalidated EC byte is the one way this program can hurt you.
    "AllowRaw": False,
    # Allow the graphics-mode (MUX) switch.  Off by default for a different
    # reason than AllowRaw: the switch *is* validated, but it changes what the
    # firmware does at the next POST, and on a model whose MUX has not been
    # reverse engineered that can leave the machine with no display at all.
    # Requiring an edit here makes it a deliberate act, never a stray click.
    "AllowMuxSwitch": False,
    # Group allowed to talk to the daemon socket.  "wheel" = admin group,
    # so socket access grants nothing its holder could not already get.
    "SocketGroup": "wheel",
    # Re-apply state after resume from suspend (firmware sometimes resets
    # the EC on S3).
    "ReapplyOnResume": True,
}


# --------------------------------------------------------------------------
# Validation — the daemon never trusts a client, and the GUI is a client.
# --------------------------------------------------------------------------

class ValidationError(ValueError):
    """A client-supplied value was rejected.  Message is safe to echo back."""


def check_profile_name(name) -> str:
    if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
        raise ValidationError(
            "invalid profile name (want 1-32 chars of [A-Za-z0-9_-])")
    return name


def check_choice(value, choices, what: str):
    if value not in choices:
        raise ValidationError(
            f"invalid {what}: {value!r} (want one of {sorted(choices)})")
    return value


def check_int(value, lo: int, hi: int, what: str) -> int:
    # bool is an int in Python — and "true" must never become speed 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or isinstance(value, float) and value != int(value):
        raise ValidationError(f"invalid {what}: not an integer")
    value = int(value)
    if not (lo <= value <= hi):
        raise ValidationError(f"invalid {what}: {value} (want {lo}..{hi})")
    return value


def check_charge_end(value) -> int:
    return check_int(value, CHARGE_MIN, CHARGE_MAX, "charge limit")


def check_mux_mode(value) -> str:
    return check_choice(value, MUX_MODES, "graphics mode")


def check_curve(temps, speeds):
    """Validate one fan curve.

    Rules the EC requires (and a wrong curve is the one setting that can
    cook the machine, so this is deliberately strict):
      * exactly 6 temperatures, strictly increasing, inside TEMP_MIN..MAX
      * exactly 7 speeds, non-decreasing, inside SPEED_MIN..MAX
    """
    if not isinstance(temps, list) or not isinstance(speeds, list):
        raise ValidationError("curve must be {temps:[...], speeds:[...]}")
    if len(temps) != N_TEMPS:
        raise ValidationError(f"want {N_TEMPS} temperature points, got {len(temps)}")
    if len(speeds) != N_SPEEDS:
        raise ValidationError(f"want {N_SPEEDS} fan speeds, got {len(speeds)}")

    temps = [check_int(t, TEMP_MIN, TEMP_MAX, "curve temperature")
             for t in temps]
    speeds = [check_int(s, SPEED_MIN, SPEED_MAX, "curve fan speed")
              for s in speeds]

    for a, b in zip(temps, temps[1:]):
        if b <= a:
            raise ValidationError("curve temperatures must strictly increase")
    for a, b in zip(speeds, speeds[1:]):
        if b < a:
            raise ValidationError("curve fan speeds must not decrease")

    return temps, speeds


def curve_speed_at(temps, speeds, temp: float) -> int:
    """Effective fan speed the EC will choose for `temp` on this curve."""
    speed = speeds[0]
    for threshold, level in zip(temps, speeds[1:]):
        if temp > threshold:
            speed = level
    return speed


# --------------------------------------------------------------------------
# Wire protocol: one JSON object per line, one request per connection.
# --------------------------------------------------------------------------

class ProtocolError(RuntimeError):
    pass


def send_cmd(cmd: dict, timeout: float = 5.0) -> dict:
    """Talk to msi-controld.  Returns the response dict; raises on transport error."""
    payload = (json.dumps(cmd) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path())
        s.sendall(payload)
        with s.makefile("r", encoding="utf-8") as fh:
            line = fh.readline()
    if not line:
        raise ProtocolError("daemon closed the connection without answering")
    try:
        resp = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"daemon sent a bad response: {e}") from e
    if not isinstance(resp, dict):
        raise ProtocolError("daemon response is not an object")
    return resp


def call(cmd: dict, timeout: float = 5.0) -> dict:
    """Like send_cmd, but raises ProtocolError on ok:false too."""
    resp = send_cmd(cmd, timeout=timeout)
    if not resp.get("ok"):
        raise ProtocolError(resp.get("error", "unknown daemon error"))
    return resp.get("data", {})


def daemon_available() -> bool:
    try:
        send_cmd({"cmd": "ping"}, timeout=1.0)
        return True
    except (OSError, ProtocolError):
        return False


# --------------------------------------------------------------------------
# Local (unprivileged) reads — enough for a degraded `status` without the
# daemon.  Anything that needs an EC *write* (RPM tach, fan curve) is only
# reachable through the daemon.
# --------------------------------------------------------------------------

def _read(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read().strip()


def _read_opt(path: str):
    try:
        return _read(path)
    except OSError:
        return None


def _read_int(path: str):
    text = _read_opt(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def find_battery() -> str | None:
    """BAT0/BAT1/… — this machine uses BAT1, others use BAT0."""
    bats = sorted(glob.glob(os.path.join(power_dir(), "BAT*")))
    return bats[0] if bats else None


def _lines(text) -> list:
    if not text:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def local_status(mux: bool = True) -> dict:
    """Best-effort state straight from sysfs — no daemon, no root.

    Missing pieces (RPM, fan curve) come back as None so the UI can say
    "daemon offline" instead of printing a wrong number.  `mux=False` skips
    the graphics block: the daemon computes that itself from the EC and would
    otherwise pay for the PCI scan twice on every poll.
    """
    ec = msi_ec_dir()
    bat = find_battery()

    shift_avail = _lines(_read_opt(os.path.join(ec, "available_shift_modes")))
    fan_avail = _lines(_read_opt(os.path.join(ec, "available_fan_modes")))

    state = {
        "driver": {
            "present": os.path.isdir(ec),
            "debug": os.path.isdir(os.path.join(ec, "debug")),
            "fw": _read_opt(os.path.join(ec, "fw_version")),
            "fw_date": _read_opt(os.path.join(ec, "fw_release_date")),
        },
        "shift": {
            "current": _read_opt(os.path.join(ec, "shift_mode")),
            "available": shift_avail,
        },
        "fan": {
            "current": _read_opt(os.path.join(ec, "fan_mode")),
            "available": fan_avail,
        },
        "cooler_boost": _read_opt(os.path.join(ec, "cooler_boost")),
        "webcam": _read_opt(os.path.join(ec, "webcam")),
        "webcam_block": _read_opt(os.path.join(ec, "webcam_block")),
        "fn_key": _read_opt(os.path.join(ec, "fn_key")),
        "win_key": _read_opt(os.path.join(ec, "win_key")),
        "super_battery": _read_opt(os.path.join(ec, "super_battery")),
        "cpu": {
            "temp": _read_int(os.path.join(ec, "cpu", "realtime_temperature")),
            "fan_pct": _read_int(os.path.join(ec, "cpu", "realtime_fan_speed")),
            "rpm": None,
        },
        "gpu": {
            "temp": _read_int(os.path.join(ec, "gpu", "realtime_temperature")),
            "fan_pct": _read_int(os.path.join(ec, "gpu", "realtime_fan_speed")),
            "rpm": None,
        },
        "battery": _battery_status(bat),
        "graphics": local_graphics() if mux else None,
        "curve": None,          # needs an EC write (tach/ec_get) → daemon only
        "partial": True,        # tells the UI this view is degraded
    }
    return state


def _battery_status(bat: str | None) -> dict | None:
    if not bat:
        return None
    pct = _read_int(os.path.join(bat, "capacity"))
    return {
        "name": os.path.basename(bat),
        "present": _read_opt(os.path.join(bat, "present")) not in (None, "0"),
        "status": _read_opt(os.path.join(bat, "status")),
        "pct": pct,
        "energy_now": _read_int(os.path.join(bat, "energy_now")),
        "energy_full": _read_int(os.path.join(bat, "energy_full")),
        "energy_full_design": _read_int(os.path.join(bat, "energy_full_design")),
        "charge_start": _read_int(os.path.join(bat, "charge_control_start_threshold")),
        "charge_end": _read_int(os.path.join(bat, "charge_control_end_threshold")),
        "ac_online": _ac_online(),
    }


def _ac_online():
    for adp in sorted(glob.glob(os.path.join(power_dir(), "ADP*"))) + \
               sorted(glob.glob(os.path.join(power_dir(), "AC*"))):
        val = _read_opt(os.path.join(adp, "online"))
        if val is not None:
            return val == "1"
    return None


# --------------------------------------------------------------------------
# Graphics mode (MUX) — reading it without root
#
# The daemon reads the EC state bit with ec_read (two sysfs ops).  A client
# without the daemon cannot: msi-ec's debug/ec_get needs the address written
# to it first, and that write is root-only.  So the unprivileged view is built
# from the two sources that *are* world-readable — the UEFI variable and which
# PCI display controllers exist — and the EC bit is reported as unknown rather
# than guessed.  Deliberately NOT read here: debug/ec_dump, which is also
# world-readable but costs ~120 ms because the driver walks all 256 registers
# to build it.  That is far too expensive for a 1.5 s poll.
# --------------------------------------------------------------------------

def efivars_dir() -> str:
    return os.path.join(sysfs_root(), "firmware", "efi", "efivars")


def mux_var_path() -> str:
    return os.path.join(efivars_dir(), MUX_VAR_BASENAME)


def ec_dump_path() -> str:
    return os.path.join(msi_ec_dir(), "debug", "ec_dump")


_EC_DUMP_ROW = re.compile(r"^\|\s*0x([0-9a-f])_\s*\|(.*)\|$")
_EC_DUMP_BYTE = re.compile(r"\b([0-9a-f]{2})\b")


def parse_ec_dump(text: str) -> dict:
    """The driver's `debug/ec_dump` as `{address: byte}`.

    It is a formatted table (`| 0x2_ | xx xx … |`), so addresses come from the
    row label and never from counting bytes across the whole file — a header
    line or a width change then shifts nothing.
    """
    out = {}
    for line in (text or "").splitlines():
        m = _EC_DUMP_ROW.match(line.strip())
        if not m:
            continue
        base = int(m.group(1), 16) << 4
        for i, byte in enumerate(_EC_DUMP_BYTE.findall(m.group(2))):
            if base + i <= 0xFF:
                out[base + i] = int(byte, 16)
    return out


def read_ec_dump() -> dict | None:
    try:
        with open(ec_dump_path(), "r", encoding="utf-8", errors="replace") as fh:
            return parse_ec_dump(fh.read())
    except OSError:
        return None


def mux_from_ec_byte(val) -> str | None:
    """Mode the EC state bit claims, or None when it could not be read."""
    if val is None:
        return None
    return MUX_DISCRETE if val & MUX_EC_MASK else MUX_HYBRID


def parse_mux_var(raw) -> tuple:
    """`(attributes, payload)` from the raw bytes of the efivarfs file."""
    if not raw or len(raw) <= MUX_VAR_SKIP:
        return None, b""
    return int.from_bytes(raw[:MUX_VAR_SKIP], "little"), raw[MUX_VAR_SKIP:]


def mux_from_var(raw) -> str | None:
    """Mode the UEFI variable requests, or None when it is absent/short."""
    _attrs, data = parse_mux_var(raw)
    if len(data) <= MUX_VAR_MODE_BYTE:
        return None
    return MUX_DISCRETE if data[MUX_VAR_MODE_BYTE] else MUX_HYBRID


def igpu_present() -> bool | None:
    """Is an Intel PCI display controller present?  None if we cannot tell.

    In discrete mode the firmware powers the iGPU down, so its *absence* is
    the one observation of what the running machine is actually doing — as
    opposed to what the EC and the UEFI variable say it should do.
    """
    root = os.path.join(sysfs_root(), "bus", "pci", "devices")
    try:
        names = os.listdir(root)
    except OSError:
        return None
    for name in names:
        if _read_opt(os.path.join(root, name, "vendor")) != "0x8086":
            continue
        klass = _read_opt(os.path.join(root, name, "class")) or ""
        if klass.startswith("0x03"):        # 0x03xxxx = display controller
            return True
    return False


def local_graphics() -> dict:
    """Best-effort graphics-mode view without the daemon.

    Everything here is world-readable, so the CLI's degraded view and the GUI
    with the daemon down can still state the mode instead of showing a dash.
    `ec_bit` is None because reading one EC register needs root.
    """
    raw = None
    try:
        with open(mux_var_path(), "rb") as fh:
            raw = fh.read()
    except OSError:
        pass
    igpu = igpu_present()
    var_mode = mux_from_var(raw)
    effective = None
    if igpu is not None:
        effective = MUX_HYBRID if igpu else MUX_DISCRETE
    requested = var_mode if var_mode is not None else effective
    return {
        "supported": raw is not None or igpu is not None,
        "enabled": False,           # only the daemon knows the config
        "effective": effective,
        "requested": requested,
        "reboot_required": bool(effective and requested
                                and effective != requested),
        "ec_bit": None,
        "ec_mode": None,
        "var_mode": var_mode,
        "var_present": raw is not None,
        "var_path": mux_var_path(),
        "igpu_present": igpu,
        "backup": None,
    }


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            cfg.update(user)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        # A broken config must never stop the daemon from starting; the
        # defaults are safe.
        pass
    return cfg
