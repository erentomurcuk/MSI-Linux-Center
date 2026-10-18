# TODO — GS66 Control Center

Companion to [`APP_DETAILS.md`](APP_DETAILS.md), which holds all the context
this list assumes. Status markers: `[x]` done · `[~]` deferred · `[ ]` open.

Last updated: 2026-10-17.

---

## Done

- [x] **Research + design** — EC map cross-checked against isw, teackot's
      msi-ec and the in-tree driver. Daemon + CLI + PySide6 GUI chosen.
- [x] **All source written** — `gs66_common.py`, `gs66d.py`, `gs66-center`,
      `gs66-gui`.
- [x] **114 offline tests passing** — `python3 tests/test_gs66.py`.
- [x] **GUI smoke test with behaviour assertions** — contrast, the battery
      state machine, the curve keyboard path, every confirmation's wording,
      the dashboard's numbers against their labels, and four MUX states. Since
      2026-10-17 it also pins the *invariants*: identical token keys in both
      themes, the model number in the same ink box in the shipped SVG and the
      painted mark, a glyph behind every nav row, the sidebar and the page
      stack being the same list, and one type scale across the tiles.
- [x] **MSI rebrand, app icon and consistency pass** — see *Done in the
      2026-10-17* below (MSI-red accent, the centred-shield icon, the tray mark
      tinted by temperature, and the colour/type/spacing inconsistencies the
      first pass left behind).
- [x] **Deployed to the desktop and verified live** — GUI + icon + `.desktop`
      installed by three `install` calls from a single `pkexec` prompt, no
      daemon restart, md5-verified, GUI relaunched and screenshotted. See
      *Redeployed and re-verified* in Open for the recipe and the one gotcha
      (a replaced icon does not show in the menu until the shell re-reads it).
- [x] **GUI smoke test passing** — renders one PNG per tab to `tests/shots/`.
- [x] **Installed and verified on real hardware** — driver bound
      (`16V1EMS1.118`, debug interface on), DKMS `msi-ec/0.13 installed`,
      `gs66-centerd` active, all 5 profiles apply correctly, `factory`
      captured honestly.
- [x] **`verify.sh` reports ALL CHECKS PASSED** — every shift mode, fan mode,
      cooler boost, charge threshold, fan-curve write-back round-trip.
- [x] **Moved project** `~/gs66-center` → `~/Projects/GS66Control`.
- [x] **Moved hardware probes** out of `/tmp/opencode/` (wiped nightly) into
      `tools/`.
- [x] **GUI/UX overhaul + repo preparation** — see *Done in the 2026-10-16
      UI/UX overhaul* below (theme system, sidebar shell, offline banner, the
      fan-curve poll-clobber fix, and the repo files: `Makefile`, `.gitignore`,
      `.editorconfig`, `CONTRIBUTING.md`, `CHANGELOG.md`, `etc/config.json`).

### Bugs found and fixed along the way — do not reintroduce

- pkexec relative-path failure → `SELF="$(readlink -f "${BASH_SOURCE[0]}")"`.
- `ModuleNotFoundError: gs66_common` in the *installed* CLI/GUI → added
  `_add_lib_path()` searching script dir + `/usr/local/lib/gs66-center` to
  all three programs.
- `QTimer() takes at most 1 argument` in `gs66-gui main()` → removed a stray
  `QTimer()` and dead `sig`.
- `verify.sh` `head()` shadowed the `head` binary → renamed `section()`.
- DKMS `@VERSION@` placeholder and `msi_ec` / `msi-ec` name mismatch.
- Qt5-isms (`QPalette.Foreground`, `.base()` → `ColorRole`), missing
  `QPointF` import, dead imports, unreadable Cooler Boost styling, `--json`
  only accepted before the subcommand, GUI factory-ghost used read-write
  `apply` → switched to read-only `show`.
- Earlier `Mode unspecified` / `Limit 0%` readings were **not** bugs — they
  were the EC's genuine unset boot state. Writes work correctly.

---

## Deferred

### [~] Third fan support — *the register is unidentified, so it is not exposed*

The GS66 Stealth has **3 fans** (1 CPU + 2 GPU side); LibreHardwareMonitor
reports `GS66 Stealth 10SG: 3`. The driver only models `cpu` + `gpu`.

**Settled 2026-10-16 — `0xc8` is not a fan, and the earlier note was wrong.**

