# APP_DETAILS: MSI Linux Control Center

Everything the next session needs to know about this project, without
re-deriving it. Companion to [`TODO.md`](TODO.md).

---

## 1. What this is

A Linux control centre for **MSI laptops**, replacing MSI Dragon Center:
battery charge thresholds, fan speeds / modes / curves, performance (shift)
modes, Cooler Boost, live monitoring, the graphics MUX switch, an all-core
CPU frequency cap, Intel voltage offsets (undervolting), and panel overdrive.
It was written for one machine and then
generalised, so the laptop-specific facts live in a **model registry**
(`models/<id>/model.json`) rather than in the code; see §2a.

| Fact | Value |
|---|---|
| Reference machine | MSI GS66 Stealth 10SE (the dev machine, and the worked example) |
| EC firmware | `16V1EMS1.118` |
| BIOS | `E16V1IMS.112` |
| OS | Fedora 44, kernel `7.2.9-201.fc44.x86_64`, KDE Plasma 6 (Wayland) |
| User | `eren`, uid 1000, in `wheel` |
| Secure Boot | **disabled** (out-of-tree module requires this) |
| Project root | `~/Projects/MSI-Linux-Control` (renamed from `~/Projects/MSIControl`, itself moved from `~/msi-control`) |

**GUI toolkit: PySide6 6.11.2**. The user asked for PyQt6; PySide6 was
substituted because it was already installed. The difference is licensing
(LGPL vs GPL) and the import package name; the API is near-identical.
**This substitution must be reported in the final summary to the user.**

---

## 2. Architecture

Three programs, one shared module. The daemon owns *every* hardware write
(EC, cpufreq sysfs, the MSR mailbox); the CLI
and GUI are unprivileged clients.

```
   msi-control-gui (PySide6)  ─┐
                        ├── JSON over Unix socket ──> msi-controld (root, systemd)
   msi-control (CLI)  ──┘                                  │
                                    ┌─────────────────────┼─────────────────────┐
                                    ▼                     ▼                     ▼
                     msi-ec driver sysfs        cpufreq sysfs (scaling_*)   /dev/cpu/*/msr
                     + debug iface              one file per policy         the OC mailbox, MSR 0x150
```

* **`src/msi_controld.py`**: root daemon, systemd system service `msi-controld`.
  One JSON object per line, one request per connection. Validates every
  client value, serialises all EC access under one lock, and **verifies each
  write by reading it back**.
* **`src/msi-control`**: stdlib-only CLI. Reads work without the daemon
  (plain sysfs); writes need it.
* **`src/msi-control-gui`**: PySide6 GUI, eight sections. Never privileged, never
  touches sysfs. All daemon traffic runs on one worker thread.
* **`src/msi_control.py`**: the model registry, the EC register map,
  validation, protocol and paths. Single source of truth so daemon/CLI/GUI can
  never disagree, and the place a model file is loaded and validated.

### Design rules (enforced, not aspirational)

1. Never trust a client: everything is validated in `msi_control`.
2. Every hardware write is followed by a read-back: an EC byte, a cpufreq
   sysfs value, an MSR mailbox plane. Hardware that silently ignores a write
   is indistinguishable from hardware that accepted it; for the undervolt
   planes the same read-back doubles as the BIOS-lock detector.
3. Nothing is written optimistically in the UI; the GUI re-reads state after
   each command, so what you see is what the EC actually holds.
4. With the daemon down the GUI/CLI degrade to **read-only** instead of lying.
5. stdlib-only for daemon and CLI, which must work in a recovery shell.

### Environment overrides (testability)

`MSI_SYSFS`, `MSI_SOCK`, `MSI_CONFIG`, `MSI_STATE`: let the whole stack
run against a fake sysfs tree and a RAM-backed EC with no root at all.
`MSI_CPU_DEV` redirects the `/dev/cpu` tree the undervolt backend probes
(default `/dev/cpu`, what the `msr` module creates).
`MSI_MODELS` overrides the model-registry directory, and `MSI_MODEL` forces a
model id (§2a).

---

## 2a. The model registry

The program is about MSI laptops in general; a **model file** is what makes it
about *this* laptop. One JSON object per model under `models/<id>/model.json`,
loaded and validated at import:

| Section | Holds |
|---|---|
| `match` | how a machine is recognised: DMI `product_name` (weight 50), `board_name` (40), EC `ec_firmware` glob (30); case-insensitive anchored globs, highest total wins |
| `ec` | per-model registers: `curve` (cpu/gpu temps + speeds), `rpm` (`numerator`, cpu/gpu tach addresses) and `overdrive` (panel-overdrive bit) |
| `charge` | the charge-threshold bounds this firmware accepts |
| `features` | thirteen flags: the four driver ones, the four register-backed (fan curves, RPM, MUX, panel overdrive), and five optional |
| `profiles` | this model's built-in profiles (curves are model-specific) |

Full field-by-field reference: [`models/README.md`](models/README.md). The
worked example is [`models/gs66-stealth-10se/`](models/gs66-stealth-10se/).

Key behaviours:

* **Validated at load, not at use.** A typo in a register is a message naming
  the file and the field, never a wrong number written to the EC. `fan_curve:
  true` requires `ec.curve`; `rpm: true` requires `ec.rpm`; `display_overdrive:
  true` requires `ec.overdrive`; `id` must match its
  directory; no profile may be called `factory`.
* **Matching nothing is a working state.** The built-in `unknown` model enables
  everything the `msi-ec` driver exposes by itself (temps, shift/fan modes,
  Cooler Boost, charge limit) and **refuses** fan curves, RPM and the graphics
  switch with `NotSupported(feature)` instead of guessing.
* **`MSI_MODEL` forcing a missing id is a hard error**, so a typo cannot look
  like "unsupported". An empty `MSI_MODEL` means auto-detect.
* Model values are published as module-level globals at the **end** of
  `msi_control.py` (`MODEL`, `CURVE_REGS`, `N_TEMPS`, `N_SPEEDS`, `RPM_REGS`,
  `RPM_NUMERATOR`, `CHARGE_MIN`, `CHARGE_MAX`, `BUILTIN_PROFILES`) once every
  helper they need exists, so the parser can call `clean_profile` et al. while
  it is still building the model.
