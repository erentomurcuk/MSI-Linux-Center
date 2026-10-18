# Changelog

All notable changes to MSI Linux Control (formerly GS66 Center) are recorded
here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
`YYYY-MM-DD` dates. Older rows keep the names of their time - `GS66 Center`,
`gs66-centerd` - because they are history, not stale references.

**Read this file before you trust the tree.** This project has no version
numbers yet - the sections are dated passes, newest first, and each one is a
rollback point that matches a by-hand backup taken before the change. Every
upgrade, fix, or configuration change gets a row here **in the same session**
it was made; the runbook convention is documented in `CONTRIBUTING.md`.

---

## 2026-10-18 - polish pass: plain dashes, temperature cards, tray ink

Three changes from a review of the running app: the temperature cards were
repeating the fan duty, the tray icon had one fixed look, and the whole tree
used typographic dashes. The test suite gained two checks and both theme
screenshot sets were regenerated.

### Changed
- **The CPU and GPU cards no longer repeat the fan duty.** They carried
  `fan 48%` under the bar, the same number the CPU-fan / GPU-fan cards beside
  them already show as their value *and* name, so one reading was printed
  twice and the temperature cards read as fan cards. The sub-line is gone, and
  `StatCard.show()` now **hides** an empty sub-line instead of reserving a
  blank row, so the tiles stay on one baseline. The bar and the band chip are
  unchanged: they are the temperature's own encoding.
- **The tray icon's ink is now a choice.** `temp_pixmap()` takes a `mode`:
  `colour` (the band tint with a contrast-colour number, the previous and
  default behaviour), `white` or `black` (a flat monochrome shield for matching
  a light or a dark panel, with the number in the opposite ink so it stays
  readable). Settings gained a **Tray icon** card - a
  `Show the temperature on the tray icon` switch and Colour / White / Black
  buttons. The preference is stored in the app's own `QSettings`, never in the
  daemon config (it describes this window, not the EC), and it repaints the tray
  immediately rather than waiting out the next 1.5 s poll. With the number off,
  the tooltip still carries both temperatures.
- **No em-dashes anywhere.** Every em-dash, plus the few en-dashes and Unicode
  minus signs that read as the same family, in the tracked text became a plain
  hyphen: 713 characters across 25 files, from the docs and the source to the
  tests and the icon's own comment. `third_party/msi-ec/` is skipped by the
  sweep and remains upstream's copy byte for byte.

### Added
- `check_tray_icon` and `check_tray_options` in `tests/gui_smoke.py`. The first
  samples the painted pixels of the three inks (white brighter than the band
  tint, which is brighter than black), pins that colour mode still follows the
  temperature band, and asserts the Settings controls mirror the window rather
  than keeping a second copy. The second drives the write-and-reload path, the
  same way the geometry check does.
- The dashboard check now asserts each temperature card carries an empty and
  hidden sub-line and a band chip, so the fan line cannot creep back.

---

## 2026-10-18 - review pass: install safety, honest messages, version

A full read-through of the daemon, CLI, GUI and install path after the rename.
Four real defects found and fixed, one of which could have cost a machine; the
test suite grew from 138 to 142.

### Fixed
- **`install.sh` could leave two daemons owning the EC.** An earlier **GS66
  Center** install (`gs66-centerd`, its own socket and unit) is not removed by a
  plain reinstall, so both daemons would have written the same embedded
  controller through the same kernel driver - the one thing this program's
  one-writer rule forbids. `install.sh` now stops and disables `gs66-centerd`,
  removes its unit, binaries, desktop entry, icon, doc and sleep hook, and
  **copies the existing config and saved profiles** into `/etc/msi-control` and
  `/var/lib/msi-control` first (never overwriting anything already there).
- **Ctrl+Q did not quit.** With a system tray available, `Ctrl+Q` and the
  window's X only *hide* the window (correct for the X); the shortcut and the
  header's Quit button are now wired to a real `quit_app()` that saves the
  window geometry and open section on the way out. The Quit button previously
  called `QApplication.quit()` directly, which leaves the event loop without a
  `closeEvent` - so it silently forgot both.
- **`msi-control curve` blamed the daemon for a missing model.** On a laptop
  with no curve registers it said "is msi-controld running with debug=1?"; it
  now says the model has no fan-curve registers and points at `detect` and
  `CONTRIBUTING.md`.
