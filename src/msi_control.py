#!/usr/bin/env python3
"""msi_control - shared core: model registry, registers, paths, protocol.

Everything the daemon, the CLI and the GUI have to agree about lives here:
the paths, the wire protocol, the validation, and the **model registry** -
one JSON file per laptop model under `models/`, holding the EC addresses,
feature flags and built-in profiles that differ between MSI machines.

A model is chosen once, at import.  MSI_MODEL forces one by id (the tests
use that); otherwise every `models/*/model.json` is scored against this
machine's DMI identity and EC firmware.  Matching nothing is a working state
rather than a broken one: the *unsupported* model still reads and writes
everything the msi-ec driver exposes by itself (temperatures, shift and fan
modes, cooler boost, the charge threshold) and refuses what needs
model-specific register knowledge (fan curves, RPM, the graphics switch)
instead of guessing at it.  `msi-control detect` prints the report a
contributor needs in order to add their model to the registry.

Everything here is stdlib-only on purpose: the daemon runs as root and the
CLI must work in a recovery shell.
"""

from __future__ import annotations

import glob
import json
import os
import re
import socket

# --------------------------------------------------------------------------
# Version
#
# One number, in the shared core, so the CLI, the daemon, the GUI and a bug
# report can never disagree about which build is running.  Follows the
# usual `MAJOR.MINOR.PATCH` shape; bump PATCH for a fix, MINOR for a feature,
# MAJOR for a change that breaks an installed config or protocol.
# --------------------------------------------------------------------------

VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Paths - every one overridable through the environment so the whole stack
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


def dmi_dir() -> str:
    return os.path.join(sysfs_root(), "class", "dmi", "id")


def models_dir() -> str:
    """Where `models/<id>/model.json` live.

    Source tree: `<repo>/models`, since this file is `<repo>/src/...`.
    Installed: `/usr/local/lib/msi-control/models`, beside this module.  The
    first candidate that exists wins; `MSI_MODELS` overrides both, which is
    how the tests point at a throwaway registry and how a contributor can
    try out a model without installing it.
    """
    override = os.environ.get("MSI_MODELS")
    if override:
        return override
    here = os.path.dirname(os.path.realpath(__file__))
    candidates = [os.path.join(here, "models"),
                  os.path.join(here, os.pardir, "models"),
                  "/usr/local/lib/msi-control/models",
                  "/usr/lib/msi-control/models"]
    for cand in candidates:
        if os.path.isdir(cand):
            return os.path.realpath(cand)
    # Nothing on disk yet: name the source-tree location so an error message
    # points at the directory a contributor would create.
    return os.path.realpath(candidates[1])


# --------------------------------------------------------------------------
# The model registry
#
# This is what makes the program about MSI laptops in general instead of
# about one of them.  A model file describes only the parts that differ
# between machines:
#
#   match      how a machine is recognised (DMI + EC firmware globs)
#   ec         fan-curve and tachometer addresses, the RPM constant
#   charge     the charge-threshold bounds the firmware accepts
#   features   what this firmware can actually do
#   profiles   this model's built-in profiles (curves are model-specific)
#
# Shift modes, fan modes, cooler boost and the charge threshold are NOT here,
# because the msi-ec driver exposes them through sysfs on every machine it
# binds to - they are model-independent facts, and the driver's own
# `available_*_modes` attributes say which values a given firmware accepts.
#
# A model file is validated when it is loaded, not when it is used: a typo in
# a register address is then a message naming the file and the field, rather
# than a wrong number written into the embedded controller.
# --------------------------------------------------------------------------

MODEL_SCHEMA = 1

# Feature flags a model may declare.  The first four are driver-provided and
# are additionally checked at runtime against the attributes that exist; the
# next three need raw EC knowledge and are refused outright when false; the
# last five are optional driver features that only some firmwares have.
MODEL_FEATURES = (
    "shift", "fan", "cooler_boost", "charge_limit",
    "fan_curve", "rpm", "mux",
    "webcam", "webcam_block", "super_battery", "fn_key", "win_key",
)

# What a machine can do when nobody has written a model file for it: exactly
# what the driver exposes by itself.  Deliberately not optimistic - every
# `False` here is a control the user would otherwise be offered and then
# refused, and one of them (mux) can leave a laptop with no display.
UNKNOWN_FEATURES = {
    "shift": True, "fan": True, "cooler_boost": True, "charge_limit": True,
    "fan_curve": False, "rpm": False, "mux": False,
    "webcam": False, "webcam_block": False, "super_battery": False,
    "fn_key": False, "win_key": False,
}

UNKNOWN_ID = "unknown"

CHARGE_MIN_DEFAULT = 20
CHARGE_MAX_DEFAULT = 100

# Fields of `match` and how much each one is worth when it hits.  The DMI
# product name is the strongest signal; the EC firmware is weaker because
# firmwares are shared across a model family, and it is what saves a machine
# whose DMI string the vendor changed between BIOS revisions.
MATCH_WEIGHTS = (("product_name", 50), ("board_name", 40), ("ec_firmware", 30))