* `msi-control detect` (no root, works with the daemon down) prints the report a
  contributor pastes into a PR; `--json` machine output; `--write DIR` a valid
  draft; `--ec-dump` the 256-register map.
* **`VERSION`** (near the top of `msi_control.py`) is the one version number:
  the CLI's `--version` and the Settings card both read it, so a build can be
  named in a bug report.

The MUX addresses are **not** per-model; the whole MSI family shares one block
(§3), so a model only sets `"mux": true`.

---

## 3. EC register map

> This map is the **GS66 Stealth 10SE's**, and in the program it lives in
> `models/gs66-stealth-10se/model.json`, not in the code (§2a). It is kept here
> in full because it is the reasoning and the provenance, not just the numbers.

Cross-checked against YoyPa/isw's `[16V1EMS1]` section + `MSI_ADDRESS_DEFAULT`
(which carries a real EC dump from a GS66-10SE), teackot/msi-ec `CONF_G1_3`,
and the in-tree `drivers/platform/x86/msi-ec.c` charge semantics.

| Purpose | Register(s) |
|---|---|
| Performance ("shift") mode | `0xf2`: eco `0xc2`, comfort `0xc1`, sport `0xc0`, turbo `0xc4` |
| Fan mode | `0xf4`: auto `0x0d`, silent `0x1d`, advanced `0x8d` |
| Cooler Boost | `0x98` bit 7 |
| Charge threshold | `0xef`: `end` is `raw` minus `0x80`, `start` is `end` minus 10, range 20-100 |
| CPU temp / fan % | `0x68` / `0x71` |
| GPU temp / fan % | `0x80` / `0x89` |
| CPU curve | temps `0x6a-0x6f`, speeds `0x72-0x78` |
| GPU curve | temps `0x82-0x87`, speeds `0x8a-0x90` |
| Tachometers | `0xcc` (CPU), `0xca` (GPU), 16-bit big-endian period |
| **Graphics mode (MUX)** | `0xd1` (trigger) · `0x2e` bit 6 (legacy state) · UEFI `MsiDCVarData` byte 5 (three-mode) or byte 1 (legacy) |

Curve semantics: **6 temperature thresholds, 7 speed levels**. The fan spins
at `speeds[0]` until the temperature passes `temps[0]`, then `speeds[1]` …
`speeds[6]`. Validation requires temps strictly increasing, speeds
non-decreasing.

> 🔢 **RPM = `478000 // period`**, reproduced verbatim from isw. A wrong
> constant silently prints plausible-looking nonsense. **Confirmed against
> hardware 2026-10-16**: Cooler Boost drives `0xcc` from ~3700 to **~8100**,
> which is the top of this laptop's fan range, so the constant is right and
> the scale is real.

### The MUX registers are *not* in the driver's map