An earlier probe concluded "`0xc8` never moves", which was a
**period-versus-RPM misreading**: the recorded values (87–91) were raw 16-bit
*periods*, and `478000 // 87` is ~5500 RPM — a number nobody stopped to
interpret. Sampled properly, `0xc8` oscillates 1889→2069 RPM continuously.

But it is still not a fan. The decisive test is Cooler Boost:

```
                   0xc8        0xca (GPU)   0xcc (CPU)   duty 0x71/0x89
baseline           ~1990       ~3590        ~3700        70 / 70
cooler boost ON    ~1800       ~7960        ~8100       70 / 70   <- flat
```

`0xca`/`0xcc` more than double; `0xc8` does not follow. So it is not driven by
Cooler Boost and not one of the triplet. `msi-ec`'s `CONF_G1_3` maps exactly
two fans and contains **no** mention of `fan3`, `sys_fan` or any third
tachometer, so there is nothing to model it against either.

**Decision: no third-fan reading is shipped.** An unidentified register
rendered as a fan would be a guess wearing a fact's clothes — the same reason
`temp_band` is shared rather than reimplemented per widget. Re-opening this
needs one of:

1. a byte-level scan (`tools/scan_ec.py`, **never yet run** — needs root) that
   finds a *pair* which drifts on its own **and** responds to a fan control;
2. an MSI service manual or EC disassembly naming the address;
3. `nvidia-smi -q -d FAN` (or another independent readback) agreeing with a
   candidate register under a known load.

Until then the honest answer to "where is the third fan" is *the EC does not
expose it on this firmware.*

The **RPM scale itself is now confirmed**, not merely assumed: Cooler Boost
drives the documented pair from ~3700 to **~8100**, the top of this laptop's
range, which `478000 // period` reproduces. That question is closed.

**Probe scripts, all in `tools/`, all need root:**

```bash
pkexec env PATH=/usr/local/bin:/usr/bin:/bin python3 tools/fanprobe.py
pkexec env PATH=/usr/local/bin:/usr/bin:/bin python3 tools/scan_ec.py
```

The `env PATH=…` prefix is **mandatory** — pkexec sanitises `PATH` and drops
`/usr/local/bin`, so the script dies with `FileNotFoundError: gs66-center`.
Both restore what they touch (fanprobe restores the original curves;
scan_ec only toggles the Cooler Boost bit).

---

## Done in the 2026-10-17 MSI rebrand + consistency pass

Second look-and-feel pass on top of the overhaul below: the palette is now
MSI's, the app has a real icon, and the inconsistencies the first pass left
behind were found and fixed rather than styled over. Daemon, CLI and protocol
untouched; 114 offline tests + the GUI smoke suite green.

By-hand rollback point: `~/backup-pre-ui-overhaul-20261017-112844/`.

### [x] The app mark — a centred red shield, and nothing else

- `icons/gs66-center.svg` is the installed icon (`install.sh` puts it in the
  menu entry and `hicolor/scalable`), and the GUI paints the **same** shield for
  the window icon, the header and the tray: one `_mark_frame()`, one
  `_mark_shield()` gradient, three consumers. `MARK_BOX` is the four numbers all
  three read, which is what keeps them one shape.
- The first cut of this pass drew MSI's badge literally (dark rounded tile,
  shield, `66`); the user's verdict was *"just make it a centered red shield.
  Thats enough."* The tile and the lettering are gone and the shield is drawn as
  large as the canvas allows — which is also the variant that still reads as a
  mark at the 16–22 px a taskbar and a menu entry actually render.
- **The tray mark is tinted by the hottest reading** and carries that number;
  it was a plain coloured circle, which shared nothing with the app's identity.
  It is the only mark that carries text, and `tray_digit_font()` sizes the digits
  off the label because `100` at the two-digit size is wider than the badge.

### [x] MSI colours, and one meaning per colour

- Accent → MSI red (dark `#e2231a` / `#ff4a3d`, light `#c8102e` / `#a30d25`).
  The green→amber→orange→red band stays exactly as it was: it is temperature —
  status, not chrome.