class ModelError(RuntimeError):
    """A model file is malformed.  The message names the file and the field."""


def _glob_match(pattern, value) -> bool:
    """Case-insensitive glob, anchored at both ends (`16V1EMS1.*`)."""
    if not isinstance(pattern, str) or not isinstance(value, str):
        return False
    return re.fullmatch(pattern.strip(), value.strip(), re.IGNORECASE) is not None


def _as_addr(value, what: str = "EC address") -> int:
    """An EC address as an int.  Accepts 106, "106" and "0x6a".

    Addresses are written in hex in the model files because that is how the
    EC documentation, the runbook and the driver's own `ec_get` all speak;
    reading them back as ints is the one place that formatting stops.
    """
    if isinstance(value, bool):
        raise ModelError(f"{what}: {value!r} is not an address")
    if isinstance(value, int):
        addr = value
    elif isinstance(value, str):
        text = value.strip().lower()
        try:
            addr = int(text, 16) if text.startswith("0x") else int(text, 10)
        except ValueError:
            raise ModelError(
                f"{what}: {value!r} is not an address "
                "(use hex like \"0x6a\", or a decimal integer)") from None
    else:
        raise ModelError(f"{what}: {value!r} is not an address")
    if not 0 <= addr <= 0xFF:
        raise ModelError(f"{what}: {value!r} is outside 0x00..0xff")
    return addr