Everything above comes from `msi-ec`'s `CONF_G1_3` or from isw. The graphics
mode does not: **`msi-ec` has no MUX support of any kind**, so the addresses
below are reverse-engineered and cross-checked against
[`ElXreno/msi-gpu-switcher`](https://github.com/ElXreno/msi-gpu-switcher),
whose two backends cover both dialects the MSI family speaks. The dialect in
use is discovered at runtime from the variable itself, never per-model:

* **Three-mode firmware** (newer Optimus machines): variable data byte 5
  carries the request in bits 0-1 (`0` hybrid, `1` discrete, `2` integrated),
  the applied mode in bits 2-3, bit 4 (`0x10`) is the **marker** that this
  dialect is in use, bit 5 says integrated mode exists, and bit 6 set means
  discrete is **not** supported (the GUI refuses the switch then). A switch
  is: read-modify-write byte 5, write the efivar, arm the trigger, wait for
  the acknowledge.

* **Legacy firmware** (the AMD Alpha 17 the reference tool was written for):
  variable byte 1, the trigger, and the state bit:

| Where | What it holds |
|---|---|
| EC `0x2e` bit 6 | MSI's "PXCT" bit: 1 = panel on the dGPU, 0 = hybrid |
| EC `0xd1` bits 0-1 | the "apply at next POST" trigger; write `(v & ~0x03) \| 0x01`, answer 0x02 |
| UEFI `MsiDCVarData` byte 1 | the firmware reads this at POST: 1 = discrete, 0 = hybrid |

* **Neither marker**: the variable carries no documented request, and the
  program reports exactly that instead of guessing. This is not a corner
  case on this project's own machine: the GS66's `16V1EMS1` firmware rewrites
  byte 1 itself one second after every POST, mirroring the mode the BIOS
  applied, not a request. Reading byte 1 as one used to produce a permanent
  phantom "next boot: Discrete, reboot required" on a machine that booted
  hybrid every time; found 2026-11-15 by watching the variable get rewritten
  at boot, and fixed by gating every variable-side request on the marker bit.

`0x2e` also carries the **webcam at bit 1**, which is exactly why every write
here is a read-modify-write of one bit rather than a whole-byte poke.

Write order follows the reference tool: **UEFI variable → trigger →
(acknowledge, three-mode) → EC bit (legacy)**. The daemon then re-reads what
it wrote and raises rather than reporting a switch that did not stick; on
three-mode firmware the acknowledge can land between the write and the
read-back, so the trigger write verifies only the request bit, not the whole
byte.

The `MsiDCVarData` variable is `state: 7` (non-volatile, boot-service,
runtime-accessible) and normally **immutable** (`lsattr` →
`----i-----------------`). `MsiEc._efivar_write()` therefore clears
`FS_IMMUTABLE_FL` with an `fcntl` ioctl, does a **single** `os.write()` of the
4 attribute bytes plus the payload (efivarfs refuses a split write), and
restores the flag in a `finally`. No `chattr`, no subprocess; the daemon
spawns nothing.

Reading the mode needs no privileges at all except for the EC bit:
`/sys/firmware/efi/efivars/…` and `/sys/bus/pci/devices/*/vendor` are both
world-readable, which is how `msi-control status` still reports the mode with
the daemon down. **`debug/ec_dump` is deliberately not used for this**: it is
world-readable but costs **~120 ms per read** (the driver walks all 256
registers), which is far too much for a 1.5 s UI poll.

### EC debug interface gotcha

The driver's debug interface is **write-address-then-read**:

```bash
echo "f2" > /sys/devices/platform/msi-ec/debug/ec_get   # set address
cat     /sys/devices/platform/msi-ec/debug/ec_get       # read that byte
```

`cat` alone returns the *previous* address. Requires root. Only available
when the module is loaded with `debug=1` (install.sh sets this in
`/etc/modprobe.d/msi-ec.conf`).

### The non-EC hardware writes (cpufreq + MSR 0x150 + the overdrive bit)

The daemon writes to three things that are not the msi-ec sysfs tree;
all three follow the same rules (validate, serialise, read back):

* **cpufreq sysfs** (`/sys/devices/system/cpu/cpufreq/policyN/
  scaling_max_freq`): the all-core cap. Facts come from `cpufreq_facts()`
  (min/max hardware bounds, per-policy lists); `CpuFreq.set_all_max`
  lifts a policy's `scaling_min_freq` when the requested max is below it,
  because cpufreq refuses a max under the floor. Not in `snapshot()` ever:
  the kernel resets the values at boot, so persisting them would be a lie.
  The read-back is a short poll (80 ms deadline per policy), not one
  shot: on Intel cpufreq (intel_pstate with HWP) the kernel applies a new
  limit *after* `write(2)` returns, so an immediate read saw the previous
  cap and refused a write that had stuck, on every set after the first.
  A genuine refusal still surfaces once the deadline passes.
* **MSR `0x150`, the OC mailbox**: the ThrottleStop undervolt protocol,
  reimplemented the way Leoyzen's gist and georgewhewell/undervolt do.
  Command write `0x8000001000000000 | plane << 40` ("data follows") or
  `0x8000000000000000 | plane << 40` ("read back"), then a data word =
  11-bit two's complement of `round(mV * 1.024)` at bits 21-31.
  `-175 mV` encodes to `0xE9A00000`, verified against the value
  ThrottleStop shows. Planes: 0 core, 1 gpu, 2 cache, 3 uncore, 4 analogio;
  clamp `0..-300 mV`. Per-thread state: every command goes to every
  `/dev/cpu/*/msr`; `read_plane` returns the values of all threads
  shallow-sorted, so the UI can never over-report the depth. A locked BIOS
  (the Plundervolt default) ignores the writes: the read-back mismatch is
  the *only* detector, and `Undervolt.apply` reports it naming the three
  MSI BIOS toggles. Offsets clear at boot and at suspend, which is the
  safety property the whole feature leans on; nothing about them persists
  daemon-side. `MSI_CPU_DEV` (default `/dev/cpu`) exists for the tests.
* **The overdrive bit**, EC `0x2e` bit 4 on the machines it was measured
  on (GP76 10UG, Blur Busters forum): behind the `display_overdrive`
  model flag plus `ec.overdrive` (`address`, `bit`, optional whole-byte
  `off`/`on` values). Same byte as the webcam (bit 1) and the MUX state
  (bit 6), so `MsiEc.set_overdrive` is a one-bit RMW unless the model
  supplies explicit byte values. **Not proven for `16V1EMS1`**, so the
  GS66 model ships with the flag `false` and the GUI/CLI report
  "no proven register" rather than flipping a bit nobody has watched
  move on this firmware.

---

## 4. Install layout

| Installed path | Source |
|---|---|
| `/usr/local/lib/msi-control/` | `msi_control.py`, `msi_controld.py` |
| `/usr/local/lib/msi-control/models/` | the model registry (`models/<id>/…`), copied whole |
| `/usr/local/bin/msi-control` | `src/msi-control` |
| `/usr/local/bin/msi-control-gui` | `src/msi-control-gui` |
| `/etc/msi-control/config.json` | created by install.sh if absent |
| `/var/lib/msi-control/` | state: `last.json`, `profiles/*.json` |
| `/usr/src/msi-ec-0.13/` | DKMS source (driver v0.13) |

* **The kernel driver is out-of-tree `teackot/msi-ec` v0.13 via DKMS**, not
  the in-tree one. The in-tree driver exposes only battery thresholds.
* DKMS installs into `updates/` so it **shadows** the in-tree module without
  clobbering it; `DEST_MODULE_LOCATION` is repointed accordingly. Package
  name is `msi-ec` (hyphen), source dir `msi-ec-0.13`.
* `dkms.conf` carries an `@VERSION@` placeholder that install/fixup `sed` away.

### 🔑 The `_add_lib_path()` pattern

All three programs open with the same helper: the wrappers live in
`/usr/local/bin` while the shared module lives in
`/usr/local/lib/msi-control`, so `dirname(__file__)` alone is not enough.
Without it the installed CLI/GUI dies with `ModuleNotFoundError:
msi_control`. It searches the script dir, `../lib/msi-control`, then the two
absolute candidates.

---

## 5. Daemon socket protocol

One JSON object per line, one request per connection.

```jsonc
{"cmd":"ping"}
{"cmd":"status"}                                    // incl. RPM + curve
{"cmd":"set","key":"shift","value":"eco"}
{"cmd":"set","key":"charge_end","value":60}
{"cmd":"curve","which":"cpu","temps":[…6],"speeds":[…7]}
{"cmd":"profile","action":"list|show|apply|save|delete|save-current","name":…}
{"cmd":"reapply"}
{"cmd":"selftest"}                                     // verify.sh ported; reversible, gated by --yes on the CLI
{"cmd":"capture-factory"}
{"cmd":"raw","addr":242,"val":13}                   // gated by AllowRaw
{"cmd":"graphics"}                                  // show the MUX state
{"cmd":"graphics","action":"set","mode":"hybrid"}    // gated by AllowMuxSwitch
{"cmd":"graphics","action":"revert"}                 // undo from the saved backup
{"cmd":"freq"}                                      // all-core cap facts
{"cmd":"freq","action":"set","max_khz":3500000}     // every policy, read back
{"cmd":"undervolt"}                                 // five planes + msr_present + allowed
{"cmd":"undervolt","action":"apply","values":{"core":-80}}  // gated by AllowUndervolt
{"cmd":"undervolt","action":"reset"}                // all planes back to 0
{"cmd":"overdrive"}                                 // supported? on?
{"cmd":"overdrive","action":"set","on":true}        // the model's proven bit, RMW
```

Response is always `{"ok":true,"data":{…}}` or `{"ok":false,"error":"…"}`.

Socket is `/run/msi-controld.sock`, mode `0660`, owner `root:<SocketGroup>`
(default `wheel`), so socket access grants nothing its holder could not
already get.

---

## 6. Config: `/etc/msi-control/config.json`

| Key | Default | Meaning |
|---|---|---|
| `RestoreLast` | `true` | re-apply last known state at daemon start (boot) |
| `DefaultProfile` | `null` | explicit profile at boot; wins over `RestoreLast` |
| `AllowRaw` | `false` | expose `msi-control raw`: an unvalidated EC byte is the one way this program can hurt you |
| `AllowMuxSwitch` | `false` | allow the graphics-mode (MUX) switch | 
| `AllowUndervolt` | `false` | allow writing Intel voltage offsets (MSR `0x150`); offsets are still volatile, the gate only unlocks the write |
| `SocketGroup` | `wheel` | group allowed to talk to the socket |
| `ReapplyOnResume` | `true` | re-apply after S3, which sometimes resets the EC |

A broken/absent config never stops the daemon, because defaults are safe.

`AllowMuxSwitch` is off for a *different* reason from `AllowRaw`: the switch is
fully validated, but it changes what the firmware does at the next POST, and on
a model whose MUX was never reverse-engineered that can leave the machine with
no display. Requiring a config edit makes it a deliberate act rather than a
stray click, and it is the same "make the dangerous thing explicit" pattern as
`AllowRaw`. It is **never** applied automatically: not by `RestoreLast`, not by
`ReapplyOnResume`, not by any profile; see the comment on
`Service.snapshot()`, and `test_only_battery_carries_a_charge_end`'s sibling
rule for graphics.

---

## 7. Built-in profiles

`silent`, `balanced`, `gaming`, `battery`. Plus **`factory`**, which is *not*
hard-coded: it is captured from the EC on first run so the original firmware
curve can always be restored. The GUI fetches it via the **read-only**
`profile show`, never `apply`, which would push settings into the EC as a
side effect.

| Profile | shift | fan | cooler boost | curves | `charge_end` |
|---|---|---|---|---|---|
| `silent` | eco | silent | off | none | *absent* |
| `balanced` | comfort | auto | off | none | *absent* |
| `gaming` | sport | advanced | off | cpu + gpu | *absent* |
| `battery` | eco | auto | off | none | **60** |
| `factory` | captured | captured | captured | captured | captured if readable |

> ⚠️ **`charge_end` is optional in a profile, on purpose.** Absent means
> *"leave the charge limit alone"*, and `_apply_profile_dict` skips the write
> entirely (`if prof.get("charge_end") is not None`). A charge limit is a
> battery-health choice rather than a performance one, so only `battery`,
> whose whole purpose is battery preservation, carries a value. The three
> performance profiles used to ship `charge_end: 100`, which meant applying
> `balanced` silently reset a deliberately-chosen limit; the daemon's own
> journal recorded it as `applied profile balanced: charge limit 60% -> 100%`.
> `test_only_battery_carries_a_charge_end` pins the shape of the table,
> because "the key is absent" *is* the mechanism and tidying it back in would
> re-open the footgun with every other test still green.
>
> Note that `RestoreLast` is unaffected: `last.json` *does* carry the limit,
> so a reboot restores it. That is a deliberate restore, not a profile
> applying itself.

A **saved** profile (`save-current` in either client) may carry the same
optional shape on the CPU side, and the same mechanism applies: absent means
untouched, and both keys go through `_apply_profile_dict` only, never the
re-apply snapshot.

| Key | Captured when | Applied how |
|---|---|---|
| `freq_khz` | the live all-core cap (`freq.max_khz`), if the policies agree | `freq.set_all_max`, clamped to this CPU's real bounds, with a read-back note |
| `undervolt` | only the planes with a nonzero live offset | `uv.apply(fill_missing=False)`: named planes only, so a profile cannot zero an offset it did not mention; skipped with a warning when `AllowUndervolt` is off |

---

## 8. UI structure (PySide6)

`MainWindow` is a header + sidebar + `NavStack` (a `QStackedWidget`), one page
shown at a time, each page wrapped in a scroll area:

```
Header   brand · connection pill · Refresh · Quit
Banner   shown only when the daemon is offline (read-only warning)
─────────────────────────────────────────────────────────────
Sidebar  Dashboard Performance Frequency Undervolting Fans
         Battery Profiles Settings
NavStack the visible page
Status   transient actions (left) · daemon + firmware (right, permanent)
```

| Section | Contents |
|---|---|
| Dashboard | 4 stat tiles (CPU/GPU temp, CPU/GPU fan RPM) + Battery + Right now + System |
| Performance | shift mode buttons + Cooler Boost + **Graphics mode (MUX)** + panel overdrive (model-gated) |
| Frequency | all-core max-frequency slider + four quick slots (a slot click applies immediately); values reset at reboot |
| Undervolting | five plane offsets + Apply/Reset + the permanent BIOS-unlock notice + the app-startup set |
| Fans | fan mode + draggable CPU/GPU curve canvases with factory ghost |
| Battery | charge-limit slider + 60/80/100 presets + Apply/Revert |
| Profiles | built-in + saved bundles, save/delete |
| Settings | sections on/off, daemon state, re-apply, factory capture, hardware self-test (runs `selftest`, renders the per-check lines, keeps the paste-ready markdown behind Copy), accent presets (live re-style + sidebar rebuild), full config, raw EC console |

The Frequency and Undervolting sections can be hidden from Settings
(`QSettings` keys `sections/freq`, `sections/undervolt`; a hidden section
also disappears from the sidebar and the `Ctrl+N` shortcuts skip it), for
users who do not undervolt. The undervolt startup set lives in `QSettings`
too (`undervolt/startup`) on the app side only: the daemon never persists
voltages, which is the stability design (§2 of the README's undervolting
section).

### Themes

**One token set per theme**, and every widget colour is read from it. The theme
is chosen by `MSI_THEME=light|dark`, else `styleHints().colorScheme()`, else
**dark** when the platform reports *Unknown* (which is what an offscreen run
reports, so a headless screenshot is deterministic). `apply_theme(app)` is
called from both `MainWindow.__init__` and `main()`; the smoke test never calls
`main()`, so the `__init__` call is the one that matters there.

**The accent is a token too, not a constant**: a preset in `ACCENTS` (six of
them, `ACCENT_DEFAULT = "msi-red"`) supplies `accent` + `accent_hover` per
theme, `build_theme(name, accent_id)` lays it over the base, and the rest is
derived: `on_accent` picks black or white by luminance, `accent_ink` is pushed
toward white (dark) or black (light) in fixed steps until it clears 4.5:1 on
the accent's own soft tint, because the bare accent is only ~2.6:1 there. The
choice lives in `QSettings` (`style/accent`), read in `main()` before the first
stylesheet and re-applied live by `MainWindow.set_accent()`, which also
rebuilds the sidebar: nav icons bake the accent into their selected pixmap,
and setting a stylesheet rebuilds no icons. The brand mark is painted from
`BRAND_RED` regardless, because the mark is the brand. The smoke test
measures every preset in both themes, not just the one this process wears.

* Font sizes are set in code (`setFont`), **never in QSS**: a QSS `font-size`
  silently overrides `setFont` and that is how a theme pass ends up with
  inconsistent type.
* The app stylesheet is `%`-templated, not an f-string, so literal `{`/`}`
  survive.
* `muted_color()` derives secondary text from the live palette (blend 0.6 fg →
  0.4 bg), so it is legible on both themes rather than the hardcoded grey that
  was near-invisible on light.
* The chrome is flat: header, sidebar and status bar paint the window
  background with no separator lines, and a page is a set of cards floating
  on that one surface. Both themes carry that (`bg` is one step away from
  `surface`, and the hairline `border` only exists to keep a card's edge
  findable). The sidebar marks the current section with a rounded neutral
  pill (`surface3`) plus the accent-tinted icon; there is no coloured bar,
  so the red accent keeps meaning "interactive value", never "chrome".
* Buttons are full pills (`R_BTN`, pinned as half the control rung); inputs,
  list rows and menus keep the `R_CTRL` radius; chips and tooltips use
  `R_PILL`. Card captions and the temperature-band/battery chips are
  sentence case, not upper case.

#### The tokens (`<name>_<rung>`)

| Group | Tokens | Note |
|---|---|---|
| Surfaces | `bg surface surface2 surface3 border border_strong` | |
| Text | `text text_dim muted` | `muted` is the QSS value; `muted_color()` is the live-palette one |
| Brand | `accent accent_hover accent_soft accent_ink on_accent` | MSI red. `on_accent` is **computed** by `text_on()`, never chosen by hand |
| Status | `crit crit_soft` + `COL_OK/COL_WARM/COL_HOT/COL_CRIT/COL_DEAD` | the temperature band; `temp_color()` maps a reading onto it |
| Shape | `r_card r_btn r_ctrl r_pill r_bar r_inner` | `R_CARD` 20, `R_BTN` 18 (half the 36 px control rung: buttons are full pills), `R_CTRL` 10, `R_PILL` 9, `R_BAR` 3, `R_INNER` 14 |
| Controls | `pad_ctrl_v pad_hero_v h_ctrl h_spin` | `PAD_CTRL_V` 6, `PAD_HERO_V` 12, `H_CTRL` 22, `H_SPIN` 19: one rung for every control; see below |
| Layout | `SPACE` 8, `PAGE_PAD` 24, `PAGE_PAD_V` 20, `PAGE_GAP` 16 | `page_layout()` is the only place a page's margin is written |
| Type | `TYPE_DISPLAY` 30, `TYPE_H1` 21, `TYPE_H2` 16, `TYPE_BODY` 13, `TYPE_CAPTION` 11, `TYPE_MICRO` 10 | `ui_font()`/`mono_font()`; pixels, never points |

**One control height.** `H_CTRL` + twice `PAD_CTRL_V` + the border is the
height of every button, single-line field, spin box and sidebar row in the app
- 36 px with the shipped tokens. It lives in the stylesheet, once; **no widget
sets its own `setMinimumHeight()`**, because that is exactly how battery
`Apply` ended up 10 px taller than the `60%` preset beside it and how the
sidebar, the profiles list and the raw console each disagreed with the page
they sat on. Two measured facts are load-bearing and are written down where
they are used: Qt's QSS `min-height` is the **content** box (rendered height =
token + 2·padding + 2·border), and a `QSpinBox` spends 3 px on its arrows on
top of everything else, hence `H_SPIN` being 3 less than `H_CTRL`. The
deliberate exceptions are the cooler-boost button (`[hero="true"]`, 48 px,
the tab's single big switch) and the charge slider (`H_SLIDER` 28, because a
handle needs room text does not). `ICON_SM` (16) sizes every icon in the
app, because an icon taller than a line of text silently makes its own row
taller than the ones beside it. `check_control_sizes` measures all of it.