- **"Capture factory profile" claimed a capture that did not happen.** The
  daemon now returns `captured` / `exists` / `unsupported` / `failed` and the
  GUI reports which; a second capture reads *"Factory profile already captured
  - nothing changed"* instead of *"Captured factory profile"*.
- `MsiEc.set_curve` validates the fan name before indexing the register table
  (defence in depth; the request path already checked it).

### Added
- **Version.** `VERSION` in `msi_control.py`, printed by `msi-control
  --version` and shown in Settings (`MSI Linux Control 1.0.0 · model schema 1`),
  so a build can be named in a bug report.

### Tests
- 138 → **142**: `set_curve` rejects an unknown fan, `capture_factory` returns
  `captured` then `exists`, the capture result travels the protocol, the
  unsupported model reports `unsupported`, and `--version` prints a version.
  `gui_smoke.py` pins all four capture-factory messages.

---

## 2026-10-18 - renamed to MSI Linux Control, and made model-driven

The app is no longer "GS66 Center". It is **MSI Linux Control**, it works on
any MSI laptop through a model registry, and there are two new documents -
`AGENTS.md` (for an AI agent working in the tree) and a rewritten
`CONTRIBUTING.md` (for a person) - that both describe how to add your model and
open a pull request. 138 offline tests and the GUI smoke suite are green.

### Added
- **`models/` - the model registry.** One `models/<id>/model.json` per laptop,
  describing only what differs between machines: the `match` rules (DMI
  product/board name + EC firmware glob, weighted 50/40/30), the `ec` block
  (fan-curve and tachometer addresses, RPM constant), the `charge` bounds, the
  `features` flags, and the model's built-in `profiles`. Loaded and validated at
  import; the best-scoring model for this machine wins.
- **`models/gs66-stealth-10se/`** - the worked example: the GS66's `model.json`
  (the map the old code hard-coded) plus a README recording where each address
  came from and what was confirmed on hardware.
- **Unsupported-model mode.** A laptop with no matching model is a *working*
  state, not a broken one: everything the `msi-ec` driver exposes by itself
  (temperatures, shift and fan modes, Cooler Boost, the charge limit) keeps
  working, and fan curves, RPM and the graphics switch are **refused** with a
  `NotSupported` error instead of being guessed at. The GUI shows an amber
  banner, a read-only Fans tab and the model in Settings.
- **`msi-control detect`** - identification and diagnostics: the human report,
  `--json`, `--write DIR` (a valid draft model), and `--ec-dump` (the
  256-register map). Needs no root and no running daemon.
- **`AGENTS.md`** - the repository map written for an AI agent, including the
  step-by-step "Adding a model" procedure and the PR flow.
- **`models/README.md`** - the model-file format, field by field.
- **Tux on the shield.** The app mark is now MSI's red shield with Tux on it;
  `brand_pixmap()` and `icons/msi-control.svg` carry the same picture and the
  smoke test still compares them. The tray copy stays the plain tinted shield.

### Changed
- **Renamed everything user-facing.** CLI `msi-control`, GUI
  `msi-control-gui`, daemon and service `msi-controld`, socket
  `/run/msi-controld.sock`, config `/etc/msi-control/`, state
  `/var/lib/msi-control/`, environment variables `MSI_*`. The daemon *module*
  is `src/msi_controld.py` (underscore) so it stays importable.
- **The GS66 is now a profile, not the program.** Its EC addresses moved out of
  `msi_control.py` into `models/gs66-stealth-10se/model.json`; the code reads
  them from the active model. The MUX addresses stay in the core because the
  whole MSI family shares one block; a model only sets `"mux": true`.
- `README.md`, `APP_DETAILS.md`, `TODO.md`, `CONTRIBUTING.md` generalised from
  "the GS66" to "MSI laptops", with the GS66 kept as the worked example.
- `install.sh` copies `models/` into `/usr/local/lib/msi-control/models` and
  prints a `detect` hint; `verify.sh` gained a model section and only tests the
  fan-curve write-back when the active model actually has curve registers.

### Not changed on purpose
- **The hardware's own name.** `GS66 Stealth 10SE`, `MS-16V1`, `16V1EMS1.118`
  are facts about the laptop and stay as they are.
- **`third_party/msi-ec/`.** The vendored teackot driver is upstream code; its
  device strings and file names are not ours to rename.

---

## [Unreleased]

### Added
- `Makefile` - `make check` (compile + tests + GUI smoke), plus `test`,
  `smoke`, `lint`, `verify`, `install`, `uninstall`, `clean`.