def _as_int(value, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelError(f"{what}: {value!r} is not an integer")
    if not lo <= value <= hi:
        raise ModelError(f"{what}: {value} is outside {lo}..{hi}")
    return value


def _as_text(value, what: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ModelError(f"missing required field {what!r}")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"{what} must be a non-empty string")
    return value.strip()


class Model:
    """One laptop model: its EC addresses, features and built-in profiles."""

    def __init__(self, data: dict, source: str | None = None):
        if not isinstance(data, dict):
            raise ModelError("a model file must contain a JSON object")
        self.source = source
        self.where = source or "<built-in>"

        schema = data.get("schema", MODEL_SCHEMA)
        if schema != MODEL_SCHEMA:
            raise ModelError(
                f"unsupported model schema {schema!r} "
                f"(this build understands schema {MODEL_SCHEMA})")

        self.id = self._text(data, "id", required=True)
        if not PROFILE_NAME_RE.match(self.id):
            raise ModelError(
                f"id {self.id!r} must be lower-case letters, digits and "
                "hyphens (it is used as a directory name and a lookup key)")
        self.name = self._text(data, "name") or self.id
        self.vendor = self._text(data, "vendor") or \
            "Micro-Star International Co., Ltd."
        self.status = self._text(data, "status") or "stable"
        self.notes = self._text(data, "notes") or ""
        prov = data.get("provenance") or []
        self.provenance = [str(p) for p in prov] if isinstance(prov, list) \
            else [str(prov)]

        self.match = self._match(data.get("match") or {})

        ec = data.get("ec") or {}
        if not isinstance(ec, dict):
            raise ModelError("`ec` must be an object")
        self.curve_regs, self._n_temps, self._n_speeds = \
            self._curve(ec.get("curve") or {})
        self.rpm_regs, self.rpm_numerator = self._rpm(ec.get("rpm") or {})

        # `_features` cross-checks the flags against the registers, so it has
        # to run *after* the EC block above.
        self.features = dict(UNKNOWN_FEATURES)
        self._features(data.get("features") or {})

        charge = data.get("charge") or {}
        if not isinstance(charge, dict):
            raise ModelError("`charge` must be an object")
        self.charge_min = _as_int(charge.get("min", CHARGE_MIN_DEFAULT),
                                  "charge.min", 1, 100)
        self.charge_max = _as_int(charge.get("max", CHARGE_MAX_DEFAULT),
                                  "charge.max", 1, 100)
        if self.charge_min >= self.charge_max:
            raise ModelError(
                f"charge: min {self.charge_min} is not below max "
                f"{self.charge_max}")

        self.profiles = self._profiles(data.get("profiles") or {})

    # -- parsing helpers ---------------------------------------------------

    def _text(self, data, key, required=False) -> str | None:
        try:
            return _as_text(data.get(key), key, required)
        except ModelError as e:
            raise ModelError(f"{self.where}: {e}") from None

    def _match(self, match: dict) -> dict:
        if not isinstance(match, dict):
            raise ModelError(f"{self.where}: `match` must be an object")
        out = {}
        for field, _weight in MATCH_WEIGHTS:
            raw = match.get(field) or []
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list) or \
                    not all(isinstance(p, str) and p.strip() for p in raw):
                raise ModelError(
                    f"{self.where}: match.{field} must be a string or a list "
                    "of non-empty strings")
            out[field] = [p.strip() for p in raw]
        if not any(out.values()):
            raise ModelError(
                f"{self.where}: `match` names nothing, so this model could "
                "never be selected")
        return out

    def _features(self, given: dict) -> None:
        if not isinstance(given, dict):
            raise ModelError(f"{self.where}: `features` must be an object")
        for key, value in given.items():
            if key not in MODEL_FEATURES:
                raise ModelError(
                    f"{self.where}: unknown feature {key!r} "
                    f"(want one of {', '.join(MODEL_FEATURES)})")
            if not isinstance(value, bool):
                raise ModelError(
                    f"{self.where}: feature {key!r} must be true or false")
            self.features[key] = value
        if self.features["rpm"] and not self.rpm_regs:
            raise ModelError(
                f"{self.where}: \"rpm\": true but ec.rpm has no addresses")
        if self.features["fan_curve"] and not self.curve_regs:
            raise ModelError(
                f"{self.where}: \"fan_curve\": true but ec.curve has no "
                "registers")

    def _curve(self, curve) -> tuple:
        """`{"cpu": {...}}` from the file; also the curve *shape*."""
        if not curve:
            return {}, N_TEMPS_DEFAULT, N_SPEEDS_DEFAULT
        if not isinstance(curve, dict):
            raise ModelError(f"{self.where}: ec.curve must be an object")
        regs = {}
        shape = None
        for which, spec in curve.items():
            if which not in ("cpu", "gpu"):
                raise ModelError(
                    f"{self.where}: ec.curve has unknown fan {which!r} "
                    "(want cpu and/or gpu)")
            if not isinstance(spec, dict):
                raise ModelError(
                    f"{self.where}: ec.curve.{which} must be an object")
            temps = [_as_addr(a, f"ec.curve.{which}.temps")
                     for a in spec.get("temps") or []]
            speeds = [_as_addr(a, f"ec.curve.{which}.speeds")
                      for a in spec.get("speeds") or []]
            if len(speeds) != len(temps) + 1:
                raise ModelError(
                    f"{self.where}: ec.curve.{which} has {len(temps)} "
                    f"thresholds and {len(speeds)} speeds - a step function "
                    "always has one more speed than threshold")
            if not temps:
                raise ModelError(f"{self.where}: ec.curve.{which} is empty")
            if shape is None:
                shape = (len(temps), len(speeds))
            elif shape != (len(temps), len(speeds)):
                raise ModelError(
                    f"{self.where}: every fan's curve must have the same "
                    f"shape ({which} has {len(temps)}/{len(speeds)}, "
                    f"another fan has {shape[0]}/{shape[1]})")
            regs[which] = {"temps": temps, "speeds": speeds}
        n_temps, n_speeds = shape
        return regs, n_temps, n_speeds

    def _rpm(self, rpm) -> tuple:
        if not rpm:
            return {}, RPM_NUMERATOR_DEFAULT
        if not isinstance(rpm, dict):
            raise ModelError(f"{self.where}: ec.rpm must be an object")
        numerator = _as_int(rpm.get("numerator", RPM_NUMERATOR_DEFAULT),
                            "ec.rpm.numerator", 1, 10 ** 9)
        regs = {}
        for which in ("cpu", "gpu"):
            if which in rpm:
                regs[which] = _as_addr(rpm[which], f"ec.rpm.{which}")
        if not regs:
            raise ModelError(
                f"{self.where}: ec.rpm names no fan (want cpu and/or gpu)")
        return regs, numerator

    def _profiles(self, given: dict) -> dict:
        if not isinstance(given, dict):
            raise ModelError(f"{self.where}: `profiles` must be an object")
        out = {}
        for name, prof in given.items():
            if name == "factory":
                raise ModelError(
                    f"{self.where}: a built-in profile may not be called "
                    "\"factory\" - that name is reserved for the curve "
                    "captured from the EC on first run")
            if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
                raise ModelError(
                    f"{self.where}: bad profile name {name!r} "
                    "(want 1-32 chars of [A-Za-z0-9_-])")
            try:
                out[name] = clean_profile(prof, self.n_temps, self.n_speeds,
                                          self.charge_min, self.charge_max)
            except ValidationError as e:
                raise ModelError(
                    f"{self.where}: profile {name!r}: {e}") from None
        return out

    # -- what the rest of the program asks a model -------------------------

    @property
    def n_temps(self) -> int:
        return self._n_temps

    @property
    def n_speeds(self) -> int:
        return self._n_speeds

    @property
    def supported(self) -> bool:
        return self.id != UNKNOWN_ID

    def feature(self, name: str) -> bool:
        return bool(self.features.get(name, False))

    def summary(self) -> dict:
        """The model as the front-ends and `/api`-style callers want it."""
        return {
            "id": self.id,
            "name": self.name,
            "vendor": self.vendor,
            "status": self.status,
            "supported": self.supported,
            "source": self.source,
            "features": dict(self.features),
        }

    @classmethod
    def unknown(cls) -> "Model":
        return cls({"id": UNKNOWN_ID,
                    "name": "Unrecognised MSI laptop",
                    "status": "unknown",
                    "match": {"product_name": ["*"]}})

    def __repr__(self) -> str:
        return f"<Model {self.id} from {self.where}>"