**One meaning per colour.** The accent is the *only* "selected / this is your
value" colour; the green→amber→orange→red band belongs to temperature and
nothing else. That is why the battery tile (a percentage, not a temperature)
wears the accent, and why Cooler Boost's "on" state is the filled accent rather
than the amber it used to wear: Cooler Boost is the thing that *cools*, so
dressing it as "hot" was backwards. The two exceptions are deliberate and both
name a state the user asked about: the battery chip goes green for
charging/full and amber for "not charging" on AC (the charge limit doing its
job), and the fan curve is drawn in the accent with the *live* position marked
in its temperature colour.

`check_theme_tokens` asserts both sets define the same keys, that
`on_accent`/`accent` and `accent_ink`/`surface` clear their contrast floors, and
that every radius, padding and height token is an `<int>px` string the
stylesheet can interpolate.

### Building blocks

* `Card`: the only layout atom. `.card` is a flex column so a card's badge row
  sits on its bottom edge.
* `kv_row()`: one label/value row, styled in exactly one place. Every card
  that lists facts (Right now, System, MUX state, Daemon) used to build this by
  hand, which is how their values drifted to different weights and alignments.
  The **value** carries the stretch and right-aligns inside it; giving the
  *stretch* the slack instead hands a wrapping label its minimum width and
  breaks a short string across two lines.