- **The accent is the only "selected / this is your value" colour.** Three
  contradictions fixed:
  * Cooler Boost's "on" state is the filled **accent**, not amber. It is the
    feature that *cools*; amber said "hot".
  * The **battery tile** wears the accent (it was green whatever the charge, so
    8% looked as fine as 100%) and gained a state chip: green charging/full,
    amber "not charging" on AC, accent otherwise. The sub-line is now
    `AC · health 91% · limit 60%`.
  * The **fan curve** is drawn in the accent with the live position marked in
    its temperature colour and labelled — the curve itself used to be tinted by
    the live temperature, so an untouched curve changed colour minute to minute.
- The **offline banner and the offline pill share one colour** (they used
  `COL_HOT` and `COL_CRIT`).

### [x] Consistency sweep

- `page_layout()` for all six pages; `kv_row()` for every label/value row
  (Dashboard Right now + System, Performance graphics state, Settings daemon) —
  hand-built rows are how their values had drifted to different weights. The
  value label carries the stretch; a trailing `addStretch(1)` hands a wrapping
  label its minimum width and breaks short strings across two lines.
- One pixel **type scale** (`TYPE_MICRO` 10 … `TYPE_DISPLAY` 30) via
  `ui_font()`/`mono_font()`; no more mixed point/pixel sizes.
- Radio-button rows **fill their card** instead of ending in a trailing stretch.
- New radius tokens `R_PILL` 8 / `R_BAR` 5, so the eight hardcoded `8px`/`4px`
  radii (chips, menu rows, tooltips, both progress bars) come from the token set.
- `Card("Cooler boost")` → `Card("Cooling")`; profile rows read
  `name  ·  built-in` with tooltips.
- **The profiles list and the config box gained a `:focus` border.** Every other
  field already had hover/focus/disabled states; those two had hover and
  nothing else, so tabbing into them gave no cue.
- **`set_chip()` clears the style as well as the text when it hides a chip** — a
  hidden chip that keeps the last colour is a surprise for the next caller.

### [x] 🔴 The painted mark and the shipped icon had drifted — FIXED, then superseded

`brand_pixmap()` put `66` with glyph centre at **0.575** of the tile; the SVG
put it at **0.479** — twelve pixels apart at 128 px. Side by side they clearly
disagreed; in any screenshot of one of them it looked perfect. The fix brought
the SVG's size and baseline onto the painted mark's numbers, and
`check_brand_mark` rasterises both and compares the ink boxes, so they cannot
drift again. Perturbing the SVG's top edge makes the check fail, which is how it
was proven to guard something. **The `66` and the tile are gone now** (see the
mark section above); the check outlived them cleanly because it compares the
*shield's* ink box against `MARK_BOX` rather than a glyph.

### [x] 🔴 The status bar erased every confirmation — FIXED

The daemon's `connected · fw …` line was pushed with `showMessage(…, 2500)` on
**every poll**, and the poll is 1.5 s: an action's "Set charge limit to 80%"
was wiped off the bar within a second and a half of being written, and a slow
reply never appeared at all. The daemon readout now lives in a permanent widget
on the right of the bar and only changes when the state changes.

### [x] Five new invariants in the smoke test

Each one guards a failure that a screenshot cannot show:

| Check | Guards against |
|---|---|
| `check_theme_tokens` | a theme that silently lost a token, or an accent ink below AA |
| `check_brand_mark` | `icons/gs66-center.svg` drifting from the painted mark |
| `check_glyphs` | a typo in `Sidebar.ITEMS` → a transparent pixmap beside a label |
| `check_nav_matches_tabs` | an unreachable section, or a dead nav click |
| `check_type_scale` | the tiles disagreeing about `TYPE_DISPLAY` |

Plus the battery-chip colour cases in `check_dashboard_facts`: the chip's colour
is a **decision** (a state, not a reading), so it is pinned.

### [x] Both themes re-screenshotted

`tests/shots/` (dark) and `tests/shots/light/` regenerated from the smoke run.
The Makefile gained **`make shots-light`** — regenerating the light set was a
documented-but-unmemorable incantation, which is how a checked-in screenshot set
goes stale.

---

## Done in the 2026-10-16 UI/UX overhaul

The GUI was rebuilt (`src/gs66-gui`, ~2 700 lines) around one token set per
theme and a sidebar + stacked-page shell. The daemon protocol is untouched and
both suites stay green.

### [x] A real theme system — light and dark

