# APP_DETAILS — GS66 Control Center

Everything the next session needs to know about this project, without
re-deriving it. Companion to [`TODO.md`](TODO.md).

---

## 1. What this is

A Linux control centre for the **MSI GS66 Stealth 10SE** laptop, replacing
MSI Dragon Center: battery charge thresholds, fan speeds / modes / curves,
performance (shift) modes, Cooler Boost, live monitoring.

| Fact | Value |
|---|---|
| Target machine | MSI GS66 Stealth 10SE — the dev machine itself |
| EC firmware | `16V1EMS1.118` |
| BIOS | `E16V1IMS.112` |
| OS | Fedora 44, kernel `7.2.9-201.fc44.x86_64`, KDE Plasma 6 (Wayland) |
| User | `eren`, uid 1000, in `wheel` |
| Secure Boot | **disabled** (out-of-tree module requires this) |
| Project root | `~/Projects/GS66Control` (moved from `~/gs66-center` on 2026-10-14) |

**GUI toolkit: PySide6 6.11.2** — the user asked for PyQt6; PySide6 was
substituted because it was already installed. The difference is licensing
(LGPL vs GPL) and the import package name; the API is near-identical.
**This substitution must be reported in the final summary to the user.**

---

## 2. Architecture

Three programs, one shared module. The daemon owns *every* EC write; the CLI
and GUI are unprivileged clients.

```
   gs66-gui (PySide6)  ─┐
                        ├── JSON over Unix socket ──> gs66d (root, systemd)
   gs66-center (CLI)  ──┘                                  │
                                                           ▼
                                    msi-ec kernel driver sysfs + debug iface
```

* **`src/gs66d.py`** — root daemon, systemd system service `gs66-centerd`.
  One JSON object per line, one request per connection. Validates every
  client value, serialises all EC access under one lock, and **verifies each
  write by reading it back**.
* **`src/gs66-center`** — stdlib-only CLI. Reads work without the daemon
  (plain sysfs); writes need it.
* **`src/gs66-gui`** — PySide6 GUI, six sections. Never privileged, never
  touches sysfs. All daemon traffic runs on one worker thread.
* **`src/gs66_common.py`** — the EC register map, validation, protocol and
  paths. Single source of truth so daemon/CLI/GUI can never disagree.

### Design rules (enforced, not aspirational)

1. Never trust a client — everything is validated in `gs66_common`.
2. Every EC write is followed by a read-back. An EC that silently ignores a
   byte is indistinguishable from one that accepted it.
3. Nothing is written optimistically in the UI — the GUI re-reads state after
   each command, so what you see is what the EC actually holds.
4. With the daemon down the GUI/CLI degrade to **read-only** instead of lying.
5. stdlib-only for daemon and CLI — they must work in a recovery shell.

### Environment overrides (testability)

`GS66_SYSFS`, `GS66_SOCK`, `GS66_CONFIG`, `GS66_STATE` — let the whole stack
run against a fake sysfs tree and a RAM-backed EC with no root at all.

---

## 3. EC register map

Cross-checked against YoyPa/isw's `[16V1EMS1]` section + `MSI_ADDRESS_DEFAULT`
(which carries a real EC dump from a GS66-10SE), teackot/msi-ec `CONF_G1_3`,
and the in-tree `drivers/platform/x86/msi-ec.c` charge semantics.

| Purpose | Register(s) |
|---|---|
| Performance ("shift") mode | `0xf2` — eco `0xc2`, comfort `0xc1`, sport `0xc0`, turbo `0xc4` |
| Fan mode | `0xf4` — auto `0x0d`, silent `0x1d`, advanced `0x8d` |
| Cooler Boost | `0x98` bit 7 |
| Charge threshold | `0xef` — `end = raw − 0x80`, `start = end − 10`, range 20–100 |
| CPU temp / fan % | `0x68` / `0x71` |
| GPU temp / fan % | `0x80` / `0x89` |
| CPU curve | temps `0x6a–0x6f`, speeds `0x72–0x78` |
| GPU curve | temps `0x82–0x87`, speeds `0x8a–0x90` |
| Tachometers | `0xcc` (CPU), `0xca` (GPU) — 16-bit big-endian period |
| **Graphics mode (MUX)** | `0x2e` bit 6 (1 = discrete) · `0xd1` bits 0–1 (trigger) · UEFI `MsiDCVarData` byte 1 |