* `stat_row()` / `field_label()`: the page-headline number and the caption
  above a form field, for the same reason.
* `page_layout(widget)`: the standard page frame. All six pages used to repeat
  the same three lines.
* `notice_style(colour, background)`: inline QSS for a warning strip, so the
  MUX and fan-curve warnings cannot end up with different padding or radius.
* `StatCard`: big number + bar + sub-line, identical structure for every
  reading, with a fixed-height spark slot so a tile never re-flows. The bar and
  the number may be different quantities; the sub-line names both
  (`RPM · fan 48%`). `set_chip()` clears the style as well as the text when it
  hides the chip; a hidden chip that keeps the last colour is a surprise
  waiting for the first caller that shows it.
* `CurveCanvas`: draggable step-function editor, also keyboard-operable
  (`StrongFocus`; arrows move a point, Alt+arrows walk the selection, Shift is
  the coarse step). Mouse and keyboard share one clamp.
* `shield_path()` / `brand_pixmap()` / `temp_pixmap()` / `MARK_BOX`: the app
  mark: **one centred red shield and nothing else**, deliberately. The first
  cut carried MSI's badge literally (dark rounded tile, shield, `66`) and at
  the 16-22 px a taskbar renders it is a dark square with an unreadable smudge
  in it, so the tile and the lettering are gone and the shield is drawn as
  large as the canvas allows. One `_mark_frame()` and one `_mark_shield()`
  gradient feed the window icon, the header mark and the tray, so they cannot
  become three different marks; `temp_pixmap()` is the same shield tinted by
  the hottest reading, with that reading painted on it: the only text any mark
  carries, sized by `tray_digit_font()` so `100` still fits inside the badge.
  Its `mode` picks the ink: `"colour"` is the band tint with a contrast-colour
  number, while `"white"` and `"black"` are a flat monochrome shield for a
  themed panel. Settings chooses it and `QSettings` remembers it, because it is
  a property of this window rather than of the EC. `check_tray_icon` samples
  the painted pixels of all three, since a mode that quietly fell back to the
  tinted shield would still look like a menu that worked. Beside the icon
  sits a menu with the everyday switches: show the window, the four
  performance modes as radio actions, Cooler Boost, the 60/80/100 charge
  limits, and a CPU frequency cap submenu holding the Frequency page's
  four quick slots (read from the page's own QSettings keys, so the tray
  can never disagree with the page about what slot 1 holds, re-labelled on
  every poll, empty slots hidden) plus a "Full speed" action that re-posts
  the hardware maximum. It is built even where no system tray exists
  (a WM without one, the offscreen platform) so the mirror logic is
  exercised everywhere, and every entry routes through the same
  `submit -> daemon -> validated write` path as the window's own buttons;
  `_update_tray_menu()` flips
  the checks from the latest status poll and never writes anything itself.
  `check_tray_menu` pins the current state, the modes hidden where the
  firmware lacks them, the slot submenu's labels, checks and exact command
  payloads, and that everything goes dead when the daemon is offline.