* **One token set per theme**, fed into a `QPalette` and one application
  stylesheet. Choice order: `GS66_THEME=light|dark` → the desktop's colour
  scheme → **dark** when the platform reports *Unknown* (an offscreen test run
  reports exactly that, so headless screenshots are deterministic).
* `apply_theme(app)` is called from **both** `MainWindow.__init__` and
  `main()` — the smoke test never calls `main()`, so the `__init__` call is the
  one that runs there.
* Font sizes live in code (`setFont`), never QSS: a QSS `font-size` overrides
  `setFont`, which is a quietly reliable way to get inconsistent type.
* The stylesheet is `%`-templated rather than an f-string so literal braces
  survive.

### [x] Navigation, header, banner, shortcuts

* `Sidebar` (208 px, runtime-painted glyph icons — no icon-theme dependency) +
  `NavStack`. `NavStack` keeps the `addTab`/`count`/`currentIndex`/`tabText`
  surface, so the smoke test and the geometry/tab persistence were not touched
  by the move off `QTabWidget`.
* `Header` with a tri-state connection pill (`connecting…` / `daemon online` /
  `daemon offline`), Refresh, Quit.
* `Banner` — appears **only** when the daemon is unreachable, and says plainly
  that every reading is read-only and how to start the daemon.
* `Ctrl+1…6` navigate, `Ctrl+R` refresh, `Ctrl+Q` quit.
* Every page is wrapped in a scroll area, so a short window scrolls instead of
  clipping.

### [x] 🔴 Fan-curve edits were lost on the next poll — FIXED

`FansTab.refresh()` adopted the EC's curve on every 1.5 s poll, so a point you
had just dragged was overwritten before Apply — the edit simply vanished.

* An `_unsaved` flag per fan; `_canvas_changed`/`_spin_changed`/`_load_ghost`
  set it, `refresh()` only adopts the EC curve while it is clear, and the flag
  clears only once the EC curve **matches the editor** (`_same_curve`).
* Apply is enabled only when the editor is valid **and** dirty **and** online;
  a `● unsaved changes` label makes the pending state visible.
* The same class of bug in the Battery tab was fixed in the 2026-10-14 pass;
  both editors now hold a value and write nothing until Apply.

### [x] Dashboard, Profiles, Settings polish

* Dashboard: four compact `StatCard` tiles + Battery / Right now + a new
  full-width **System** card (driver + firmware, debug interface, socket, the
  two config flags), with a trailing stretch so no card is left tall and empty.
* Profiles: `QListWidget` + a top-aligned detail card; selection survives a
  poll.
* Settings: the config box no longer keeps a scrollbar at six keys — the height
  is computed from the font metrics and capped (the old
  `QTextDocument.adjustSize()` returned 8 px for 8 lines).

### [x] Two more bugs found while reviewing the rewrite

* **The curve canvas could draw a legal point off its own edge.**
  `CurveCanvas.TMIN` was a hardcoded `20` while the EC accepts
  `core.TEMP_MIN = 10`, so a keyboard step could move point 0 to 10 °C — left
  of the plot, where the handle vanished and the mouse could no longer reach
  the value the spin box showed. The axes now derive from
  `core.TEMP_MIN/MAX` and `core.SPEED_MIN/MAX`, and `check_curve_keyboard`
  asserts a legal point can never be drawn outside the plot.
* **Profiles Save/Delete and Settings Re-apply/Capture stayed live offline.**
  They are daemon writes, so they now disable with the daemon instead of
  answering with a `✗` on click. `check_offline_disables_writes` pins it.

### [x] Repo preparation (no git repository was created)

Added the files a repo needs, **without** running `git init`:

* `.gitignore` — bytecode, DKMS/kernel build output, and the scratch
  `_xdg_config` a smoke run parks under `tests/shots/`.
* `.editorconfig` — LF, UTF-8, 4-space Python, tabs for the Makefile.
* `Makefile` — `make check` (compile + tests + smoke), plus `test`, `smoke`,
  `lint`, `verify`, `install`, `uninstall`, `clean`.
* `CONTRIBUTING.md` — the one rule (the daemon owns every EC write), how to run
  the tests, the style, and the changelog convention.
* `CHANGELOG.md` — dated passes, newest first, from the history in this file.
* `etc/config.json` — the default config as a repo template, now installed by
  `install.sh` so the shipped and documented defaults cannot drift.

### [x] Both themes screenshotted