# --------------------------------------------------------------------------
# Loading and choosing a model
# --------------------------------------------------------------------------

# Shape defaults, used only when a model declares no curve at all (in which
# case `features.fan_curve` must be false and nothing ever asks the shape).
N_TEMPS_DEFAULT, N_SPEEDS_DEFAULT = 6, 7
RPM_NUMERATOR_DEFAULT = 478000


def model_files(root: str | None = None) -> list:
    """Every `models/<id>/model.json`, sorted by id."""
    root = root or models_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(root, name, "model.json")
        if os.path.isfile(path):
            out.append((name, path))
    return out


def load_model_file(path: str) -> Model:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as e:
        raise ModelError(f"cannot read {path}: {e.strerror or e}") from e
    except json.JSONDecodeError as e:
        raise ModelError(f"{path} is not valid JSON: {e}") from e
    model = Model(data, source=path)
    # The directory name and the `id` are two ways of saying the same thing;
    # letting them disagree would make the registry depend on how it is read.
    want = os.path.basename(os.path.dirname(os.path.realpath(path)))
    if want != model.id:
        raise ModelError(
            f"{path}: id {model.id!r} does not match its directory {want!r}")
    return model


def load_models(root: str | None = None) -> tuple:
    """`(models, errors)` - a broken model file never stops the others.

    A malformed file is reported rather than raised, because the machine it
    breaks for is most likely *not* the machine its author is sitting at, and
    the person who can fix it is reading `msi-control detect` on theirs.
    """
    models, errors = [], []
    for _id, path in model_files(root):
        try:
            models.append(load_model_file(path))
        except ModelError as e:
            errors.append(str(e))
    return models, errors


def machine_facts() -> dict:
    """This machine's identity: DMI strings plus the EC firmware revision.

    All of it is world-readable, so `detect` works without root and works
    with the daemon down - which is exactly when somebody is most likely to
    be asking what this laptop is.
    """
    ec = msi_ec_dir()

    def dmi(field):
        return _read_opt(os.path.join(dmi_dir(), field))

    return {
        "sys_vendor": dmi("sys_vendor"),
        "product_name": dmi("product_name"),
        "product_version": dmi("product_version"),
        "board_vendor": dmi("board_vendor"),
        "board_name": dmi("board_name"),
        "board_version": dmi("board_version"),
        "bios_version": dmi("bios_version"),
        "ec_firmware": _read_opt(os.path.join(ec, "fw_version")),
        "ec_firmware_date": _read_opt(os.path.join(ec, "fw_release_date")),
    }


def match_score(model: Model, facts: dict) -> int:
    """How well `model` describes `facts`.  0 means "not this machine"."""
    score = 0
    for field, weight in MATCH_WEIGHTS:
        value = facts.get(field)
        if value and any(_glob_match(p, value)
                         for p in model.match.get(field, [])):
            score += weight
    return score


def select_model(hint: str | None = None, root: str | None = None) -> Model:
    """Pick this machine's model, or the unsupported one.

    `hint` (or MSI_MODEL) forces an id: an *explicit* request for a model
    that is not there is an error, because silently falling back would make a
    typo look like a machine the registry has never heard of.
    """
    models, errors = load_models(root)
    hint = (hint if hint is not None
            else os.environ.get("MSI_MODEL", "")).strip()
    if hint:
        for model in models:
            if model.id == hint:
                return model
        known = ", ".join(m.id for m in models) or \
            "none found in " + (root or models_dir())
        raise ModelError(
            f"MSI_MODEL={hint!r} is not in the registry (known models: "
            f"{known})" + ("\n  " + "\n  ".join(errors) if errors else ""))
    facts = machine_facts()
    best, best_score = None, 0
    for model in sorted(models, key=lambda m: m.id):
        score = match_score(model, facts)
        if score > best_score:
            best, best_score = model, score
    return best if best is not None else Model.unknown()


# --------------------------------------------------------------------------
# What the contributor-facing `detect` command reports
# --------------------------------------------------------------------------

# The sysfs attributes of the msi-ec driver this program cares about.
# `detect` lists which of them a machine actually has, because absence is the
# only reliable statement a firmware makes about a feature it lacks.
DRIVER_ATTRS = ("shift_mode", "fan_mode", "cooler_boost", "webcam",
                "webcam_block", "super_battery", "fn_key", "win_key",
                "fw_version", "fw_release_date", "cpu", "gpu", "debug")


