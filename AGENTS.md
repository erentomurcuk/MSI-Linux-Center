# AGENTS.md — working on MSI Linux Control

This file is for an AI coding agent (or a person) picking this repository up
cold. It is the map: what the project is, how the pieces fit, the rules that
are not negotiable, and — the part most likely to be your job — how to add a
laptop model so a machine that is not the dev machine works fully.

Read this first, then [`APP_DETAILS.md`](APP_DETAILS.md) for the deep
architecture, and [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution
workflow. If you are here *because your laptop is not fully supported*, jump to
[Adding a model](#adding-a-model-the-usual-job).

---

## 1. What this is

A Linux control centre for **MSI laptops**: battery charge thresholds,
performance (shift) modes, fan modes and Cooler Boost, custom fan curves, named
profiles, a graphics-mode (MUX) switch, and live temperature / fan-RPM
monitoring — the things MSI's Dragon Center does on Windows, on Linux, without
Dragon Center.

It was written for one machine (an MSI GS66 Stealth 10SE) and then generalised:
the laptop-specific facts — EC register addresses, which features the firmware
has, the built-in profiles — now live in a **model registry** under `models/`,
one JSON file per model. The code is about MSI laptops in general; a model file
is what makes it about *your* laptop.

| Piece | Runs as | Talks to |
|---|---|---|
| `src/msi_controld.py` → `msi-controld` | root, systemd | the EC, through the **only** writer |
| `src/msi_control.py` | imported by all | the model registry, validation, paths, protocol |
| `src/msi-control` | unprivileged CLI | the daemon's Unix socket |
| `src/msi-control-gui` | unprivileged, PySide6 | the daemon's Unix socket |
| `models/<id>/model.json` | data | read by `msi_control` at import |

```
msi-control-gui ─┐
msi-control     ─┼─► /run/msi-controld.sock ─► msi-controld (root) ─► msi-ec + BAT*
                 ┘        JSON one line              │
                                              msi-ec kernel driver
                                              (vendored, out-of-tree, debug=1)
```

**Only `msi-controld` writes to the EC.** It validates every value it is
handed, takes one lock around every EC write, and **reads every byte back** so
it can never report a write that did not stick. With the daemon down the GUI
and CLI degrade to read-only rather than lying.

Everything the daemon and CLI do is **stdlib-only** — they must work in a
recovery shell with nothing installed. PySide6 is a GUI-only dependency.

---

## 2. The one rule that matters

> **The daemon owns every EC write, and every write is read back.**

Nothing outside `msi_controld.py` may touch the embedded controller. Not the
GUI, not the CLI, not a script, not a test. If you find yourself wanting to
poke a register from somewhere else, the answer is to add it to the model and
go through the daemon — a stray EC write is the one way this program can
hard-crash the machine.

When you share a register with another function, **read-modify-write one bit**,
never a whole-byte poke. `0x2e` is the live example: bit 6 is the MUX switch and
bit 1 is the webcam, so `0x2e |= 0x40` would switch the webcam on as a side
effect. Every such write in this tree is a single-bit RMW for exactly that
reason.

---

## 3. Repository layout

```
src/msi_control.py      shared core: model registry, EC map, validation, paths
src/msi_controld.py     the privileged daemon (systemd service msi-controld)
src/msi-control         CLI (subcommands: status, detect, monitor, mode, fan,
                        boost, battery, curve, profile, graphics, reapply,
                        raw, wait)
src/msi-control-gui     PySide6 GUI, six sections
models/<id>/model.json  one laptop model — THE file you add for a new laptop
models/README.md        the model-file format, field by field
models/<id>/README.md   optional: notes on how that model's registers were found
tests/test_msi_control.py  offline suite — fake sysfs + RAM-backed EC, 142 tests
tests/gui_smoke.py         headless GUI build + behaviour + screenshots
tests/shots/               committed screenshots — dark, light/ beside it
tools/                  hardware probes (need root) + fixup/recapture helpers
third_party/msi-ec/     vendored teackot/msi-ec v0.13 — do not edit or rename
systemd/ modprobe/ desktop/ icons/ etc/
install.sh uninstall.sh verify.sh
Makefile                check / test / smoke / shots / verify / install …
README.md APP_DETAILS.md CONTRIBUTING.md CHANGELOG.md TODO.md AGENTS.md
```

`third_party/msi-ec/` is upstream code kept in-tree (not a submodule) so the
DKMS build needs no network. Its device strings, identifiers and file names are
**upstream's**, not ours — never rename anything inside it to fit our naming.

---

## 4. Building and testing

There is no build step. To run a test you need nothing but Python 3.10+:

```bash
python3 tests/test_msi_control.py                       # 142 tests, no root, ~6 s
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py    # GUI, needs PySide6
make check        # compile + the offline suite + the GUI smoke test
make shots        # regenerate tests/shots/          (dark)
make shots-light  # regenerate tests/shots/light/    (light)
sudo ./verify.sh  # live hardware checks — root, safe and reversible
```

`tests/test_msi_control.py` needs **no root and no hardware**: it builds a fake
sysfs tree and a RAM-backed EC and runs the *real* daemon over the *real*
socket, so protocol, validation, profiles and the CLI are exercised
end-to-end. `MSI_SYSFS`, `MSI_SOCK`, `MSI_CONFIG`, `MSI_STATE` and `MSI_MODELS`
redirect every path, which is what makes that possible — and it is also how you
test a new model without installing it.

**Run `make check` after every change.** A green suite is the bar; there is no
CI. If you changed a colour, a label or a state machine, extend
`tests/gui_smoke.py`. If you changed the daemon, the CLI or a model, extend
`tests/test_msi_control.py`. A behaviour only a human can check will be broken
again within a month.

---

## 5. The model registry

A model file describes **only what differs between machines**:

```jsonc
{
  "schema": 1,
  "id": "gs66-stealth-10se",          // must equal the directory name
  "name": "MSI GS66 Stealth 10SE",
  "vendor": "Micro-Star International Co., Ltd.",
  "status": "stable",                 // draft | testing | stable

  "match": {                          // how this machine is recognised
    "product_name": ["GS66 Stealth 10SE"],   // DMI, weight 50
    "board_name":   ["MS-16V1"],             // DMI, weight 40
    "ec_firmware":  ["16V1EMS1.*"]           // EC fw glob, weight 30
  },

  "ec": {                             // the per-model EC addresses
    "curve": {
      "cpu": {"temps": ["0x6a","0x6b","0x6c","0x6d","0x6e","0x6f"],
              "speeds":["0x72","0x73","0x74","0x75","0x76","0x77","0x78"]},
      "gpu": {"temps": ["0x82","0x83","0x84","0x85","0x86","0x87"],
              "speeds":["0x8a","0x8b","0x8c","0x8d","0x8e","0x8f","0x90"]}
    },
    "rpm": {"numerator": 478000, "cpu": "0xcc", "gpu": "0xca"}
  },

  "charge": {"min": 20, "max": 100, "step": 10},

  "features": {                       // what THIS firmware can do
    "shift": true, "fan": true, "cooler_boost": true, "charge_limit": true,
    "fan_curve": true, "rpm": true, "mux": true,
    "webcam": false, "webcam_block": false, "super_battery": false,
    "fn_key": false, "win_key": false
  },

  "profiles": {                       // this model's built-in profiles
    "silent":   {"shift": "eco",     "fan": "silent",   "cooler_boost": false},
    "balanced": {"shift": "comfort", "fan": "auto",     "cooler_boost": false},
    "gaming":   {"shift": "sport",   "fan": "advanced", "cooler_boost": false,
                 "curve": {"cpu": {"temps": […], "speeds": […]}, "gpu": {…}}},
    "battery":  {"shift": "eco", "fan": "auto", "cooler_boost": false,
                 "charge_end": 60}
  },

  "provenance": ["where these addresses came from — a link, a dump, a measurement"],
  "notes": "the one or two non-obvious facts about this firmware"
}
```

Rules the loader enforces (a violation is a message naming the file and the
field, never a silent wrong number written to the EC):

* `id` must equal the directory name; the same `PROFILE_NAME_RE`
  (`^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`) applies to model ids and profile names.
* Feature flags must be `true`/`false`. `rpm: true` requires `ec.rpm` present;
  `fan_curve: true` requires both curve blocks. Absence is checked, so a model
  cannot *claim* a feature it has no registers for.
* A built-in profile may not be named `factory` — `factory` is reserved for the
  snapshot captured from the live EC on first run.
* Charge bounds must be sane (`min <= max`, both 20–100 by convention).

**Shift modes, fan modes, Cooler Boost and the charge threshold are NOT in the
model file.** The `msi-ec` driver exposes those through sysfs on every machine
it binds to, and its own `available_*_modes` attributes say which values a
given firmware accepts — they are model-independent facts. A model file is only
for the registers the driver does not know.

### What happens with no matching model

Matching nothing is a **working state, not a broken one**. The *unsupported*
model still does everything the driver exposes by itself — temperatures, shift
and fan modes, Cooler Boost, the charge limit — and **refuses** what needs
model-specific registers: fan curves, RPM and the graphics switch. It never
guesses at a register. The GUI shows an amber banner, the Fan tab goes
read-only with a notice, and the Settings tab points at `msi-control detect`.

---

## 6. Adding a model — the usual job

This is the task most likely to bring you here, so it is written as a
procedure. It is the same whether an agent does it or the user does; an agent
can do steps 2–5 and open the pull request in step 7.

### Step 1 — detect

```bash
msi-control detect            # identity + driver + registry, human readable
msi-control detect --json     # the same as machine-readable JSON
msi-control detect --ec-dump  # + the 256-register EC map (needs msi-ec debug=1)
```

`detect` needs **no root and no running daemon** — it reads world-readable DMI
and sysfs. It prints this machine's DMI strings, the EC firmware revision, which
`msi-ec` attributes exist, and whether any registry entry matches (with the
score and the runners-up).

### Step 2 — draft

```bash
msi-control detect --write /tmp                # -> /tmp/<id>/model.json
```

This writes a **valid but modest** draft: the `match` block filled in from the
hardware, `ec` empty, `fan_curve`/`rpm`/`mux` all `false`, and the everyday
profiles. It never claims a feature whose register nobody has identified yet.

### Step 3 — find the missing registers

The draft already makes the machine usable (sysfs features all work). To turn on
fan curves and RPM you need this model's addresses, which differ per firmware.
Two sources:

* **A dump.** With the driver's debug interface on
  (`debug/ec_get` / `debug/ec_set` / `debug/ec_dump` under
  `/sys/devices/platform/msi-ec/debug/`), `msi-control detect --ec-dump` gives
  all 256 bytes. Change one thing at a time on the machine and diff the dump:
  set a fan curve point, watch which byte moves; light Cooler Boost, watch the
  tachometer bytes. That is how the worked example's map was built.
* **Prior art.** Other projects carry EC maps for many MSI models — commonly
  YoyPa/isw (whose `[Firmware]` sections hold real dumps), the `msi-ec` driver's
  in-tree `CONF_*` tables, and other forks of this program. **Check the
  licence** before copying anything, and record the source in `provenance`.

Write the addresses into the `ec` block using hex strings (`"0x6a"`), set the
`features` flags you have proven `true`, and adjust `profiles` (curves are
model-specific — the GS66's are a reasonable *starting shape*, not a promise).

### Step 4 — the worked example

[`models/gs66-stealth-10se/`](models/gs66-stealth-10se/) is the reference: the
model file plus a README explaining exactly where each address came from and
how it was confirmed. Start from its shape; do not copy its *values* unless your
machine really shares the firmware family `16V1EMS1` (in which case the EC
firmware glob will match and you may not need a new file at all).

### Step 5 — test before you ask anyone to trust it

```bash
# 1. Does the model load and validate, and does it match? (no root)
MSI_MODEL=<your-id> python3 -c 'import sys; sys.path.insert(0,"src");
import msi_control as c; print(c.MODEL, c.MODEL.features)'

# 2. Does the whole stack still pass with your model present?
make check

# 3. On YOUR machine, with the daemon running (root, reversible):
sudo ./verify.sh
```

`verify.sh` walks every shift and fan mode, Cooler Boost, the charge threshold
and a full fan-curve write-back round-trip, and reports each as OK or FAIL. It
only becomes meaningful once a matching model exists; before that it says so
and skips the register-backed checks. **It restores what it changes** — but run
it on a machine you can afford to have mis-set, not on a production box.

### Step 6 — fill in the model's README

Copy `models/gs66-stealth-10se/README.md` and replace the content: which
addresses you found, how, and what you measured. The next person with your
model needs the *provenance*, not just the numbers.

### Step 7 — open a pull request

```bash
git checkout -b add-model-<your-id>
git add models/<your-id>/
git commit -F - <<'MSG'
Add model <your-id>

<provenance: hardware, firmware revision, how the registers were confirmed>
MSG
git push -u origin add-model-<your-id>
```

Keep the PR to **one model** plus its `models/<id>/README.md`; that keeps review
about one machine's registers. Include the `msi-control detect` output (edit out
your serial/hostname if you care) in the PR description, and say which feature
flags you turned on and how you proved each one. A model that ships with a
`status` of `draft` is welcome — a model that turns on `fan_curve` without a
write-back test is not.

### If you cannot find a register

Leave the flag `false`. A model that reads temperatures and controls shift and
fan modes is genuinely useful on its own; one that writes a guessed address into
an unknown EC is a machine that will not boot. **The unsupported state is a
feature, not a bug.**

---

## 7. Environment variables (and why they exist)

| Variable | Overrides | Used by |
|---|---|---|
| `MSI_SYSFS` | `/sys` | the fake sysfs tree in tests, probes |
| `MSI_SOCK` | `/run/msi-controld.sock` | tests (a throwaway socket) |
| `MSI_CONFIG` | `/etc/msi-control/config.json` | tests |
| `MSI_STATE` | `/var/lib/msi-control` | tests |
| `MSI_MODELS` | the registry directory | tests, and trying a model out un-installed |
| `MSI_MODEL` | model auto-detection — forces one model id | tests, debugging |
| `MSI_THEME` | `light` \| `dark` (else follow the desktop) | the GUI |

`MSI_MODEL` with an id that is not in the registry is a **hard error**, not a
fallback — silently falling back would turn a typo into "your laptop is
unsupported". An *empty* `MSI_MODEL` means "no hint, auto-detect".

---

## 8. Conventions that are enforced by tests

These are not style preferences; each one exists because the thing it prevents
has happened here.

* **Contrast is measured.** Every theme colour pair is checked against WCAG AA
  in `gui_smoke.py`. Adding a colour means adding it to the check.
* **The painted mark and the shipped icon are the same picture.** The GUI's
  `brand_pixmap()` and `icons/msi-control.svg` are rasterised and compared by
  ink box. Change one, change the other.
* **Two themes, one token set each, the same keys.** A theme cannot silently
  lose a border.
* **One control height, one type scale.** The smoke test measures them, because
  a 10 px-taller button is invisible as a bug and just looks emphasised.
* **Comments explain *why*.** The reason a constant was chosen, the bug a guard
  prevents, the measurement behind a value. `# increment i` is noise; "speeds[0]
  owns no point" saves the next reader an hour.
* **Every change updates the docs in the same change**, and adds a row to
  [`CHANGELOG.md`](CHANGELOG.md). A change that leaves the docs describing the
  old behaviour is a bug in the docs.

---

## 9. Safety

1. **Read** [`README.md`](README.md) §"Safety model" and the MUX warning before
   touching the graphics switch.
2. **The privacy and safety posture wins.** If a feature conflicts with the
   daemon-owns-writes rule or the read-back rule, the rule wins.
3. **Back up before you change** anything you cannot regenerate — a
   `~/backup-pre-*/` copy, the project convention.
4. **`AllowRaw` and `AllowMuxSwitch` default to off.** Do not turn them on for
   convenience.
5. **Never reboot the machine for the user** — the MUX switch applies at the
   next boot, and the program says so instead of rebooting.

---

## 10. Change log

Every upgrade, fix or configuration change gets a row in
[`CHANGELOG.md`](CHANGELOG.md), in the same session, before the work is called
done. Old rows keep the old names (`GS66 Center`, `gs66-centerd`) — that is
history, not a stale reference to fix.