`tests/shots/` (dark) and `tests/shots/light/` (light) each carry the six
sections; the daemon-offline state was rendered separately to check the banner
and the red pill.

---

## Done in the 2026-10-16 pass

### [x] Graphics mode (MUX) switch — new

The user asked for this explicitly ("add MUX Switch for GPU, currently in only
Nvidia mode"). The machine is genuinely in discrete mode — three independent
signals agree (EC `0x2e` = `0x5b`, UEFI `data[1]` = 1, no Intel display
controller on the PCI bus).

Full design, risk analysis and the three-source read model are documented in
`README.md` § *Graphics mode — the MUX switch* and `APP_DETAILS.md` §3. What
matters here:

* Gated behind **`AllowMuxSwitch`** (default `false`) **and** `--yes` / a
  confirmation dialog. It can leave a laptop with no display, and MSI's own
  MUX model list excludes the 10th-gen GS66.
* **Never automatic** — excluded from `RestoreLast`, `ReapplyOnResume` and
  every profile, with a comment on `snapshot()` saying why.
* Backs the raw efivarfs image up to `/var/lib/gs66-center/mux-backup.json`
  before writing; `graphics revert` puts it back byte for byte.
* Read-modify-write of one bit, so the webcam bit beside it survives.
* The write path itself is **unproven on this model** — it needs a reboot to
  test and the user should decide when to take that risk.

### [x] Bugs found in the review and fixed

* 🔴 **Dashboard reported state of charge as battery health.** The tile divided
  `energy_now / energy_full` and printed it as *"N% of new"* — so it announced
  "67% of new" directly beside a big **67%** and told you nothing about the
  battery. `BatteryTab` had always done it right
  (`energy_full / energy_full_design`); the dashboard did not. Now `health
  91%`, and `check_dashboard_facts` asserts the two numbers **cannot**
  coincide again.
* 🔴 **`verify.sh` did not put the fan mode back.** It captured the original
  shift mode and restored it, then hardcoded `fan auto` — so running the
  verifier silently moved anyone who had deliberately chosen `advanced` back
  onto the firmware default, in a script whose header promises *"nothing here
  leaves the machine in a state other than the one it found"*.
* **Fan cards showed RPM as the number and duty as the bar with no label.**
  The sub-line just said "RPM", so the bar's meaning was a guess and it does
  not track the number beside it. Now `RPM · fan 48%`.
* **The mode buttons' selected state was too subtle** to read at a glance
  (`mark_checked()`).
* **Settings showed only part of the config** — the daemon's `status()`
  omitted `SocketGroup` and now omitted `AllowMuxSwitch` too. Both published.
* **`gs66-center profile show` printed compact JSON** while every other
  `--json` path indents. Now consistent.
* **Config box had a scrollbar** at six keys. Height now measured from the
  document and capped, so a seventh key cannot reintroduce it.

### [x] What was investigated and deliberately *not* changed

* **No third fan.** See Deferred above — `0xc8` does not follow Cooler Boost
  and the driver maps two fans. Shipping it would have been a guess.
* **`fan_pct` under Cooler Boost.** `0x71`/`0x89` stay at 70 while the fans
  run at ~8100 RPM. That is not a bug to fix; RPM is displayed next to it for
  exactly this reason. Recorded in `APP_DETAILS.md` §10.
* **`status` polling was not "optimised".** 20 `gs66-center status --json`
  calls cost 1.935 s total (~97 ms each), and the daemon used 743 ms of CPU
  across 3 h. There is nothing here worth making more complex.

---

## Done in the 2026-10-14 polish pass

### [x] Battery tab: Apply button

The charge-limit slider used to apply **only on slider release**, so a slow
drag across the track silently reprogrammed the charger several times on the
way to wherever you were aiming.

* `Apply` / `Revert` buttons; enabled only when the slider differs from the
  EC's value. The button names its own value — `Apply 80%` — instead of the
  bare `Apply` that made you check the slider first.
* Presets (60/80/100) now **select only**; they never write.
* `60% applied  ·  80% pending` states the difference explicitly, and
  `N% applied — no pending change` when there is none.
* The big label is still built from the **EC's** value, so the UI cannot
  claim a limit that did not stick.
* `refresh()` decides whether to adopt the EC value *before* updating
  `_applied` — once it does, "the slider is behind the EC" is no longer
  observable, which was the subtle part.

### [x] "applied" notifications describe what changed

Done in the **daemon**, because it is the only party that reads the EC back
both before and after a write.

* `MsiEc.get(key)` reads a setting back through the same path `set` writes
  it; `set` now returns `{key, was, now, changed, summary}`.
* `describe_change()` / `diff_settings()` / `curve_points_changed()` live in
  `gs66_common`, so the CLI and the GUI can never phrase the same event
  differently. Both render the daemon's `summary` verbatim.
* `profile apply` returns a `changes` list — only the keys that actually
  moved, plus `cpu curve (3 points)`. A setting the profile never mentions
  is left out rather than reported as "unchanged".
* The no-op is honest: `performance already eco`, `applied gaming: nothing
  to change`. Re-running a profile is not an error and no longer reads as an
  accomplishment.

### [x] General code + UI/UX review

* **Contrast.** `text_on()` picks black or white by measured WCAG luminance;
  white on the amber `hot` colour was **2.9:1** and failed AA. Now ≥ 4.5:1
  for every palette colour, asserted in `tests/gui_smoke.py`.
* **`muted_color()`** derives secondary text from the live palette (blend
  0.6 fg → 0.4 bg) instead of the hardcoded `rgba(160,160,160,1)`, which was
  near-invisible on a light background. Verified ~4.0:1 light, ~5.5:1 dark.
* **`CurveCanvas` is keyboard-operable** — `StrongFocus`, arrows to move a
  point, Alt+arrows to walk the selection, Shift for the coarse step. Mouse
  and keyboard share one `_set_point()` clamp, so neither can produce a
  curve the user cannot Apply. The selected point gets a focus ring, since a
  custom-painted widget gets no focus rect for free.
* **Tray menu** (Show window / Quit). Closing only hides, and without a menu
  a desktop that does not surface the icon left no way back in *and* no way
  to quit.
* Accessible names + tooltips on the slider, canvases, raw-EC fields.
* **Dead code removed:** `_temps` deque, unused `collections` import, the
  `findChildren()` walk over two widgets already held by name, `x_prev`.
* **`QSignalBlockerSafe`** moved above the first class that uses it.
* **Notifications:** status bar for routine outcomes, tray balloon **only**
  for failures. A balloon for "shift eco" is noise people learn to ignore.
* Deleting a profile now asks — the one irreversible action in that tab.
* pyflakes clean over `src/` and `tests/`.

### [x] Small correctness fixes found along the way

* Dashboard: `if rpm` treated a stopped fan (0 RPM) as "RPM needs the
  daemon". Now `is not None`.
* `verify.sh` hardcoded `BAT1`; it now discovers the battery, and its RPM
  check accepts 0 (a stopped fan is a real reading).
* `tools/fixup.sh` derived its source path from `BASH_SOURCE` instead of the
  stale `/home/eren/gs66-center`.
* CLI and GUI disagreed about temperature bands (CLI called 62 °C "warm",
  GUI called it "cool"). Both now use `core.temp_band()`.
* `curve set` now honours `--json`.
* Two unused imports in `test_gs66.py`.

### [x] Profiles no longer touch the charge limit

`silent`, `balanced` and `gaming` each shipped `charge_end: 100`, so applying
any of them silently reset a limit the user had deliberately picked. The
daemon's own journal recorded it happening:

```
Oct 14 22:32:54  gs66d: applied profile balanced: charge limit 60% -> 100%
```

Fixed at the **table**, not in the apply path: `_apply_profile_dict` already
skipped an absent/`None` `charge_end`, so the three performance profiles now
simply omit the key. The rule is uniform and documented in
`BUILTIN_PROFILES` — **a profile that does not mention a charge limit does not
touch one** — and `battery` keeps `charge_end: 60`, because battery
preservation is that profile's entire purpose. `RestoreLast` is unaffected:
`last.json` *does* carry the limit, so a reboot still restores it.

* `test_performance_profiles_leave_the_charge_limit_alone` sets the machine to
  80%, applies all three, and asserts the limit is still 80% — in the EC
  **and** in both the `changes` and `activities` confirmations, because the
  message has to agree with the hardware.
* `test_only_battery_carries_a_charge_end` pins the shape of the table. "The
  key is absent" *is* the mechanism, so tidying it back in would re-open the
  footgun with every other test still green.
* The Profiles tab hint no longer claims a profile stores a charge limit.
* `README.md` §Built-in profiles and `APP_DETAILS.md` §7 updated to match.

---

## Open

### [ ] Graphics-mode write is unproven on this model

Everything about the MUX is verified **except the write itself** — reading it
is confirmed from three independent sources, the write path is
reverse-engineered from a tool developed on a *different* MSI laptop, and
testing it requires a reboot that can plausibly leave the machine with no
display. That is a decision for the user, not a thing to try quietly.

If they want to take it: switch, reboot, and check `gs66-center graphics`
reports `running now: Hybrid` (an Intel controller will appear on the PCI
bus). Recovery is a firmware reset if it does not come back.

### [x] Battery limit is at 60 — verified live, not just against a fake payload

The user set the charge limit to **60%** themselves. Checked against the live
daemon on 2026-10-17: the Battery tab shows the slider at 60, reads
`Limit stop at 60%, start at 50%`, `Apply` is **disabled** and the line under it
says `60% applied — no pending change`; `gs66-center battery` agrees.

The GUI's reading and the CLI's reading of the same EC value are the two things
that had to agree, and they do. The **restore** side is covered offline rather
than live: `test_performance_profiles_leave_the_charge_limit_alone` (sets 80%,
applies all three performance profiles, asserts the limit is still 80% in the EC
*and* in both confirmations), `test_only_battery_carries_a_charge_end`, and
`RestoreLast` replaying `last.json`, which does carry the limit.

### [x] Redeployed and re-verified — 2026-10-17

The GUI rewritten in this pass needed to reach the desktop, and it did — without
touching the daemon:

* **Minimal targeted install, one `pkexec` prompt, three files.** `src/gs66-gui`
  → `/usr/local/bin/gs66-gui`, `icons/gs66-center.svg` →
  `/usr/share/icons/hicolor/scalable/apps/`, `desktop/gs66-center.desktop` →
  `/usr/share/applications/`, then `gtk-update-icon-cache -f -t
  /usr/share/icons/hicolor`.
* **No `install.sh` run, and no daemon restart.** `gs66d.py`, `gs66-center` and
  `gs66_common.py` were *already* md5-identical to `src/`, and this pass changed
  no protocol, so a DKMS rebuild / module reload / service bounce would have been
  risk with no benefit. The old GUI process was stopped, the new one started on
  the live Plasma Wayland session, and all six md5 pairs were verified equal.
* ⚠️ **A replaced icon does not appear in the menu until the shell re-reads it.**
  After the install, `kiconfinder6 gs66-center` already resolved to the new file
  and the `ksycoca6` database had already been rebuilt — yet the launcher kept
  painting the **old** dark tile with `66`, while the panel's task entry (whose
  icon comes from `setWindowIcon()`) was new immediately. A running Plasma caches
  the *rendered* pixmap in memory and nothing on disk invalidates it:
  **`systemctl --user restart plasma-plasmashell.service`** (or a re-login) is
  part of judging an icon change, and the launcher showed the new mark straight
  afterwards. Do not go hunting for a second icon file, a theme override or a bad
  `Icon=` key — there is none. Qt re-registers the tray item with
  `StatusNotifierWatcher` by itself, so the shell restart costs nothing.