def driver_facts() -> dict:
    """What the msi-ec driver exposes here - no root, no daemon."""
    ec = msi_ec_dir()
    bat = find_battery()
    present = [a for a in DRIVER_ATTRS if os.path.exists(os.path.join(ec, a))]
    return {
        "present": os.path.isdir(ec),
        "debug": os.path.isdir(os.path.join(ec, "debug")),
        "attributes": present,
        "missing_attributes": [a for a in DRIVER_ATTRS if a not in present],
        "shift_modes": _lines(_read_opt(os.path.join(
            ec, "available_shift_modes"))),
        "fan_modes": _lines(_read_opt(os.path.join(ec, "available_fan_modes"))),
        "cpu": {"temp": _read_int(os.path.join(
                    ec, "cpu", "realtime_temperature")),
                "fan_pct": _read_int(os.path.join(
                    ec, "cpu", "realtime_fan_speed"))},
        "gpu": {"temp": _read_int(os.path.join(
                    ec, "gpu", "realtime_temperature")),
                "fan_pct": _read_int(os.path.join(
                    ec, "gpu", "realtime_fan_speed"))},
        "battery": bool(bat),
        "charge_limit": bool(bat and os.path.exists(os.path.join(
            bat, "charge_control_end_threshold"))),
    }


def detect(include_dump: bool = True) -> dict:
    """Everything needed to tell whether a model exists, and to write one."""
    models, errors = load_models()
    facts = machine_facts()
    scored = sorted(((match_score(m, facts), m) for m in models),
                    key=lambda pair: (-pair[0], pair[1].id))
    matched = next(({"id": m.id, "name": m.name, "score": s}
                    for s, m in scored if s > 0), None)
    report = {
        "machine": facts,
        "driver": driver_facts(),
        "registry": {"dir": models_dir(),
                     "models": [m.id for m in models],
                     "errors": errors},
        "matched": matched,
        "candidates": [{"id": m.id, "name": m.name, "score": s}
                       for s, m in scored],
    }
    if include_dump:
        dump = read_ec_dump()
        report["ec_dump"] = (
            {"readable": True,
             "bytes": {f"{a:02x}": f"{v:02x}" for a, v in sorted(dump.items())}}
            if dump is not None else {"readable": False})
    return report


def render_detect(report: dict) -> str:
    """The report as plain text - the block a contributor pastes into a PR."""
    m, d = report["machine"], report["driver"]
    add = []
    add.append("MSI Linux Control - model detection")
    add.append("=" * 38)
    add.append("")
    add.append("machine")
    for key in ("sys_vendor", "product_name", "product_version", "board_name",
                "board_version", "bios_version"):
        add.append(f"  {key:<16} {m.get(key) or '-'}")
    add.append(f"  {'ec_firmware':<16} {m.get('ec_firmware') or '-'}"
               f"  {m.get('ec_firmware_date') or ''}".rstrip())
    add.append("")
    add.append("driver (msi-ec)")
    add.append(f"  {'present':<16} {'yes' if d['present'] else 'NO'}"
               f"   debug interface: {'yes' if d['debug'] else 'no'}")
    add.append(f"  {'shift modes':<16} {', '.join(d['shift_modes']) or '-'}")
    add.append(f"  {'fan modes':<16} {', '.join(d['fan_modes']) or '-'}")
    add.append(f"  {'attributes':<16} {', '.join(d['attributes']) or '-'}")
    if d["missing_attributes"]:
        add.append(f"  {'absent':<16} {', '.join(d['missing_attributes'])}")
    add.append("")
    add.append("registry")
    add.append(f"  directory       {report['registry']['dir']}")
    add.append(f"  models          "
               f"{', '.join(report['registry']['models']) or 'none'}")
    for err in report["registry"]["errors"]:
        add.append(f"  BROKEN          {err}")
    add.append("")
    if report["matched"]:
        who = report["matched"]
        add.append(f"matched: {who['name']}  ({who['id']}, score "
                   f"{who['score']})")
    else:
        add.append("matched: NOTHING - this laptop has no model file yet.")
        add.append("")
        add.append("  Everything the msi-ec driver exposes already works:")
        add.append("  temperatures, shift and fan modes, cooler boost and the")
        add.append("  charge limit.  Fan curves, RPM and the graphics switch")
        add.append("  need this model's registers.  See CONTRIBUTING.md ->")
        add.append("  \"Adding your model\", or run")
        add.append("      msi-control detect --write <dir>")
        add.append("  for a model.json to fill in.")
    if report["candidates"]:
        add.append("")
        add.append("candidates")
        for cand in report["candidates"]:
            add.append(f"  {cand['score']:>3}  {cand['id']:<24} {cand['name']}")
    dump = report.get("ec_dump") or {}
    if dump:
        add.append("")
        if dump.get("readable"):
            add.append("EC dump (256 registers, from the driver's debug iface)")
            data = dump["bytes"]
            for row in range(0, 256, 16):
                cells = []
                for i in range(16):
                    val = data.get(f"{row + i:02x}")
                    cells.append(f"{val:02x}" if isinstance(val, int)
                                 else (val or "--"))
                add.append(f"  {row:02x}  " + " ".join(cells))
        else:
            add.append("EC dump: not readable (needs msi-ec with debug=1)")
    return "\n".join(add)