* `Banner`, `Header`, `Sidebar`, `NavStack` as above. `NavStack` keeps the old
  `addTab`/`count`/`currentIndex`/`tabText` surface so the smoke test and the
  geometry/tab persistence were untouched by the move away from `QTabWidget`.

`POLL_MS = 1500` while a window is on screen and `POLL_MS_HIDDEN = 10000`
once it is hidden to the tray (each is re-armed in `showEvent`/`hideEvent`,
and `_show_window()` fires one immediate poll so a reopened window is never
showing a stale reading). The slower hidden cadence is deliberate: one
status is a full hardware read-back (~31 EC-byte transfers, ~160 OC-mailbox
MSR operations with five planes across every logical CPU, ~50 sysfs reads),
which is exactly what a window on screen wants every 1.5 s, and far too much
for keeping a tray badge fresh (measured on the 10875H dev machine, one
status is ~40 ms end to end, mostly serialized EC and mailbox time, plus a
thread per request). `mark_checked()` gives every mode button
(shift, fan, graphics) the same unmistakable selected state: the accent fills
the button and the label goes bold, because Breeze's checked state is a few
percent off the unchecked one and these are read at a glance from a laptop
screen at an angle.

`InstanceGuard` (wired up in `main()`, before any window is built) is the
one-copy-per-user rule: a `QLocalServer` named with the UID is the
rendezvous, a second launch probes it, asks the running copy to show its
window and exits 0. A socket file left by a crashed copy is removed only
after the probe found nobody listening, so a live one is never unplugged,
and the guard hands over in `main()` so a rejected launch never even
constructs a `MainWindow`. A `--hidden` launch (see below) hands over
without the show request.

The Startup card in Settings is the same feature's session half. The
autostart helpers (`autostart_write`, `autostart_disable`,
`autostart_state`) manage `~/.config/autostart/msi-control.desktop`, with
`XDG_CONFIG_HOME` honoured so the smoke suite's scratch directory keeps the
tests off the real one; the file's Exec line, not a QSettings shadow,
carries the `--hidden` choice, so the desktop and the checkboxes cannot
drift apart. `parse_args` owns `--hidden`, and `main()` starts hidden only
when a tray exists (a tray-less hidden start is an unreachable app), at
`POLL_MS_HIDDEN` because a window that was never shown never received a
`hideEvent`.