```bash
cd /home/eren/Projects/GS66Control
make check        # 114 offline + GUI smoke (also: make shots-light)

# 1. deploy — installs only, so no DKMS rebuild and no daemon bounce.
#    NOTE the absolute paths (and --keep-cwd): pkexec runs PROGRAM in $HOME,
#    so a bare `install src/...` cannot stat its source — see Housekeeping.
R=/home/eren/Projects/GS66Control
pkexec bash -c "install -m 755 $R/src/gs66-gui /usr/local/bin/gs66-gui
                install -m 644 $R/icons/gs66-center.svg /usr/share/icons/hicolor/scalable/apps/
                install -m 644 $R/desktop/gs66-center.desktop /usr/share/applications/
                /usr/bin/gtk-update-icon-cache -f -t /usr/share/icons/hicolor"

# 2. make the new menu icon visible (see the gotcha above)
systemctl --user restart plasma-plasmashell.service

# 3. relaunch the GUI on the live session — find it first; never pgrep -f,
#    whose pattern self-matches the shell running it
ps -eo pid,args | awk '/[g]s66-gui/ {print $1}'
setsid /usr/local/bin/gs66-gui >/dev/null 2>&1 &
```

For a **daemon** change instead, `./install.sh` is still the whole answer (it
also enables the service and installs the resume hook), and the full recipe —
`verify.sh`, then put the machine back to a sensible mode — is in the
*Housekeeping* notes below.