- `.editorconfig`, `.gitignore`, and `CONTRIBUTING.md`.
- `etc/config.json` - the default config now lives in the repo as a template,
  and `install.sh` installs it, so the shipped defaults and the documented ones
  cannot drift apart.

### Changed
- `README.md`, `APP_DETAILS.md` and `TODO.md` brought in line with the UI
  overhaul below and the PySide6 substitution.

---

## 2026-10-17 - MSI rebrand, app icon and consistency pass

A second look-and-feel pass, on top of the 2026-10-16 overhaul: the palette is
now MSI's, the app has a real icon, and the inconsistencies the first pass left
behind were hunted down and fixed rather than styled over. **No daemon, CLI or
protocol change**; 114 offline tests and the GUI smoke suite stay green.

By-hand rollback point: `~/backup-pre-ui-overhaul-20261017-112844/` - the three
files that change behaviour: `src/gs66-gui`, `icons/gs66-center.svg`,
`tests/gui_smoke.py`.

### Added
- 🔴 **The app mark - a centred red shield, and nothing else.**
  `icons/gs66-center.svg` is the installed icon (menu entry +
  `hicolor/scalable`), and `shield_path()` / `brand_pixmap()` /
  `temp_pixmap()` paint the same shield for the window icon, the header and the
  tray. One `_mark_frame()` and one `_mark_shield()` gradient feed all three,
  so the mark cannot become three different marks.
  * **The tile, the lettering and the stroke are gone on purpose.** The first
    cut of this pass drew MSI's badge language literally - a dark rounded tile,
    a red shield, `66` on it - which looks right in a 128 px preview and is a
    dark square with an unreadable smudge in it at the 16-22 px a taskbar and a
    menu entry actually render. One bold silhouette survives the shrink; the
    model number, which nobody can read at that size anyway, was costing the
    shape half its area.
  * **`MARK_BOX`** is where the shield sits on the square (19, 12, 90, 104 of
    128), and the only reason the painted mark, the tray and the SVG agree:
    all three read the same four numbers.
- **`temp_pixmap()` is the same shield tinted by the hottest reading**, with
  that number on it - it used to be a plain coloured circle, which shared
  nothing with the app's identity. No reading yet → the brand mark. The reading
  is now the *only* text on any mark, and `tray_digit_font()` sizes it off the
  label, because `100` at the two-digit size is wider than the badge.
- **`check_theme_tokens`** - both themes must define the *same* token keys
  (a stylesheet interpolates `%(token)s` to an empty string, so a theme that
  silently lost one just drops a border); the accent/ink pair must clear its
  contrast floor; every radius token must be an `<int>px` string.
- **`check_brand_mark`** - rasterises the shipped SVG and the painted mark at
  the same size, takes the ink bounding box of both and compares the four
  edges, so `icons/gs66-center.svg` and `brand_pixmap()` cannot drift apart.
  It also pins the three things a screenshot cannot show: that the corners are
  still **transparent** (the tile has not crept back), that the shield fills
  `MARK_BOX` exactly (a mark floating in its canvas looks smaller than every
  icon beside it in a taskbar) and that the tray's `9` / `62` / `100` all fit
  inside the badge. Verified both ways: perturbing the SVG's top edge makes it
  fail.
- **`check_nav_matches_tabs`** - the sidebar and the page stack must be the
  same list, in the same order; a section with no nav entry is unreachable and
  a nav entry with no section is a dead click.
- **`check_glyphs`** - every glyph the UI names must actually draw something.
  A typo in `Sidebar.ITEMS` does not raise: `glyph_pixmap` falls through its
  if-chain and returns a transparent pixmap, so the sidebar shows a label with
  a hole beside it and nothing else notices.
- **`check_type_scale`** - the five dashboard tiles must all be
  `TYPE_DISPLAY`, the battery headline `TYPE_H1`, every card caption
  `TYPE_CAPTION` bold.
- **`check_control_sizes`** - every button, single-line field, spin box and
  sidebar row in the window must be the *same* height, the cooler-boost hero
  must be taller, and the slider must keep its own touch target. The rung is
  derived from the tokens (`H_CTRL + 2·PAD_CTRL_V + 2`), never written down as
  a literal, and it measures the height the *layout* will give the widget -
  `sizeHint()` alone would not notice a stray `setMinimumHeight()`, which is
  precisely the bug it exists to catch. Verified both ways: re-adding
  `setMinimumHeight(40)` to one battery button fails it.