The status bar carries the daemon readout **permanently**, on the right:
`msi-controld · fw 16V1EMS1.118`, or, when the daemon is down, a red
`offline` note carrying the reason. It used to be pushed as a transient
message on every poll, and the poll is 1.5 s, so an action's confirmation was
wiped off the bar within a second and a half of appearing.

**The graphics card always states the reason a control is dead**: offline,
`AllowMuxSwitch` off, or unsupported firmware each get their own sentence. A
greyed-out button with no explanation reads as a bug, and this is the one
control where "why can't I click this" is a genuinely important question.

**With the daemon down, every writer goes dead**: shift/fan buttons, Cooler
Boost, the MUX switch, Apply/Revert on Battery and Fans, Profiles Save/Delete,
Settings Re-apply/Capture, the Frequency page's Apply and slot buttons, the
undervolt Apply/Reset, and the overdrive toggle. The banner already says the
app is read-only; a
control that stayed live and answered with a `✗` on click would contradict it.
`check_offline_disables_writes` and the graphics/battery checks pin this.

Palette lives in one place, `temp_color()`, so "hot" means the same thing on
every widget: `<50` ok, `<70` warm, `<85` hot, else critical.

### Deferred writes

Two editors hold a pending value and write nothing until **Apply**, because
both had a live-write bug:

* **Battery**: the slider used to write on release, so a slow drag programmed
  the charger several times. `Apply 80%` / `Revert`, presets select only, and
  the tab states `60% applied · 80% pending`.
* **Fans**: `refresh()` used to adopt the EC's curve every poll, so a dragged
  point was overwritten before it could be applied. An `_unsaved` flag per fan
  (cleared only when the EC matches the editor) gates adoption; Apply is
  enabled only when valid **and** dirty **and** online.

The Frequency slider follows the same pending pattern (a slow drag must not
re-cap the cores on every step); its four quick slots are `QSettings`
(`freq/slot1..4`), storing the cap *number* only, so they survive restarts
without ever becoming a daemon-side persisted state. Dragging stays pending,
but clicking a stored slot skips the queue and applies at once: a stored
value is a decision, not a draft.

---

## 9. Tests

**`tests/test_msi_control.py`: 194 tests, offline, unprivileged, ~9 s.**

Runs the *real* daemon (Service + Unix socket server + protocol), the real
validation and the real CLI as a subprocess, against a fake sysfs tree
(cpufreq policies included) and a RAM-backed EC (`FakeEc`);
`MsiEc._xfer_read/_xfer_write` are monkeypatched. The undervolt tests patch
`Undervolt._wr/_rd` onto a `FakeMsr` that decodes the OC-mailbox command
words, so per-plane read-backs, the lock-detector refusal and the
shallower-value sorter are all exercised; the overdrive tests patch
`core.MODEL`/`core.OVERDRIVE_REGS` to a model with `ec.overdrive` and prove
the one-bit RMW preserves the webcam and MUX bits of `0x2e`.
`MSI_MODELS` points at the repo's `models/` and `MSI_MODEL` pins the GS66 entry,
so the suite is deterministic on any machine; the model-registry tests
(`TestRegistry`, `TestModelSelection`, `TestDetectAndDraft`,
`TestUnsupportedModelGating`) cover validation errors, match scoring, the
detect/draft round-trip, and that an unsupported model refuses exactly the
register-backed features and keeps the driver ones.

```bash
python3 tests/test_msi_control.py
```

**`tests/gui_smoke.py`**: headless GUI construction + one PNG per section:

```bash
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py   # -> tests/shots/
MSI_THEME=light QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py "$PWD/tests/shots/light"
```

> Pass an **absolute** outdir for the light run. A relative one makes Qt ignore
> the redirected `XDG_CONFIG_HOME`, and the run then writes the developer's
> real `~/.config/msi-control/msi-control.conf`.

Beyond rendering, it drives behaviour a screenshot cannot show: contrast
ratios, the battery's pending-vs-applied state machine, keyboard access to the
curve editor, the wording of every confirmation, the dashboard's numbers
against their labels, all four graphics-card states, and that every daemon
writer goes dead when the daemon is offline.

It also pins the things that are invisible by construction:

| Check | What it would otherwise allow |
|---|---|
| `check_theme_tokens` | a theme that silently lost a token (the stylesheet interpolates an empty string, so a widget just loses its border) or an accent whose ink fails AA |
| `check_brand_mark` | `icons/msi-control.svg` drifting from the mark the GUI paints, measured by rasterising both and comparing the ink boxes. Also pins that the corners are still transparent (the tile has not crept back) and that the tray's `9` / `62` / `100` fit inside the badge |
| `check_tray_icon` | a tray ink mode that quietly did nothing: it samples the mean brightness of the colour, white and black marks (white must be brighter than the band tint, which must be brighter than black), pins that the colour mode still follows the band, and that the Settings controls mirror the window instead of keeping a second copy |
| `check_tray_menu` | a tray action that flips a state nobody rendered, a mode the firmware does not have still showing as clickable, a frequency slot that is stored but absent (or labelled with another value than the page holds), or the whole menu pretending to work while the daemon is offline |
| `check_selftest_card` | a self-test reply rendered as an unclear dump, the Copy button handing over the rendered text instead of the PR markdown, or the run button left disabled forever after an error |
| `check_accent_switch` | an accent preset that changed the button's look but nothing else: the click must land in `THEME`, in the app stylesheet, in `QSettings`, and rebuild the sidebar (its icons bake the accent in), and an unknown id must change nothing |
| `check_theme_tokens` (accent half) | a preset that fails WCAG AA in either theme: every preset is measured in both, not only the one this process happens to wear |
| `check_glyphs` | a typo in a sidebar page's `glyph` name: `glyph_pixmap` falls through its if-chain and returns a transparent pixmap, so the sidebar shows a label with a hole beside it |
| `check_nav_matches_tabs` | a section with no nav entry (unreachable) or a nav entry with no section (a dead click) |
| `check_type_scale` | the five dashboard tiles disagreeing about `TYPE_DISPLAY`, or a card caption that is not `TYPE_CAPTION` bold |
| `check_control_sizes` | one control that is 10 px taller than its neighbour, the battery `Apply` bug, which is invisible as a bug in a screenshot because it just looks like the button is emphasised |
| the battery-chip cases in `check_dashboard_facts` | a chip colour policy that is a decision, not a reading, pinned so it cannot quietly change |