### [x] `tools/fixup.sh` has a stale source path — FIXED

`tools/fixup.sh:5` said `SRC=/home/eren/gs66-center` after the project moved
to `/home/eren/Projects/GS66Control`. It now derives the root from its own
location, so it survives the next move:

```bash
SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
```

`readlink -f` is needed, not just `dirname $0`, because `pkexec` may invoke
it through a path that is itself a link.

---

## Housekeeping notes

* 🔑 **`pkexec` runs PROGRAM in the user's HOME, not the current directory**
  (from its man page; `--keep-cwd` overrides). Every `install src/...` in an
  `install.sh`/deploy chain therefore fails with `cannot stat 'src/...'` until
  `--keep-cwd` is added. It is deterministic, so an identical retry without
  the flag fails identically — do not "try again", add the flag.
* **`--json` must be given BEFORE or AFTER the subcommand, never both
  assumed.** `argparse`'s `set_defaults()` mutates the *action object's*
  `.default`, and a shared parent parser (`parents=[common]`) hands the same
  action to every subparser — so `set_defaults(json=False)` wrote `json=False`
  into the namespace and silently clobbered a leading `--json`. A script
  reading JSON off stdout got prose with exit code 0. Fixed by leaving the
  actions at `default=SUPPRESS` and applying the fallbacks in `main()`
  *after* parsing; pinned by `TestCli.test_json_flag_before_the_subcommand_is_not_dropped`.