def model_id_from_name(name: str) -> str:
    """`"GS66 Stealth 10SE"` -> `"gs66-stealth-10se"`."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip().lower())
    return slug.strip("-") or "my-msi-laptop"


def firmware_glob(firmware: str) -> str:
    """`"16V1EMS1.118"` -> `"16V1EMS1.*"`.

    The trailing revision arrives with every firmware update, so a pattern
    that pinned it would stop working for every user who updated - which is
    the opposite of what a registry entry is for.
    """
    text = (firmware or "").strip()
    if not text:
        return ""
    head = text.rsplit(".", 1)[0] if "." in text else text
    return f"{head}.*" if head != text else text


def draft_model(facts: dict | None = None) -> dict:
    """A starting `model.json` for the machine we are running on.

    Deliberately *valid* and deliberately modest: it carries the match rules
    that are read from the hardware and claims no feature that would need a
    register nobody has identified yet.  Filling in the `ec` block is the
    whole job, and doing it is what turns a sysfs-only entry into a full one.
    """
    facts = facts or machine_facts()
    name = facts.get("product_name") or "MSI laptop"
    match = {}
    if facts.get("product_name"):
        match["product_name"] = [facts["product_name"]]
    if facts.get("board_name"):
        match["board_name"] = [facts["board_name"]]
    fw = firmware_glob(facts.get("ec_firmware") or "")
    if fw:
        match["ec_firmware"] = [fw]
    return {
        "schema": MODEL_SCHEMA,
        "id": model_id_from_name(name),
        "name": f"MSI {name}",
        "vendor": facts.get("sys_vendor") or "Micro-Star International Co., Ltd.",
        "status": "draft",
        "match": match,
        "ec": {},
        "charge": {"min": CHARGE_MIN_DEFAULT, "max": CHARGE_MAX_DEFAULT,
                   "step": 10},
        "features": {
            "shift": True, "fan": True, "cooler_boost": True,
            "charge_limit": True,
            "fan_curve": False, "rpm": False, "mux": False,
            "webcam": False, "webcam_block": False, "super_battery": False,
            "fn_key": False, "win_key": False,
        },
        "profiles": {
            "silent": {"shift": "eco", "fan": "silent",
                       "cooler_boost": False},
            "balanced": {"shift": "comfort", "fan": "auto",
                         "cooler_boost": False},
            "battery": {"shift": "eco", "fan": "auto",
                        "cooler_boost": False, "charge_end": 60},
        },
        "provenance": [
            "DMI identity and EC firmware read from this machine by "
            "`msi-control detect --write`",
        ],
        "notes": "DRAFT. Fill in the \"ec\" block with this machine's "
                 "fan-curve and tachometer registers, then set the matching "
                 "\"features\" flags to true. CONTRIBUTING.md explains how to "
                 "find them (the driver's debug/ec_dump plus the worked EC "
                 "map under models/) and how to test the result before "
                 "opening a pull request.",
    }


def write_draft(directory: str, facts: dict | None = None) -> str:
    """Write `draft_model()` into `<directory>/model.json`.  Returns the path."""
    path = os.path.join(directory, "model.json")
    if os.path.exists(path):
        raise ModelError(f"{path} already exists - refusing to overwrite it")
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(draft_model(facts), fh, indent=2)
        fh.write("\n")
    return path


# --------------------------------------------------------------------------
# The active model
#
# `MODEL`, `CURVE_REGS`, `N_TEMPS`, `RPM_REGS`, `RPM_NUMERATOR`,
# `BUILTIN_PROFILES` and `CHARGE_MIN`/`CHARGE_MAX` are published at the END of
# this file, once every helper they need has been defined - see "select this
# machine's model" at the bottom.  Everything here about the embedded
# controller is *this model's*, never a generic MSI layout.
# --------------------------------------------------------------------------

# NOTE: the fan registers are per-model and live in the registry; the
# graphics-mode addresses below are not.  See the MUX block.

# --------------------------------------------------------------------------
# Graphics mode switch (the "MUX")
#
# Unlike the fan registers, these addresses are not per-model: the reference
# tool ElXreno/msi-gpu-switcher drives the identical trio on MSI hardware from
# other model lines, so the addresses live here and every model file that
# declares "mux": true uses them.  What *is* per-model is whether anybody has
# established that the switch works there - see CONTRIBUTING.md.
#
# The firmware can run the panel off the discrete GPU alone, or off the Intel
# iGPU with the dGPU as a render offload (Optimus).  Three places encode the
# same single fact, and all three agree on this machine:
#
#   * EC register 0x2e, bit 6 - MSI's "PXCT" bit.  1 = the panel is wired to
#     the dGPU (discrete); 0 = the iGPU drives the panel (hybrid).
#   * the UEFI variable MsiDCVarData, byte 1 - the same polarity, read by the
#     firmware at POST, so it is what decides the *next* boot.
#   * whether an Intel PCI display controller exists at all - the firmware
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
# Temperature bands - ONE definition, shared by the CLI and the GUI.
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
# Human-readable setting names - used by every "what changed" summary.
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
# subject need overriding - the rest carry over unchanged.
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

    `describe_change` writes a *diff* - the right shape for a log line or a
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
    """`"Switched graphics to Hybrid (Intel + NVIDIA) - reboot to apply"`.

    The graphics mode is the one setting where "done" is a lie until the
    machine restarts, so the sentence says both what was requested and what is
    still outstanding.  A no-op says so instead of implying a switch.
    """
    label = MUX_LABELS.get(mode, mode)
    if now == mode and was == mode:
        return f"Graphics mode already {label}"
    if was == mode:
        return f"Graphics mode still {label} - reboot to apply"
    return f"Switched graphics to {label} - reboot to apply"

# The charge-threshold bounds are per model (`charge.min`/`charge.max`) and
# are published as CHARGE_MIN/CHARGE_MAX at the end of this file.

# Ranges we are willing to write into EC fan-curve bytes.  These are EC-level
# limits rather than per-model facts: a fan speed is a percentage and a
# threshold is a °C reading, so the same bounds police every model's curve.
TEMP_MIN, TEMP_MAX = 10, 100
SPEED_MIN, SPEED_MAX = 0, 100

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

# Built-in profiles are per-model data - the `gaming` profile's curve is a
# statement about one machine's thermal envelope - so they live in
# `model.json` and are published as BUILTIN_PROFILES at the end of this file.
#
# The rule that must survive every model file:
#
# ⚠️ A profile carries a `charge_end` ONLY when setting a charge limit is the
# point of that profile.  "silent" / "balanced" / "gaming" deliberately omit
# it, so applying one changes nothing about charging - because the limit is
# the one setting a person chooses for *battery health* rather than for
# performance, and silently pushing it back to 100% (which these three used
# to do) threw away a limit the user had deliberately picked.  The journal
# showed it happening as `applied profile balanced: charge limit 60% -> 100%`.
# The rule is uniform and worth keeping: a profile that does not mention a
# charge limit does not touch one.  "battery" is the exception because
# preserving the battery IS that profile's purpose.
#
# `clean_profile` (below) is what enforces the rest of a profile's shape, and
# it is applied to *both* a model's built-ins and a saved profile - a stored
# profile is untrusted input that the next boot replays into the EC.

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
# Validation - the daemon never trusts a client, and the GUI is a client.
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
    # bool is an int in Python - and "true" must never become speed 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or isinstance(value, float) and value != int(value):
        raise ValidationError(f"invalid {what}: not an integer")
    value = int(value)
    if not (lo <= value <= hi):
        raise ValidationError(f"invalid {what}: {value} (want {lo}..{hi})")
    return value


def _published(name: str, fallback):
    """A model-published module global, or `fallback` if not published yet.

    The active model is selected at *import*, at the very bottom of this file,
    so for the short window while a model file is being parsed the published
    names (`CHARGE_MIN`, `N_TEMPS`, …) do not exist.  The model loader passes
    its own values explicitly in that window; this helper is only the belt to
    that brace, so nothing here can ever raise `NameError`.
    """
    return globals().get(name, fallback)


def check_charge_end(value, lo: int | None = None, hi: int | None = None) -> int:
    lo = _published("CHARGE_MIN", CHARGE_MIN_DEFAULT) if lo is None else lo
    hi = _published("CHARGE_MAX", CHARGE_MAX_DEFAULT) if hi is None else hi
    return check_int(value, lo, hi, "charge limit")


def check_mux_mode(value) -> str:
    return check_choice(value, MUX_MODES, "graphics mode")


def check_curve(temps, speeds, n_temps: int | None = None,
                n_speeds: int | None = None):
    """Validate one fan curve.

    Rules the EC requires (and a wrong curve is the one setting that can
    cook the machine, so this is deliberately strict):
      * exactly `n_temps` temperature thresholds, strictly increasing,
        inside TEMP_MIN..MAX
      * exactly `n_speeds` fan speeds, non-decreasing, inside SPEED_MIN..MAX

    The shape comes from the model (`ec.curve`), not from this function: 6+7
    is the GS66's shape, and hard-coding it is how a model with a different
    fan curve would silently be rejected.  The defaults are the active
    model's, which is what every caller other than the model loader wants.
    """
    n_temps = _published("N_TEMPS", N_TEMPS_DEFAULT) if n_temps is None else n_temps
    n_speeds = _published("N_SPEEDS", N_SPEEDS_DEFAULT) if n_speeds is None else n_speeds
    if not isinstance(temps, list) or not isinstance(speeds, list):
        raise ValidationError("curve must be {temps:[...], speeds:[...]}")
    if len(temps) != n_temps:
        raise ValidationError(
            f"want {n_temps} temperature points, got {len(temps)}")
    if len(speeds) != n_speeds:
        raise ValidationError(f"want {n_speeds} fan speeds, got {len(speeds)}")

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


def clean_profile(data: dict, n_temps: int | None = None,
                  n_speeds: int | None = None,
                  charge_min: int | None = None,
                  charge_max: int | None = None) -> dict:
    """Keep only the known keys of a profile, validated.

    Used for a *stored* profile (untrusted input the next boot replays into
    the EC) and for a model's built-in profiles at load time - one function,
    so a built-in can never be shaped unlike a saved one.  `charge_end` is
    optional on purpose; see the note above BUILTIN_PROFILES.

    The bound arguments exist for the model loader, which parses profiles
    before the model's own bounds have been published; every other caller
    omits them and gets the active model's.
    """
    if not isinstance(data, dict):
        raise ValidationError("profile must be an object")
    out = {}
    if data.get("shift") is not None:
        out["shift"] = str(data["shift"])
    if data.get("fan") is not None:
        out["fan"] = str(data["fan"])
    if data.get("cooler_boost") is not None:
        out["cooler_boost"] = bool(data["cooler_boost"])
    if data.get("charge_end") is not None:
        out["charge_end"] = check_charge_end(data["charge_end"],
                                             charge_min, charge_max)
    if data.get("curve") is not None:
        curve = data["curve"]
        if not isinstance(curve, dict):
            raise ValidationError("curve must be an object")
        out["curve"] = {}
        for which in ("cpu", "gpu"):
            spec = curve.get(which)
            if spec is None:
                continue
            if not isinstance(spec, dict):
                raise ValidationError(f"curve.{which} must be an object")
            t, s = check_curve(spec.get("temps"), spec.get("speeds"),
                               n_temps, n_speeds)
            out["curve"][which] = {"temps": t, "speeds": s}
    if not out:
        raise ValidationError("profile has no settings")
    return out


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
# Local (unprivileged) reads - enough for a degraded `status` without the
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
    """BAT0/BAT1/… - this machine uses BAT1, others use BAT0."""
    bats = sorted(glob.glob(os.path.join(power_dir(), "BAT*")))
    return bats[0] if bats else None


def _lines(text) -> list:
    if not text:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def local_status(mux: bool = True) -> dict:
    """Best-effort state straight from sysfs - no daemon, no root.

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
        "model": MODEL.summary(),
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
# Graphics mode (MUX) - reading it without root
#
# The daemon reads the EC state bit with ec_read (two sysfs ops).  A client
# without the daemon cannot: msi-ec's debug/ec_get needs the address written
# to it first, and that write is root-only.  So the unprivileged view is built
# from the two sources that *are* world-readable - the UEFI variable and which
# PCI display controllers exist - and the EC bit is reported as unknown rather
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
    row label and never from counting bytes across the whole file - a header
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
    the one observation of what the running machine is actually doing - as
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
        # Whether *this model* is known to have a working switch at all.
        # `supported` above is about what this machine exposes; this one is
        # about whether anybody has established it works here, and a card
        # that greys a control out has to say which of the two it means.
        "model_supports": MODEL.feature("mux"),
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