**`make check`** runs the offline suite, the GUI smoke test and a syntax check
together. `Makefile` also has `test`, `smoke`, `lint`, `verify`, `install`,
`uninstall`, `clean`, and the release pair: `tarball`
(`dist/msi-control-<VERSION>.tar.gz`) and `rpm` (three rpms plus a
`src.rpm` in `dist/`, built from that tarball and
`packaging/rpm/msi-control.spec`, whose `@VERSION@` placeholder the target
substitutes from `VERSION` in `msi_control.py`; `release` = both). The
hidden variable is `$(VERSION)`: nothing else carries a version string, so
bumping it there is the whole release-versioning job.

Add tests for anything new here; they run in seconds and cost nothing.

---

## 10. Hardware gotchas (real ones, already paid for)

1. **`pkexec` sanitises `PATH` and drops `/usr/local/bin`**: any script run
   via pkexec that shells out to `msi-control` dies with `FileNotFoundError`.
   Use: `pkexec env PATH=/usr/local/bin:/usr/bin:/bin python3 …`
2. **`pkexec` needs an absolute path.** A relative `$0` fails; resolve with
   `SELF="$(readlink -f "${BASH_SOURCE[0]}")"`.
3. **Never run `termux`-style pattern kills.** Not applicable here, but the
   general rule stands: get PIDs from `ps -eo pid,args`, never `pgrep -f`
   (it self-matches).
4. **There is one camera/one EC**: no concurrent sessions fighting over it.
5. **`/tmp/opencode/` is wiped by the overnight cleanup.** Anything worth
   keeping must live in the project (`tools/`), which is why the probe
   scripts were moved there.
6. **`debug/ec_dump` costs ~120 ms per read.** It looks like a cheap file read
   and is not: the driver performs 256 EC transfers to build it. Never put it
   on a poll path; read single registers through `ec_get` instead, and let
   the *daemon* do that (only it can, the address write is root-only).
7. **Read the EC, not a userspace proxy.** The firmware's numbers are already
   in registers (`0x68`/`0x80` for temps, `0x71`/`0x89` for fan duty,
   `0xcc`/`0xca` for tachometers); anything that shells out to a monitoring
   daemon adds its own refresh lag on top. That is why the CLI and GUI pull
   from `msi-controld`.
8. **Cooler Boost does not update the duty registers.** Measured 2026-10-16:
   with boost on, `0x71`/`0x89` stayed at `70` while the tachometers read
   **~8100 RPM**. So `fan_pct` is "the curve's request", not "what the fan is
   doing", and a UI that shows fan % alone will under-report a boosted machine
   - which is why the dashboard shows **RPM** next to it. Do not "fix" the
   duty read; it is not wrong, it is a different fact.
9. **`0xc8` is not a fan.** It was suspected to be the MSI's third fan
   (LibreHardwareMonitor reports 3 on this model). It is not: under Cooler
   Boost `0xca`/`0xcc` jump from ~3700 to ~8100 while `0xc8` sits at ~1900.
   `msi-ec`'s `CONF_G1_3` maps exactly two fans (`cpu` 0x71, `gpu` 0x89) and
   mentions no third. **No third-fan reading is exposed**, because an
   unidentified register painted as a fan is a guess wearing a fact's clothes.
   (CORRECTION to an earlier note in `TODO.md`: the "`0xc8` never moves"
   observation was a period-versus-RPM misreading: `0xc8` reads `89`, which
   as a *period* is ~5370 RPM, and 89 is not an RPM at all. The register does
   move; it just does not follow the fans.)

---

## 11. Project layout

```
MSI-Linux-Control/
├── src/            msi_control.py · msi_controld.py · msi-control · msi-control-gui
├── models/         <id>/model.json: the per-model registry (§2a)
│                   README.md (format) · gs66-stealth-10se/ (worked example)
├── tests/          test_msi_control.py · gui_smoke.py · shots/ (dark + light/)
├── tools/          hardware probes (need root); see TODO §third-fan
│                   ecdump.py · ecprobe.py · fanprobe.py · scan_ec.py
│                   fixup.sh · recapture.sh
├── systemd/        msi-controld.service · msi-control-sleep
├── modprobe/       msi-ec.conf
├── desktop/        msi-control.desktop
├── icons/          msi-control.svg: the red shield + Tux mark, drawn for the
│                   installed icon and mirrored by brand_pixmap()/MARK_BOX
├── etc/            config.json (the default install.sh copies in)
├── third_party/    msi-ec/   vendored teackot driver v0.13
├── install.sh      privileged install (DKMS + deploy + services + models/);
│                   retires an old gs66-center install and keeps its profiles
├── uninstall.sh    clean removal
├── verify.sh       live hardware verification: every shift/fan/charge
│                   mode, cooler boost, fan-curve write-back round-trip
├── Makefile        check / test / smoke / lint / verify / install / clean /
│                   tarball / rpm / release (artifacts land in dist/)
├── packaging/rpm/  msi-control.spec (three packages; @VERSION@ placeholder)
├── README.md       full research write-up + EC map + safety docs
├── CONTRIBUTING.md how to work here (tests, style, adding your model, PR flow)
├── AGENTS.md       the same map for an AI agent: model procedure §6
├── CHANGELOG.md    dated passes, newest first
├── TODO.md         what is left to do
└── APP_DETAILS.md  this file
```

---

## 12. Working agreements

* **Back up before changing** anything on the device: Tier B style copies
  into the project or `~/backup-pre-upgrade-*/`.
* **Run the test suite after every change.** `python3 tests/test_msi_control.py`.
* **`verify.sh` must report ALL CHECKS PASSED** before a change counts as
  done on real hardware.
* **Report the PySide6 substitution** in the final summary: the user asked
  for PyQt6.
* **EC writes are the danger zone.** Anything that pokes bytes outside the
  documented map goes in `tools/` as an explicit probe, never into the
  daemon's normal path.