Curve semantics: **6 temperature thresholds, 7 speed levels**. The fan spins
at `speeds[0]` until the temperature passes `temps[0]`, then `speeds[1]` …
`speeds[6]`. Validation requires temps strictly increasing, speeds
non-decreasing.

> 🔢 **RPM = `478000 // period`** — reproduced verbatim from isw. A wrong
> constant silently prints plausible-looking nonsense. **Confirmed against
> hardware 2026-10-16**: Cooler Boost drives `0xcc` from ~3700 to **~8100**,
> which is the top of this laptop's fan range, so the constant is right and
> the scale is real.

### The MUX registers are *not* in the driver's map

Everything above comes from `msi-ec`'s `CONF_G1_3` or from isw. The graphics
mode does not: **`msi-ec` has no MUX support of any kind**, so the three
addresses below are reverse-engineered and cross-checked against
[`ElXreno/msi-gpu-switcher`](https://github.com/ElXreno/msi-gpu-switcher)
(which drives the identical trio on an AMD MSI Alpha 17 C7VG):

| Where | What it holds |
|---|---|
| EC `0x2e` bit 6 | MSI's "PXCT" bit — 1 = panel on the dGPU, 0 = hybrid |
| EC `0xd1` bits 0–1 | the "apply at next POST" trigger; write `(v & ~0x03) \| 0x01` |
| UEFI `MsiDCVarData` byte 1 | the firmware reads this at POST — 1 = discrete, 0 = hybrid |

`0x2e` also carries the **webcam at bit 1**, which is exactly why every write
here is a read-modify-write of one bit rather than a whole-byte poke.

Write order follows the reference tool: **UEFI variable → trigger → EC bit**.
The daemon then re-reads `requested` and raises rather than reporting a switch
that did not stick.

The `MsiDCVarData` variable is `state: 7` (non-volatile, boot-service,
runtime-accessible) and normally **immutable** (`lsattr` →
`----i-----------------`). `MsiEc._efivar_write()` therefore clears
`FS_IMMUTABLE_FL` with an `fcntl` ioctl, does a **single** `os.write()` of the
4 attribute bytes plus the payload (efivarfs refuses a split write), and
restores the flag in a `finally`. No `chattr`, no subprocess — the daemon
spawns nothing.

Reading the mode needs no privileges at all except for the EC bit:
`/sys/firmware/efi/efivars/…` and `/sys/bus/pci/devices/*/vendor` are both
world-readable, which is how `gs66-center status` still reports the mode with
the daemon down. **`debug/ec_dump` is deliberately not used for this** — it is
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

---

## 4. Install layout

| Installed path | Source |
|---|---|
| `/usr/local/lib/gs66-center/` | `gs66_common.py`, `gs66d.py` |
| `/usr/local/bin/gs66-center` | `src/gs66-center` |
| `/usr/local/bin/gs66-gui` | `src/gs66-gui` |
| `/etc/gs66-center/config.json` | created by install.sh if absent |
| `/var/lib/gs66-center/` | state: `last.json`, `profiles/*.json` |
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
`/usr/local/lib/gs66-center`, so `dirname(__file__)` alone is not enough.
Without it the installed CLI/GUI dies with `ModuleNotFoundError:
gs66_common`. It searches the script dir, `../lib/gs66-center`, then the two
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
{"cmd":"capture-factory"}
{"cmd":"raw","addr":242,"val":13}                   // gated by AllowRaw
{"cmd":"graphics"}                                  // show the MUX state
{"cmd":"graphics","action":"set","mode":"hybrid"}    // gated by AllowMuxSwitch
{"cmd":"graphics","action":"revert"}                 // undo from the saved backup
```

Response is always `{"ok":true,"data":{…}}` or `{"ok":false,"error":"…"}`.

Socket is `/run/gs66-centerd.sock`, mode `0660`, owner `root:<SocketGroup>`
(default `wheel`) — so socket access grants nothing its holder could not
already get.

---

## 6. Config — `/etc/gs66-center/config.json`

| Key | Default | Meaning |
|---|---|---|
| `RestoreLast` | `true` | re-apply last known state at daemon start (boot) |
| `DefaultProfile` | `null` | explicit profile at boot; wins over `RestoreLast` |
| `AllowRaw` | `false` | expose `gs66-center raw` — an unvalidated EC byte is the one way this program can hurt you |
| `AllowMuxSwitch` | `false` | allow the graphics-mode (MUX) switch | 
| `SocketGroup` | `wheel` | group allowed to talk to the socket |
| `ReapplyOnResume` | `true` | re-apply after S3, which sometimes resets the EC |

A broken/absent config never stops the daemon — defaults are safe.

`AllowMuxSwitch` is off for a *different* reason from `AllowRaw`: the switch is
fully validated, but it changes what the firmware does at the next POST, and on
a model whose MUX was never reverse-engineered that can leave the machine with
no display. Requiring a config edit makes it a deliberate act rather than a
stray click, and it is the same "make the dangerous thing explicit" pattern as
`AllowRaw`. It is **never** applied automatically: not by `RestoreLast`, not by
`ReapplyOnResume`, not by any profile — see the comment on
`Service.snapshot()`, and `test_only_battery_carries_a_charge_end`'s sibling
rule for graphics.

---

## 7. Built-in profiles

`silent`, `balanced`, `gaming`, `battery`. Plus **`factory`**, which is *not*
hard-coded: it is captured from the EC on first run so the original firmware
curve can always be restored. The GUI fetches it via the **read-only**
`profile show` — never `apply`, which would push settings into the EC as a
side effect.

| Profile | shift | fan | cooler boost | curves | `charge_end` |
|---|---|---|---|---|---|
| `silent` | eco | silent | off | — | *absent* |
| `balanced` | comfort | auto | off | — | *absent* |
| `gaming` | sport | advanced | off | cpu + gpu | *absent* |
| `battery` | eco | auto | off | — | **60** |
| `factory` | captured | captured | captured | captured | captured if readable |

> ⚠️ **`charge_end` is optional in a profile, on purpose.** Absent means
> *"leave the charge limit alone"*, and `_apply_profile_dict` skips the write
> entirely (`if prof.get("charge_end") is not None`). A charge limit is a
> battery-health choice rather than a performance one, so only `battery` —
> whose whole purpose is battery preservation — carries a value. The three
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

---

## 8. UI structure (PySide6)

`MainWindow` is a header + sidebar + `NavStack` (a `QStackedWidget`), one page
shown at a time, each page wrapped in a scroll area:

```
Header   brand · connection pill · Refresh · Quit
Banner   shown only when the daemon is offline (read-only warning)
─────────────────────────────────────────────────────────────
Sidebar  Dashboard Performance Fans Battery Profiles Settings
NavStack the visible page
Status   transient actions (left) · daemon + firmware (right, permanent)
```

| Section | Contents |
|---|---|
| Dashboard | 4 stat tiles (CPU/GPU temp, CPU/GPU fan RPM) + Battery + Right now + System |
| Performance | shift mode buttons + Cooler Boost + **Graphics mode (MUX)** |
| Fans | fan mode + draggable CPU/GPU curve canvases with factory ghost |
| Battery | charge-limit slider + 60/80/100 presets + Apply/Revert |
| Profiles | built-in + saved bundles, save/delete |
| Settings | daemon state, re-apply, factory capture, full config, raw EC console |

### Themes

**One token set per theme**, and every widget colour is read from it. The theme
is chosen by `GS66_THEME=light|dark`, else `styleHints().colorScheme()`, else
**dark** when the platform reports *Unknown* (which is what an offscreen run
reports — so a headless screenshot is deterministic). `apply_theme(app)` is
called from both `MainWindow.__init__` and `main()`; the smoke test never calls
`main()`, so the `__init__` call is the one that matters there.

* Font sizes are set in code (`setFont`), **never in QSS** — a QSS `font-size`
  silently overrides `setFont` and that is how a theme pass ends up with
  inconsistent type.
* The app stylesheet is `%`-templated, not an f-string, so literal `{`/`}`
  survive.
* `muted_color()` derives secondary text from the live palette (blend 0.6 fg →
  0.4 bg), so it is legible on both themes rather than the hardcoded grey that
  was near-invisible on light.

#### The tokens (`<name>_<rung>`)

| Group | Tokens | Note |
|---|---|---|
| Surfaces | `bg surface surface2 surface3 border border_strong` | |
| Text | `text text_dim muted` | `muted` is the QSS value; `muted_color()` is the live-palette one |
| Brand | `accent accent_hover accent_soft accent_ink accent_line on_accent` | MSI red. `on_accent` is **computed** by `text_on()`, never chosen by hand |
| Status | `crit crit_soft` + `COL_OK/COL_WARM/COL_HOT/COL_CRIT/COL_DEAD` | the temperature band; `temp_color()` maps a reading onto it |
| Shape | `r_card r_ctrl r_pill r_bar r_inner` | `R_CARD` 16, `R_CTRL` 10, `R_PILL` 8, `R_BAR` 5, `R_INNER` 12 |
| Controls | `pad_ctrl_v pad_hero_v h_ctrl h_spin` | `PAD_CTRL_V` 6, `PAD_HERO_V` 12, `H_CTRL` 22, `H_SPIN` 19 — one rung for every control; see below |
| Layout | `SPACE` 8, `PAGE_PAD` 20, `PAGE_PAD_V` 18, `PAGE_GAP` 14 | `page_layout()` is the only place a page's margin is written |
| Type | `TYPE_DISPLAY` 30, `TYPE_H1` 21, `TYPE_H2` 16, `TYPE_BODY` 13, `TYPE_CAPTION` 11, `TYPE_MICRO` 10 | `ui_font()`/`mono_font()`; pixels, never points |

**One control height.** `H_CTRL` + twice `PAD_CTRL_V` + the border is the
height of every button, single-line field, spin box and sidebar row in the app
— 36 px with the shipped tokens. It lives in the stylesheet, once; **no widget
sets its own `setMinimumHeight()`**, because that is exactly how battery
`Apply` ended up 10 px taller than the `60%` preset beside it and how the
sidebar, the profiles list and the raw console each disagreed with the page
they sat on. Two measured facts are load-bearing and are written down where
they are used: Qt's QSS `min-height` is the **content** box (rendered height =
token + 2·padding + 2·border), and a `QSpinBox` spends 3 px on its arrows on
top of everything else, hence `H_SPIN` being 3 less than `H_CTRL`. The
deliberate exceptions are the cooler-boost button (`[hero="true"]`, 48 px — the
tab's single big switch) and the charge slider (`H_SLIDER` 28 — a handle needs
room text does not). `ICON_SM` (16) sizes every icon in the app, because an
icon taller than a line of text silently makes its own row taller than the ones
beside it. `check_control_sizes` measures all of it.

**One meaning per colour.** The accent is the *only* "selected / this is your
value" colour; the green→amber→orange→red band belongs to temperature and
nothing else. That is why the battery tile — a percentage, not a temperature —
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

* `Card` — the only layout atom. `.card` is a flex column so a card's badge row
  sits on its bottom edge.
* `kv_row()` — one label/value row, styled in exactly one place. Every card
  that lists facts (Right now, System, MUX state, Daemon) used to build this by
  hand, which is how their values drifted to different weights and alignments.
  The **value** carries the stretch and right-aligns inside it; giving the
  *stretch* the slack instead hands a wrapping label its minimum width and
  breaks a short string across two lines.
* `stat_row()` / `field_label()` — the page-headline number and the caption
  above a form field, for the same reason.
* `page_layout(widget)` — the standard page frame. All six pages used to repeat
  the same three lines.
* `notice_style(colour, background)` — inline QSS for a warning strip, so the
  MUX and fan-curve warnings cannot end up with different padding or radius.
* `StatCard` — big number + bar + sub-line, identical structure for every
  reading, with a fixed-height spark slot so a tile never re-flows. The bar and
  the number may be different quantities; the sub-line names both
  (`RPM · fan 48%`). `set_chip()` clears the style as well as the text when it
  hides the chip — a hidden chip that keeps the last colour is a surprise
  waiting for the first caller that shows it.
* `CurveCanvas` — draggable step-function editor, also keyboard-operable
  (`StrongFocus`; arrows move a point, Alt+arrows walk the selection, Shift is
  the coarse step). Mouse and keyboard share one clamp.
* `shield_path()` / `brand_pixmap()` / `temp_pixmap()` / `MARK_BOX` — the app
  mark: **one centred red shield and nothing else**, deliberately. The first
  cut carried MSI's badge literally (dark rounded tile, shield, `66`) and at
  the 16–22 px a taskbar renders it is a dark square with an unreadable smudge
  in it, so the tile and the lettering are gone and the shield is drawn as
  large as the canvas allows. One `_mark_frame()` and one `_mark_shield()`
  gradient feed the window icon, the header mark and the tray, so they cannot
  become three different marks; `temp_pixmap()` is the same shield tinted by
  the hottest reading, with that reading painted on it — the only text any mark
  carries, sized by `tray_digit_font()` so `100` still fits inside the badge.
* `Banner`, `Header`, `Sidebar`, `NavStack` as above. `NavStack` keeps the old
  `addTab`/`count`/`currentIndex`/`tabText` surface so the smoke test and the
  geometry/tab persistence were untouched by the move away from `QTabWidget`.

`POLL_MS = 1500`. `mark_checked()` gives every mode button (shift, fan,
graphics) the same unmistakable selected state — the accent fills the button
and the label goes bold, because Breeze's checked state is a few percent off
the unchecked one and these are read at a glance from a laptop screen at an
angle.

The status bar carries the daemon readout **permanently**, on the right
(`gs66-centerd · fw 16V1EMS1.118`, or `offline — read-only (…)` in red). It
used to be pushed as a transient message on every poll — and the poll is 1.5 s
— so an action's confirmation was wiped off the bar within a second and a half
of appearing.

**The graphics card always states the reason a control is dead** — offline,
`AllowMuxSwitch` off, or unsupported firmware each get their own sentence. A
greyed-out button with no explanation reads as a bug, and this is the one
control where "why can't I click this" is a genuinely important question.

**With the daemon down, every writer goes dead** — shift/fan buttons, Cooler
Boost, the MUX switch, Apply/Revert on Battery and Fans, Profiles Save/Delete,
and Settings Re-apply/Capture. The banner already says the app is read-only; a
control that stayed live and answered with a `✗` on click would contradict it.
`check_offline_disables_writes` and the graphics/battery checks pin this.

Palette lives in one place — `temp_color()` so "hot" means the same thing on
every widget: `<50` ok, `<70` warm, `<85` hot, else critical.

### Deferred writes

Two editors hold a pending value and write nothing until **Apply**, because
both had a live-write bug:

* **Battery** — the slider used to write on release, so a slow drag programmed
  the charger several times. `Apply 80%` / `Revert`, presets select only, and
  the tab states `60% applied · 80% pending`.
* **Fans** — `refresh()` used to adopt the EC's curve every poll, so a dragged
  point was overwritten before it could be applied. An `_unsaved` flag per fan
  (cleared only when the EC matches the editor) gates adoption; Apply is
  enabled only when valid **and** dirty **and** online.

---

## 9. Tests

**`tests/test_gs66.py` — 114 tests, offline, unprivileged, ~6 s.**

Runs the *real* daemon (Service + Unix socket server + protocol), the real
validation and the real CLI as a subprocess, against a fake sysfs tree and a
RAM-backed EC (`FakeEc`). `MsiEc._xfer_read/_xfer_write` are monkeypatched.

```bash
python3 tests/test_gs66.py
```

**`tests/gui_smoke.py`** — headless GUI construction + one PNG per section:

```bash
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py   # -> tests/shots/
GS66_THEME=light QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py "$PWD/tests/shots/light"
```

> Pass an **absolute** outdir for the light run. A relative one makes Qt ignore
> the redirected `XDG_CONFIG_HOME`, and the run then writes the developer's
> real `~/.config/gs66-center/gs66-center.conf`.

Beyond rendering, it drives behaviour a screenshot cannot show: contrast
ratios, the battery's pending-vs-applied state machine, keyboard access to the
curve editor, the wording of every confirmation, the dashboard's numbers
against their labels, all four graphics-card states, and that every daemon
writer goes dead when the daemon is offline.

It also pins the things that are invisible by construction:

| Check | What it would otherwise allow |
|---|---|
| `check_theme_tokens` | a theme that silently lost a token (the stylesheet interpolates an empty string, so a widget just loses its border) or an accent whose ink fails AA |
| `check_brand_mark` | `icons/gs66-center.svg` drifting from the mark the GUI paints — measured, by rasterising both and comparing the ink boxes. Also pins that the corners are still transparent (the tile has not crept back) and that the tray's `9` / `62` / `100` fit inside the badge |
| `check_glyphs` | a typo in `Sidebar.ITEMS`: `glyph_pixmap` falls through its if-chain and returns a transparent pixmap, so the sidebar shows a label with a hole beside it |
| `check_nav_matches_tabs` | a section with no nav entry (unreachable) or a nav entry with no section (a dead click) |
| `check_type_scale` | the five dashboard tiles disagreeing about `TYPE_DISPLAY`, or a card caption that is not `TYPE_CAPTION` bold |
| `check_control_sizes` | one control that is 10 px taller than its neighbour — the battery `Apply` bug, which is invisible as a bug in a screenshot because it just looks like the button is emphasised |
| the battery-chip cases in `check_dashboard_facts` | a chip colour policy that is a decision, not a reading — pinned so it cannot quietly change |

**`make check`** runs the offline suite, the GUI smoke test and a syntax check
together. `Makefile` also has `test`, `smoke`, `lint`, `verify`, `install`,
`uninstall` and `clean`.

Add tests for anything new here; they run in seconds and cost nothing.

---

## 10. Hardware gotchas (real ones, already paid for)

1. **`pkexec` sanitises `PATH` and drops `/usr/local/bin`** — any script run
   via pkexec that shells out to `gs66-center` dies with `FileNotFoundError`.
   Use: `pkexec env PATH=/usr/local/bin:/usr/bin:/bin python3 …`
2. **`pkexec` needs an absolute path.** A relative `$0` fails; resolve with
   `SELF="$(readlink -f "${BASH_SOURCE[0]}")"`.
3. **Never run `termux`-style pattern kills.** Not applicable here, but the
   general rule stands: get PIDs from `ps -eo pid,args`, never `pgrep -f`
   (it self-matches).
4. **There is one camera/one EC** — no concurrent sessions fighting over it.
5. **`/tmp/opencode/` is wiped by the overnight cleanup.** Anything worth
   keeping must live in the project (`tools/`), which is why the probe
   scripts were moved there.
6. **`debug/ec_dump` costs ~120 ms per read.** It looks like a cheap file read
   and is not: the driver performs 256 EC transfers to build it. Never put it
   on a poll path — read single registers through `ec_get` instead, and let
   the *daemon* do that (only it can, the address write is root-only).
7. **Read the EC, not a userspace proxy.** The firmware's numbers are already
   in registers (`0x68`/`0x80` for temps, `0x71`/`0x89` for fan duty,
   `0xcc`/`0xca` for tachometers); anything that shells out to a monitoring
   daemon adds its own refresh lag on top. That is why the CLI and GUI pull
   from `gs66d`.
8. **Cooler Boost does not update the duty registers.** Measured 2026-10-16:
   with boost on, `0x71`/`0x89` stayed at `70` while the tachometers read
   **~8100 RPM**. So `fan_pct` is "the curve's request", not "what the fan is
   doing", and a UI that shows fan % alone will under-report a boosted machine
   — which is why the dashboard shows **RPM** next to it. Do not "fix" the
   duty read; it is not wrong, it is a different fact.
9. **`0xc8` is not a fan.** It was suspected to be the GS66's third fan
   (LibreHardwareMonitor reports 3 on this model). It is not: under Cooler
   Boost `0xca`/`0xcc` jump from ~3700 to ~8100 while `0xc8` sits at ~1900.
   `msi-ec`'s `CONF_G1_3` maps exactly two fans (`cpu` 0x71, `gpu` 0x89) and
   mentions no third. **No third-fan reading is exposed**, because an
   unidentified register painted as a fan is a guess wearing a fact's clothes.
   (CORRECTION to an earlier note in `TODO.md`: the "`0xc8` never moves"
   observation was a period-versus-RPM misreading — `0xc8` reads `89`, which
   as a *period* is ~5370 RPM, and 89 is not an RPM at all. The register does
   move; it just does not follow the fans.)

---

## 11. Project layout

```
GS66Control/
├── src/            gs66_common.py · gs66d.py · gs66-center · gs66-gui
├── tests/          test_gs66.py · gui_smoke.py · shots/ (dark + light/)
├── tools/          hardware probes (need root) — see TODO §third-fan
│                   ecdump.py · ecprobe.py · fanprobe.py · scan_ec.py
│                   fixup.sh · recapture.sh
├── systemd/        gs66-centerd.service · gs66-center-sleep
├── modprobe/       msi-ec.conf
├── desktop/        gs66-center.desktop
├── icons/          gs66-center.svg — the red shield mark, drawn for the
│                   installed icon and mirrored by brand_pixmap()/MARK_BOX
├── etc/            config.json (the default install.sh copies in)
├── third_party/    msi-ec/   vendored teackot driver v0.13
├── install.sh      privileged install (DKMS + deploy + services)
├── uninstall.sh    clean removal
├── verify.sh       live hardware verification — every shift/fan/charge
│                   mode, cooler boost, fan-curve write-back round-trip
├── Makefile        check / test / smoke / lint / verify / install / clean
├── README.md       full research write-up + EC map + safety docs
├── CONTRIBUTING.md how to work here (tests, style, the one rule)
├── CHANGELOG.md    dated passes, newest first
├── TODO.md         what is left to do
└── APP_DETAILS.md  this file
```

---

## 12. Working agreements

* **Back up before changing** anything on the device — Tier B style copies
  into the project or `~/backup-pre-upgrade-*/`.
* **Run the test suite after every change.** `python3 tests/test_gs66.py`.
* **`verify.sh` must report ALL CHECKS PASSED** before a change counts as
  done on real hardware.
* **Report the PySide6 substitution** in the final summary — the user asked
  for PyQt6.
* **EC writes are the danger zone.** Anything that pokes bytes outside the
  documented map goes in `tools/` as an explicit probe, never into the
  daemon's normal path.