# --------------------------------------------------------------------------
# Select this machine's model, and publish it
#
# This runs once, at import, because "which laptop am I on" cannot change
# while the process lives and deciding it lazily would let two callers
# disagree.  The module-level names below are the model's, which is why the
# daemon, the CLI and the GUI never have to ask a second time - and why a
# per-model register address is typed in exactly one place in the program,
# `models/<id>/model.json`.
#
# A model file that will not parse is *reported*, not raised: the machine it
# breaks for is almost never the machine of the person who wrote it, and the
# one who can fix it is the one reading `msi-control detect`.  The active
# model then falls back to the unsupported one, which is a working state.
# --------------------------------------------------------------------------

MODEL: Model

try:
    MODEL = select_model()
except ModelError as _e:                    # only MSI_MODEL=… can get here
    raise SystemExit(f"msi-control: {_e}") from None

# Fan-curve and tachometer registers of the active model.
CURVE_REGS = dict(MODEL.curve_regs)
N_TEMPS, N_SPEEDS = MODEL.n_temps, MODEL.n_speeds
RPM_REGS = dict(MODEL.rpm_regs)
RPM_NUMERATOR = MODEL.rpm_numerator

# Charge-threshold bounds.
CHARGE_MIN, CHARGE_MAX = MODEL.charge_min, MODEL.charge_max

# Built-in profiles.
BUILTIN_PROFILES = dict(MODEL.profiles)


def active_model() -> Model:
    return MODEL


def feature(name: str) -> bool:
    """Does the active model declare `name`?  See MODEL_FEATURES."""
    return MODEL.feature(name)


def model_supported() -> bool:
    return MODEL.supported


def model_errors() -> list:
    """Model files that failed to load, as human sentences."""
    _models, errors = load_models()
    return errors