- Layout tokens `R_PILL` (8) and `R_BAR` (5), the control-geometry tokens
  `PAD_CTRL_V` / `PAD_HERO_V` / `H_CTRL` / `H_SPIN` / `H_SLIDER` / `H_BAR` /
  `ICON_SM`, and `kv_row()` / `stat_row()` / `field_label()` / `page_layout()` /
  `notice_style()` - see *Changed*.
- **`make shots-light`** - the Makefile could only regenerate the dark set, so
  the light set was a documented-but-unmemorable `GS66_THEME=light` incantation,
  which is how a checked-in screenshot set quietly goes stale.

### Changed
- **The accent is MSI red** - dark `#e2231a` (hover `#ff4a3d`), light
  `#c8102e` (hover `#a30d25`) - replacing the blue. The green→amber→orange→red
  band stays exactly as it was: it is temperature, i.e. status, not chrome.
- **Every control is one height.** Buttons carried their own
  `setMinimumHeight()` - 46 on the shift modes, 44 on the MUX pair, 42 on the
  fan modes and on battery Apply, 40 on profiles Apply, 40 on the nav rows and
  none at all on the rest - so the battery page's `Apply` stood **10 px taller
  than the `60%` preset beside it** and the profiles list, the raw-console row
  and the sidebar each disagreed with the page they sat on. All of them are
  gone. There is one rung, set once in the stylesheet from `H_CTRL` and
  `PAD_CTRL_V`, shared by buttons, fields, spin boxes and nav rows; the
  cooler-boost button is the one documented exception (`[hero="true"]`, bigger
  on purpose because it is the tab's single big switch) and the charge slider
  keeps `H_SLIDER` because a handle needs room text does not. Two measured
  details, both in comments where they are used: Qt's `min-height` is the
  *content* box, so the rendered height is the token plus padding plus border;
  and a `QSpinBox` spends 3 px on its arrows whatever you do, so it gets 3 px
  less content to land on the same rung. `check_control_sizes` measures the
  result.
- **One meaning per colour.** The accent is the only "selected / this is your
  value" colour. Three things that contradicted that were fixed:
  * **Cooler Boost's "on" state is the filled accent**, not the amber it wore -
    Cooler Boost is the feature that *cools*, so dressing it as "hot" was
    backwards.
  * **The battery tile wears the accent.** It was green whatever the charge, so
    8% on battery looked as "fine" as 100%. It gained a chip naming the state:
    green for charging/full, amber for "not charging" on AC (usually the charge
    limit doing its job, but the one state people ask about), accent otherwise.
    The status word moved out of the sub-line, which is now
    `AC · health 91% · limit 60%`.
  * **The fan curve is drawn in the accent** - it is *your* setting - with the
    machine's live position marked separately in its temperature colour and
    labelled with the reading. The curve itself used to be tinted by the live
    temperature, so an untouched curve changed colour from minute to minute.
- **The offline condition is one colour everywhere.** The banner used `COL_HOT`
  while the status pill used `COL_CRIT`; both are now `COL_CRIT`.
- **The profiles list and the config box now show focus.** Every other field
  gets an accent border on `:focus`; these two did not, so tabbing into the
  profile list or the config view gave no cue at all - the rest of the border
  family (hover, disabled) was already there.
- **Every page uses `page_layout()`**, so a page's margin and gap are written
  once. `kv_row()` replaced the hand-built label/value rows in the Dashboard
  (Right now, System), Performance (graphics state) and Settings (daemon) -
  which is how their values had drifted to different weights and alignments.
  The **value** label carries the stretch, not a trailing `addStretch(1)`,
  which used to hand a wrapping label its minimum width and break short strings
  across two lines.
- **Button rows fill their card** instead of ending in a trailing stretch
  (shift modes, MUX, fan modes), so a row of two or three controls no longer
  huddles on the left of a wide card.
- **The status bar carries the daemon readout permanently**, on the right
  (`gs66-centerd · fw 16V1EMS1.118`, red when offline). It used to be pushed as
  a *transient* message on every poll - and the poll is 1.5 s - so an action's
  confirmation was wiped off the bar within a second and a half of appearing,
  and a slow reply never showed at all.
- `Card("Cooler boost")` → `Card("Cooling")`: the card title no longer repeats
  the button's own words.
- Profile rows read `name  ·  built-in` / `name  ·  saved`, consistent with the
  app's separator, and carry a tooltip saying which is which.

### Fixed
- **Dead glyph branches.** `glyph_pixmap` carried a `performance` (ascending
  bars) and a `check` glyph that nothing called - the sidebar had already moved
  Performance to the bolt. Removed, so the glyph set equals the set in use, and
  `check_glyphs` now pins that a named glyph actually draws.
- **`set_chip()` left the old colour on a hidden chip.** It cleared the text and
  hid the widget but kept the stylesheet, so the next caller to show it without
  going through `set_chip` would get the previous state's colour. It now clears
  the style too.
- **The painted mark and the SVG icon put `66` in different places** (glyph
  centres 0.575 vs 0.479 of the tile - a twelve-pixel gap at 128 px, plainly
  visible side by side, invisible in any single screenshot). The SVG's size and
  baseline were brought onto the painted mark's numbers, and `check_brand_mark`
  was made to measure both instead of being eyeballed. *Superseded later in the
  same pass:* the number and the tile were both removed, so the remaining
  disagreement to guard against is the whole silhouette - which is what the
  check compares now.
- **A nav icon was 18 px while every other icon was 16**, and a 22 px line of
  text in an 18 px icon's row is what made the sidebar rows taller than the
  buttons they sit level with. One token, `ICON_SM`, now sizes every icon in
  the app.
- **`PAGE_PAD_V`** - the 18 px top/bottom a page's content gets was written as
  the literal `18` in `page_layout()` *and* again in the profiles page, which
  is exactly how two pages drift apart by four pixels six months later.

### Deployed
- **Installed on this machine and verified live.** `src/gs66-gui` →
  `/usr/local/bin/gs66-gui`, `icons/gs66-center.svg` →
  `/usr/share/icons/hicolor/scalable/apps/gs66-center.svg`,
  `desktop/gs66-center.desktop` → `/usr/share/applications/`, then
  `gtk-update-icon-cache -f -t /usr/share/icons/hicolor`. One `pkexec` prompt,
  three files, **no `install.sh` run and no daemon restart**: `gs66d.py`,
  `gs66-center` and `gs66_common.py` were already md5-identical to `src/` and
  the protocol is unchanged, so the daemon never needed to go down. All six
  md5 pairs (GUI, SVG, .desktop, daemon, CLI, common) were verified equal
  afterwards, and the GUI was restarted on the live session and screenshotted.
- ⚠️ **A replaced icon does not appear in the menu until the shell re-reads
  it.** `kiconfinder6 gs66-center` already resolved to the new file and the
  `ksycoca6` database had already been rebuilt at install time - yet the
  application launcher kept painting the **old** dark tile with `66` on it,
  while the panel's task entry (that icon comes from `setWindowIcon()`) showed
  the new shield immediately. A running Plasma caches the *rendered* pixmap in
  memory and nothing on disk invalidates it, so
  **`systemctl --user restart plasma-plasmashell.service`** (or a re-login) is
  part of judging any icon change - the launcher showed the new mark straight
  afterwards. Do not go looking for a second icon file, a theme override or a
  bad `Icon=` key: there is none, and the file is already right.

---

## 2026-10-16 - UI/UX overhaul

The GUI was rebuilt around one token set per theme and a sidebar + stacked-page
shell. This is a **look-and-feel** change; the daemon protocol and every
behaviour the smoke test asserts are unchanged, and both suites stay green
(114 offline tests, GUI smoke).

### Added
- **A full light/dark theme system.** One token set per theme feeds a
  `QPalette` and a single application stylesheet. Chosen by `GS66_THEME=light|dark`,
  else the desktop's colour scheme, defaulting to dark when the platform
  reports *Unknown* (which is what an offscreen test run reports).
- **A sidebar + `NavStack`** navigation shell, replacing the flat `QTabWidget`.
  `NavStack` keeps the old `addTab` / `count` / `currentIndex` / `tabText`
  surface so the smoke test and geometry/tab persistence did not have to change.
- **Painted glyph icons** (`glyph_pixmap`, `nav_icon`, `brand_pixmap`) - the GUI
  carries no icon-theme dependency.
- **A header** with the brand, a tri-state connection pill
  (`connecting…` / `daemon online` / `daemon offline`), Refresh and Quit.
- **A read-only banner** shown only when the daemon is unreachable, stating
  plainly that every reading is read-only and naming the command to start it.
- **A System card** on the dashboard: driver + firmware, debug interface,
  socket path, and the two config flags that change what the program will do.
- **Keyboard shortcuts** - `Ctrl+1…6` navigate, `Ctrl+R` refresh, `Ctrl+Q` quit
  - mirrored in the sidebar's hint.
- `tests/shots/` now carries **both** a dark and a light set of the six tabs
  (`tests/shots/` and `tests/shots/light/`).

### Changed
- **Dashboard** rebuilt around four compact stat tiles plus Battery / Right now
  / System cards, with a trailing stretch so no card is left tall and empty.
- **Profiles** moved from a grid to a `QListWidget` with a top-aligned detail
  card; the selection survives a poll.
- **Every page is wrapped in a scroll area**, so a short window scrolls instead
  of clipping.
- **Font sizes are set in code** (`setFont`), never in QSS - a QSS `font-size`
  silently overrides `setFont`, which is how a theme pass ends up with
  inconsistent type.

### Fixed
- 🔴 **The fan-curve editor lost unsaved edits on the next poll.** `refresh()`
  adopted whatever curve the EC reported every 1.5 s, so a point you had just
  dragged was overwritten before you could press Apply - the edit simply
  vanished. The tab now tracks an `_unsaved` flag per fan: a poll only adopts
  the EC curve when nothing is pending, the flag clears only once the EC
  actually matches the editor, and Apply is enabled only when the editor is
  valid **and** dirty **and** the daemon is online. A `● unsaved changes` label
  makes the pending state visible.
- **The curve canvas could draw a legal point off its own edge.** The plot's x
  axis was hardcoded `20…100` while the EC accepts `core.TEMP_MIN = 10`, so a
  keyboard nudge could move point 0 to 10 °C, where the handle sat outside the
  plot and the mouse could no longer reach the value the spin box was showing.
  The axes now derive from `core.TEMP_MIN/MAX` and `core.SPEED_MIN/MAX`, and
  the smoke test asserts a legal point can never be drawn outside the plot.
- **Profiles Save/Delete and Settings Re-apply/Capture stayed live offline.**
  They are daemon writes like every other control, so they now disable with the
  daemon rather than answering with a `✗` on click - which reads as a bug
  rather than the offline state the banner already explains. Pinned by
  `check_offline_disables_writes`.
- **Settings' config viewer kept a scrollbar at six keys.** The height was
  measured with `QTextDocument.adjustSize()`, which returned 8 px for 8 lines.
  It is now computed from the font metrics and capped, so a seventh key cannot
  reintroduce it.

---

## 2026-10-16 - Graphics mode (MUX) switch and review fixes

### Added
- **Graphics mode (MUX) switch.** Run the panel off the Intel iGPU (hybrid) or
  the NVIDIA GPU alone (discrete). Gated behind `AllowMuxSwitch` (default
  `false`) **and** an explicit confirmation, **never** applied automatically -
  not by `RestoreLast`, not on resume, not from any profile. Backs the raw
  efivarfs image up before writing; `graphics revert` restores it byte for
  byte. Read from three independent sources (EC `0x2e` bit 6, the UEFI variable,
  and the PCI bus) and the card shows *running now* and *at next boot*
  separately, so a requested-but-not-yet-rebooted switch is visible as exactly
  that.
- `tools/` gained the hardware probes (moved out of `/tmp/opencode/`, which is
  wiped nightly).

### Fixed
- 🔴 **The dashboard reported state of charge as battery health.** The tile
  divided `energy_now / energy_full` and printed it as *"N% of new"*, so it
  announced "67% of new" right beside a big **67%** and said nothing about the
  battery's health. It now uses `energy_full / energy_full_design` (health 91%),
  and `check_dashboard_facts` asserts the two numbers **cannot** coincide.
- 🔴 **`verify.sh` did not restore the fan mode.** It captured and restored the
  shift mode, then hardcoded `fan auto` - so running the verifier silently
  moved an `advanced` user back to the firmware default, in a script whose
  header promises nothing is left changed.
- **Fan cards showed RPM as the number and duty as the bar with no label.** The
  sub-line now reads `RPM · fan 48%`, because the bar does not track the number
  beside it.
- **Mode buttons' selected state was too subtle** (`mark_checked()`).
- **Settings showed only part of the config** - the daemon's `status()` omitted
  `SocketGroup` and `AllowMuxSwitch`.
- `gs66-center profile show` printed compact JSON while every other `--json`
  path indents.

### Verified
- **RPM scale confirmed against hardware.** Cooler Boost drives `0xcc` from
  ~3700 to **~8100**, which `478000 // period` reproduces.

---

## 2026-10-14 - Battery Apply button, honest notifications, UI review

### Added
- **Battery tab `Apply` / `Revert`.** The charge-limit slider used to write on
  slider *release*, so a slow drag reprogrammed the charger several times on the
  way to the intended value. Nothing is written until Apply now, the button
  names its value (`Apply 80%`), presets select only, and the tab states
  `60% applied · 80% pending` explicitly.
- **The daemon now describes what changed.** `set` returns
  `{key, was, now, changed, summary}`, and `describe_change()` /
  `diff_settings()` live in `gs66_common` so the CLI and GUI can never phrase
  the same event differently. A no-op reads honestly (`already eco`, `nothing
  to change`).
- A tray context menu (Show window / Quit) - closing only hides, so without it
  a desktop that does not surface the icon left no way back in and no way out.

### Fixed
- **Contrast.** `text_on()` picks black or white by measured WCAG luminance;
  white on the amber `hot` colour was **2.9:1** and failed AA. Every palette
  colour is now ≥ 4.5:1, asserted in the smoke test.
- **`muted_color()`** derives secondary text from the live palette instead of
  the hardcoded `rgba(160,160,160,1)`, which was near-invisible on a light
  background.
- **`CurveCanvas` is keyboard-operable** (`StrongFocus`, arrows / Alt+arrows /
  Shift), sharing one clamp with the mouse so neither can build a curve the
  user cannot Apply.
- **Profiles no longer touch the charge limit.** `silent`, `balanced` and
  `gaming` shipped `charge_end: 100`, so applying one silently reset a
  deliberately-chosen limit; the daemon's journal recorded
  `applied profile balanced: charge limit 60% -> 100%`. The key is now absent
  from the three performance profiles, and the rule is uniform: **a profile
  that does not mention a charge limit does not touch one.**
- Dead code removed; `pyflakes` clean over `src/` and `tests/`.
- Dashboard: `if rpm` treated a stopped fan (0 RPM) as "RPM needs the daemon";
  now `is not None`.
- `verify.sh` discovered `BAT1` by name; it now discovers the battery, and its
  RPM check accepts 0.
- The CLI and GUI disagreed about temperature bands (62 °C: "warm" vs "cool");
  both use `core.temp_band()` now.

---

## 2026-10-13 - Initial implementation

### Added
- The three programs and the shared module, as written:
  - `src/gs66d.py` - the root daemon (`gs66-centerd`), which owns every EC write.
  - `src/gs66-center` - the stdlib-only CLI.
  - `src/gs66-gui` - the GUI.
  - `src/gs66_common.py` - the EC register map, validation and protocol, shared
    so the three can never disagree.
- `install.sh` / `uninstall.sh` - DKMS build of the vendored
  [teackot/msi-ec](https://github.com/teackot/msi-ec) v0.13 driver (which lists
  this machine's firmware `16V1EMS1.118`), deploy under `/usr/local`, a
  `debug=1` modprobe drop-in, the systemd unit and a resume hook.
- `verify.sh` - safe, reversible live checks against the real hardware.
- `tests/test_gs66.py` - the offline suite (fake sysfs + RAM-backed EC).
- `tests/gui_smoke.py` - headless GUI construction turned into behavioural
  assertions rather than only screenshots.
- `README.md` (the research write-up, EC map and safety model) and
  `APP_DETAILS.md` (the maintainer map).

### Fixed during bring-up
- `pkexec` relative-path failure → `SELF="$(readlink -f "${BASH_SOURCE[0]}")"`.
- `ModuleNotFoundError: gs66_common` in the *installed* CLI/GUI → the
  `_add_lib_path()` helper searches the script dir plus the install lib dir.
- `QTimer() takes at most 1 argument` in the GUI's `main()`.
- `verify.sh`'s `head()` helper shadowed the `head` binary → renamed
  `section()`.
- DKMS `@VERSION@` placeholder and the `msi_ec` / `msi-ec` name mismatch.
- Qt5-isms (`QPalette.Foreground` → `ColorRole`), missing `QPointF`, dead
  imports, unreadable Cooler Boost styling, and the GUI's factory ghost using
  the read-write `apply` instead of the read-only `show`.