* **A GUI smoke test must never write the user's real config.** Adding
  `QSettings` geometry/tab persistence made `tests/gui_smoke.py` rewrite
  `~/.config/gs66-center/gs66-center.conf`, which would have opened the next
  real window on whatever tab a test run ended on. `main()` now points
  `XDG_CONFIG_HOME` at a scratch dir under `tests/shots/` before anything
  constructs a `QSettings`.
* **Run the light screenshots with an *absolute* outdir.** A **relative** outdir
  makes Qt ignore the redirected `XDG_CONFIG_HOME`, so the run writes the real
  `~/.config/gs66-center/gs66-center.conf` after all — the exact leak the
  previous bullet prevents. Use:
  `GS66_THEME=light QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py "$PWD/tests/shots/light"`.
* **Replacing the app icon does not update the menu until the shell restarts.**
  The file on disk, `ksycoca6` and `kiconfinder6 gs66-center` can all be correct
  while the launcher keeps painting the *previous* pixmap: a running Plasma caches
  the rendered icon in memory and nothing on disk invalidates that. It is not a
  second icon file, a theme override or a bad `Icon=` key, and it is not
  fixable by re-running `gtk-update-icon-cache` — `systemctl --user restart
  plasma-plasmashell.service` (or a re-login) is. The **tray** icon is different
  in kind: the app publishes its own ARGB pixels over `StatusNotifierItem`, so
  there the app is authoritative and the shell's copy is what goes stale. See
  *Redeployed and re-verified* above for the whole sequence, and note that Qt
  re-registers the tray item by itself after the restart.
* **The GUI is PySide6, not PyQt6.** The user asked for PyQt6; PySide6 was
  substituted because it was already installed. Report this in the final
  summary (also noted in `APP_DETAILS.md` §1).
* **`pgrep -f` self-match:** a check command whose own command line contains
  the literal path it is searching for matches itself. Two lines in a process
  audit were artifacts of the audit. Use `ps -eo pid,args | awk '/pat/ && !/awk/'`
  and keep the raw string out of the same command.
* **`AGENTS.md` / `CHANGELOG.md`** at `~/` document an unrelated **Samsung
  M20** Android server, not this laptop. Their changelog mandate most likely
  does not apply here — but confirm with the user before assuming, since the
  mandate is written strongly ("edit this document in the same session").
* `tests/__pycache__/` exists and is harmless; `src/__pycache__/` was removed
  during the move.
* Anything fetched or generated for a *run* belongs in the project, never
  `/tmp/opencode/` — it is wiped nightly and has already cost this project
  time once.
