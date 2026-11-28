# Changelog

All notable changes to MSI Linux Control (formerly GS66 Center) are recorded
here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
`YYYY-MM-DD` dates. Older rows keep the names of their time - `GS66 Center`,
`gs66-centerd` - because they are history, not stale references.

**Read this file before you trust the tree.** The sections are dated
passes, newest first, and each one is a rollback point that matches a
by-hand backup taken before the change; `VERSION` in
`src/msi_control.py` (introduced 2026-10-18) is the release number the
tarball and rpms carry. Every
upgrade, fix, or configuration change gets a row here **in the same session**
it was made; the runbook convention is documented in `CONTRIBUTING.md`.

---

## 2026-11-28 (follow-up) - device toggles, NVML GPU stats, the CPU load tile

### Added
- **Device and key toggles, at last.** The daemon has always been able to
  write the `msi-ec` driver's own device attributes; only the front-ends
  were missing. New CLI subcommands `webcam`, `super-battery`, `fn-key`,
  `win-key` (show plus `on`/`off`, `left`/`right` for the Fn choice), a
  **Devices & keys** card on the GUI's Performance page (one button per
  choice, mirroring the driver's own values), and the missing write keys
  in the daemon's `set`. A firmware that does not expose an attribute
  shows it dead with the reason, and a click that changes nothing is a
  no-op rather than a pointless EC write. The GS66 model now claims
  `webcam`/`webcam_block`/`fn_key`/`win_key` with provenance (upstream
  `CONF_G1_3` defines the registers for this firmware family; the
  attributes are present on this machine, verified 2026-11-28);
  `super_battery` stays `false` because this firmware has no such
  attribute. `selftest` grew a `devices` section that re-writes each
  value where it already stands, the write-back proof.
- **The NVIDIA GPU's clock and utilisation, from NVML.** The
  proprietary driver publishes nothing in sysfs, so the GPU tile could
  only say "no GPU clock published in sysfs" on the machine this
  project is about. The unprivileged front-ends (GUI and CLI, never the
  daemon: the daemon talks to the kernel and nothing else) now load
  `libnvidia-ml.so` through a stateless ctypes probe (init, handle,
  clock, utilisation, shutdown) and merge it over the payload's sysfs
  reading, NVML winning. A machine without the library pays one failed
  load per process, not per poll, and `MSI_NO_NVML` pins the probe off
  so the tests do not depend on the host's video driver. Verified live:
  the GPU row on this laptop reads `1710 MHz` through NVML where it
  read nothing before.
- **A CPU load tile.** The Dashboard's ninth tile: how busy the CPU is,
  as the daemon's own `/proc/stat` delta (needs two samples, so the
  first poll and the offline view honestly say so instead of inventing
  a zero), with the kernel's 1-minute load average as the sub-line.
  The average is one stateless reading, so it lives in
  `system_facts()` and the offline view carries it too, with a "?" where
  the busy percentage would be. `MSI_STAT` and `MSI_LOADAVG` redirect
  the two files for the tests.
- **The Frequency page says what the cores run at now.** A "running
  now" row from the per-policy `scaling_cur_freq`: one number when
  every core agrees (the common case under a single cap), and the
  honest per-core list when they do not, rather than a mean that
  matches no core.
- The offline suite grew to 233 tests: the load math (including the
  zero-delta case that must be `None`, not 0%), the NVML probe and its
  merge preference and refusal to touch the library twice, the device
  round-trips (including the absent-attribute shape and the no-op
  click), the selftest devices section, and the GUI mirror.

### Fixed
- **A docs bug from the overdrive pass.** `APP_DETAILS.md` still said
  the overdrive bit was "not proven for `16V1EMS1`" and the GS66 model
  "ships with the flag `false`", fourteen days after the write-back
  test proved it and the flag was flipped. The section now states the
  proof and its date; a doc that describes the old behaviour is a bug
  in the doc, and this one had a test-green model file contradicting
  it in the same tree.

---

## 2026-11-28 (full pass) - head-to-toe audit: EC read races, half-applied frequency caps, and the machine-wide readings on the Dashboard

### Added
- **The Dashboard reads the whole machine, not just the EC.** Three new
  tiles: **CPU speed** (the mean of the policies' `scaling_cur_freq`
  against the hardware ceiling, with the cap beside it), **GPU speed**
  (i915's `gt_act_freq_mhz`, amdgpu's starred `pp_dpm_sclk` level, the
  discrete card preferred over the iGPU), and **memory** (used of total,
  from `MemAvailable`, not `MemFree`, because caches are not starvation).
  All three are world-readable kernel interfaces, so they ride in the
  status payload (`sys` block) from both the daemon and the offline view,
  cost no EC transfer, and a driver that publishes nothing (the NVIDIA
  proprietary one) gets an honest "no GPU clock published in sysfs"
  instead of a number. The CLI's `status` prints them (a `Freq` line for
  the running speed against the cap, the GPU clock on the GPU row, a
  `Mem` line) and `monitor` carries cpu GHz + ram % on its one line.
  `MSI_MEMINFO` redirects `/proc/meminfo` for the tests, like every other
  path.
- The offline suite grew to 215 tests: the system-facts sources (i915
  actual vs requested, amdgpu starred/unstarred, discrete-wins, the
  no-clock NVIDIA shape, availability-not-free, no-cpufreq), the status
  and offline-view payloads, the CLI lines, and the GUI smoke test pins
  the three tiles' numbers, sources and no-reading states.

### Fixed
- **Battery health silently vanished on real hardware.** The wear
  reading divided `energy_full` by `energy_full_design`, but the
  reference laptop's own `BAT1` publishes the same facts as
  `charge_now`/`charge_full`/`charge_full_design` (µAh) and no
  `energy_*` attributes at all, so the Dashboard and the Battery page
  showed no health on the machine this project is about, while the
  fixture (energy_*) kept the suite green - the same real-versus-fixture
  class as the MSR `isfile()` bug. `_battery_status` now reads both
  spellings and publishes the unit-agnostic `now`/`full`/`full_design`
  pair (plus `charge_*` and `cycle_count`) for the front-ends; the GUI
  and the CLI's `Batt` line use it, and a battery that tracks cycles
  shows the count. Verified live: `msi-control status --direct` on this
  laptop now reads `health 61%` where it read nothing before.
- **EC reads raced with other threads' transfers.** `ec_read`/`ec_write`
  took no lock themselves; `rpm()` (every status poll) and `mux_ec_bit()`
  read unlocked, and the driver's debug interface is
  write-address-then-read-value on *one shared file*, so an unlocked
  pair of transfers in two threads can each receive the other's byte
  (an RPM read once came back carrying a fan-curve temperature byte),
  and an unlocked read between a write and its verify reads back the
  wrong byte and reports a phantom "EC rejected write". The lock is now
  an `RLock` taken *inside* `ec_read` and around `ec_write`'s
  write+verify pair, so every caller is safe by construction; a
  regression test models the shared file and hammers it from four
  threads.
- **A frequency cap set could come back half-applied.** A write failure
  on one policy aborted the whole loop mid-way: the earlier cores were
  capped, the rest untouched, the error was an "internal error" naming
  nothing, and every later status just said "differs per core". Every
  policy is now attempted, all failures are collected into one error
  naming each core, and the floor a policy needed lifted goes back to
  its original value when that policy's cap does not land (a lift is only
  valid under the cap it was raised for; leaving it would pin the CPU
  fast forever).
- **A model with registers for only one fan crashed the other fan's
  first poll.** The registry format allows `ec.curve`/`ec.rpm` for cpu
  alone, but `curve`/`set_curve`/`rpm` indexed the registries directly:
  a KeyError surfaced as "internal error" on the GPU's very first
  status. Both fans now degrade to an honest refusal (status shows a
  dash, the write is refused with the reason), pinned by tests.
- **Three daemon writes shared one tmp filename.** `last.json.tmp`,
  `<profile>.json.tmp` and `mux-backup.json.tmp` are written by whatever
  client thread is saving: two concurrent saves could interleave into
  the same half-written file before `os.replace()` published it. The tmp
  names are now unique per thread; the MUX backup is the one record that
  can undo a graphics switch, so a corrupted one is not academic.
- `wait_for_driver()` used wall clock; a suspend-time step could stretch
  or kill the wait. Monotonic now.
- A firmware that answers the three-mode switch with a *shorter*
  variable raised `IndexError` in the read-back check; it is now the
  ordinary "did not keep the request" refusal with the restore.
- The GUI called `refresh_overdrive(st)` twice per poll (a duplicate
  line, harmless but wasteful), and the Settings config dump was
  re-`setPlainText`ed every 1.5 s, resetting the caret and any
  selection mid-copy; both are now once-and-only-when-changed.

### Changed
- Version 1.1.0 → 1.2.0 (a feature release: the Dashboard tiles and the
  `sys` status block).
- The documented serialisation guarantee now states what the code really
  does: *all* EC access, reads included, is serialised, and why (the
  shared debug file).

---

## 2026-11-22 (check-up) - the undervolt backend was blind on real kernels; display overdrive proven

### Fixed
- **Undervolting was unavailable on every real machine.** `_msr_files()`
  accepted a path with `os.path.isfile()`, and `/dev/cpu/*/msr` is a
  *character device*, for which `isfile()` is False: the daemon reported
  "no MSR devices (/dev/cpu/*/msr)" on hardware that had a healthy
  OC mailbox, the page rendered as unavailable, and every undervolt write
  path was dead air. The offline suite could not catch it (fixture msr
  nodes are regular files); it surfaced in the check-up's live pass.
  Accepted nodes are now counted with `os.path.exists()`, and a regression
  test fakes the real shape with a FIFO.
- **An unopenable MSR node could sink the whole status payload.**
  `os.open` failures in the mailbox read/write path (permissions, or ENXIO
  once a driver is gone again) are backend facts now: `status()` degrades
  the undervolt block to "unavailable" and the CLI names the reason,
  instead of the old unhandled `OSError` that failed the entire status
  command ("internal error: Permission denied"). Found by the same
  regression window: the suite's real-daemon process runs unprivileged.

### Changed
- **Display overdrive is proven on `16V1EMS1.118` and ships enabled.**
  Prior art: the same-generation GP76 10UG toggles its panel overdrive
  with EC `0x2e` bit 4, confirmed by that machine's owner writing the bit
  under Linux (Blur Busters thread 8627); this byte already carries this
  model's webcam bit 1 and MUX bit 6. The write-back test on this own
  machine (2026-11-22, through the daemon's validated one-bit RMW):
  the firmware accepted and read back both directions, the neighbour
  bits and every other model-owned register stayed byte-identical, and
  the original state (overdrive ON, `0x5b`) was restored. `msi-control
  overdrive`, the Performance page's toggle and the status readout are
  live on this laptop; visual confirmation of the panel's response-time
  change remains a human check.
- The check-up's regression count: 194 offline tests (the MSR-blindness
  FIFO test and the shipped-model overdrive claim joined).
- `Card`'s docstring still described the pre-restyle caption (the
  caller's own case); it now describes the uppercase tracked caption
  the code draws.

---

## 2026-11-22 (later) - rare-style refresh, quiet copy, start-with-session

### Added
- **Start with the session, optionally in the tray.** Settings carries a
  Startup card. "Start with the session" writes the standard XDG autostart
  entry for this user (`~/.config/autostart/msi-control.desktop`); "Start in
  the tray" adds `--hidden` to its Exec line, and a hidden start opens no
  window: the tray icon is the way in. A machine without a system tray shows
  the window anyway, because a tray-less hidden start is an unreachable app,
  and a second launch of an already-running copy hands over without raising
  its window when the launch was `--hidden` (the running copy is doing what
  the session asked of it). The desktop file is the whole truth about how
  the session will start: nothing is mirrored in QSettings where the two
  could drift apart, the boxes rewrite it the moment they are clicked, and
  `check_autostart` asserts the file rather than the checkboxes.

### Changed
- **rareui-style refresh of the surfaces, within the same token set.** Card
  edges are one step stronger than hairline (the structure now comes from
  the visible edge rather than colour contrast alone), the selected nav
  row wears the accent tint every other selection wears, card captions
  are tracked uppercase micro-labels, and buttons carry accent press and
  focus rings. Every colour pair still clears the WCAG AA checks the smoke
  suite measures, in both themes and for every accent preset.
- **The copy stopped explaining itself.** The hint paragraph is gone from
  every card that carried one (Performance, Cooler Boost, the MUX block,
  re-apply/factory, the self-test, tray ink, sections, the dashboard's
  system card, the fan editor, battery, slots); tool tips carry the
  reasons a control needs where one is needed (e.g. what each shift mode
  trades), and the one warning that earns its keep stays: the MUX card's
  one-paragraph note, because a reboot-backed firmware switch is the one
  control where pretending it is as safe as a fan mode is the worse lie.
  The undervolting page's BIOS notice is a single sentence naming the
  three BIOS switches it depends on.
- The shipped `desktop/msi-control.desktop` carried its Comment across two
  lines, which is invalid in the format and earned a parse warning from
  the desktop on every login; it is one line now, and `Categories` drops
  its second main category ("System"), which desktop-file-validate
  flagged as a possible double listing in the menu.

---

## 2026-11-22 - undervolt writes that never landed, one GUI per user, tray-quiet polling

### Fixed
- **The GUI and the CLI sent the daemon an `undervolt` verb it never
  had.** The Undervolting page (Apply, and the startup set re-apply on the
  first status) and `msi-control undervolt set --yes` all sent
  `"action": "set"`, while the daemon answers `show`, `apply` and `reset`
  and refuses anything else. With the config gate shut, both sides hit the
  gate first and the wording hid the difference; with `AllowUndervolt` on,
  every real write came back `unknown undervolt action 'set'`, the page
  said "failed" and no offset ever reached the MSR. The clients now speak
  `apply` (the CLI subcommand keeps its user-facing `set` name, and the
  page's hint text with it), and a new offline test serves the gate-open
  service and drives the exact command line end to end, because exercising
  the write only through the gate-off path is how the mismatch survived.
- **Apply on the Undervolting page crashed before the confirmation
  dialog.** The dialog's message iterated `core.UV_PLANES` with a single
  loop variable, so the plane name was a `(name, index)` tuple and
  `vals[("core", 0)]` raised `KeyError` on every Apply click: the dialog
  never opened, nothing was written, and because the click was
  dialog-gated the smoke suite had never pressed it. It does now, with
  the dialog auto-answered, and it pins the exact socket payload, which is
  exactly how this one came to light.
- **The GUI could be started twice.** The daemon refuses a second
  listener; the window never checked anything, and a copy hidden in the
  tray does not look like a copy at all: each extra instance polled the
  same daemon on its own 1.5 s beat, painted a second tray icon,
  re-applied the startup undervolt set and argued over the same saved
  window state. A second launch now probes a per-UID `QLocalServer` name
  before any window is built (`InstanceGuard`): the running copy is asked
  to show its window and the new process exits 0, so clicking the menu
  entry with the app already running focuses it. A socket file left by a
  crashed copy is removed only after the probe finds nobody listening.
  `check_single_instance` pins the handover and the stale-lock recovery.

### Changed
- **Hidden to the tray, the app polls the hardware ten times slower.**
  One `status` is a full read-back: ~31 EC-byte transfers, ~160 OC-mailbox
  MSR operations (five planes times every logical CPU), ~50 sysfs reads,
  and a fresh thread per request, every 1.5 s, forever, only to keep a
  tray badge fresh. The poll now runs at `POLL_MS_HIDDEN` (10 s) while the
  window is hidden and returns to `POLL_MS` when it is shown, plus one
  immediate poll on reopening so the window is never a stale reading.
  Nothing changes while a window is on screen. Two new smoke checks pin
  both sides of the show/hide state machine.

---

## 2026-11-15 (night) - frequency cap fixed on real intel_pstate, frequency slots in the tray

### Fixed
- **The frequency cap reported a refusal on every set after the first.**
  Real hardware caught what the fixtures could not: Intel cpufreq
  (intel_pstate with HWP, observed on the i7-10875H) applies a new
  `scaling_max_freq` a few milliseconds *after* `write(2)` returns, so the
  daemon's immediate read-back saw the previous cap and answered
  `core policy0: read back 5.10 GHz` for a write that had in fact stuck.
  The write was real (the cap landed; the error was the lie), which is
  exactly the wrong way round. The read-back is now a short poll with an
  80 ms deadline per policy; a genuine refusal still surfaces once the
  deadline passes (tested with a patched read that never converges).
  Two regression tests simulate the lagging attribute.

### Added
- **A stored Frequency slot now applies on click.** The page slots used
  to only preload the slider, so a stored cap still needed an Apply press
  that the hand-written desktop scripts never did; and while the
  read-back bug above lived, the Apply step was the one that said
  "failed" for a write that had stuck. A slot click now sends the value
  straight through the daemon's validated writer (the same `freq set`
  payload the tray submenu sends), and if the daemon is down the slot
  says so in a flash instead of pretending. `check_frequency_page` pins
  the exact command a click produces.
- **The tray menu gained a CPU frequency cap submenu**: the Frequency
  page's four quick slots (read from the page's own QSettings keys, so
  the tray can never disagree with the page about what slot 1 holds,
  empty slots hidden) plus a Full speed action that re-posts the hardware
  maximum. Both go through the daemon's validated writer; the checks
  mirror the live cap exactly like the mode actions beside them.
  `check_tray_menu` now pins the submenu's labels, checked states and
  the exact command payloads a click produces, offline and on.

---

## 2026-11-15 (evening) - tray quick actions, hardware self-test, profiles with CPU settings

Three integrations from the review pass; keyboard backlight and NVIDIA clock
controls were considered and dropped as out of scope.

### Added
- **Tray quick actions.** The tray icon now carries a menu with the everyday
  switches: show the window, the four performance modes as radio entries,
  Cooler Boost, and the 60/80/100 charge limits. Everything routes through
  the window's own `submit` path, so it is the same validated, read-back
  daemon write a button press is; the checks mirror the latest status poll
  rather than writing anything themselves. The menu is built even where no
  system tray exists, which is what lets `check_tray_menu` pin its state,
  the firmware-unavailable modes being hidden, and everything going dead
  when the daemon is offline.
- **A hardware self-test as a command** (`msi-control selftest`, plus a
  Settings card in the GUI): `verify.sh` ported onto the daemon's own
  validated primitives. It walks every shift and fan mode, blips Cooler
  Boost, round-trips the charge limit (80 then 60), re-writes each fan
  curve where it already is, re-caps every core where it already is, and
  reads every change back, then restores the machine exactly as it found
  it whatever happened on the way. It is gated twice over: the CLI refuses
  without `--yes` and the GUI asks a confirmation that names what gets
  written. Its writes never touch `last.json` (it goes through the EC
  backend, not the journaling dispatch), so a future boot cannot replay a
  value it was merely probing. `--pr`, and the card's Copy button, hand
  over a paste-ready markdown report for a model pull request, free of
  hostname, serial and home paths. One honest footnote: the charge check
  verifies the end limit read-back as ours, and reports the start
  threshold informationally, because `start = limit - 10` is the msi-ec
  driver's doing, not this program's write; a firmware that pins it is a
  passing "held at its own value", never a silent success.
- **Saved profiles can carry the CPU-side settings.** `save-current`
  snapshots the all-core frequency cap as `freq_khz` and any nonzero
  voltage offsets as `undervolt`. Applying follows the same
  absent-means-untouched rule as `charge_end`: built-ins mention neither
  key, so they never touch your CPU settings. Offsets are stored as the
  planes named and applied with `fill_missing=False`, so a profile cannot
  zero an offset it did not mention, and they obey the `AllowUndervolt`
  gate: with it off the rest of the profile applies and the offsets are
  skipped with a warning.
- **A selectable accent.** Six presets in Settings (MSI red, blue, purple,
  teal, orange, pink), each carrying one pair per theme, because the swatch
  that reads on near-black turns to mud on white. Applying one re-styles the
  app live: the stylesheet and palette are rebuilt, and the sidebar is
  rebuilt too because nav icons bake the accent into their selected pixmap.
  Everything else is derived, not hand-tuned: `on_accent` picks black or
  white by luminance, and `accent_ink` (the selected-row ink) is pushed
  toward white or black in fixed steps until it clears AA on the accent's
  own soft tint, which the bare accent fails at ~2.6:1. The shield mark
  stays MSI red whatever is picked, because the mark is the brand. The
  choice lives in QSettings (`style/accent`) beside the geometry. Every
  preset is measured in both themes by the smoke test. Plus a smoke-suite
  hardening found on the way: the output dir (and therefore
  `XDG_CONFIG_HOME`) is absolutised, so a run invoked with a relative path
  can never fall back to the developer's real `~/.config` (Qt ignores a
  relative `XDG_CONFIG_HOME` and writes home instead, which is exactly the
  leak the scratch config dir exists to prevent).
- 16 offline tests (validation of the new profile keys, the profile
  round-trips, TestSelftest for pass/failure/restoration/CLI gating;
  189 total) plus `check_tray_menu` and `check_selftest_card` in the GUI
  suite. Screenshots regenerated for both themes. Docs updated: README
  (Use, Verify, tray paragraph, profiles, troubleshooting), APP_DETAILS
  (protocol, profiles table, UI table, tray section, smoke-checks table),
  AGENTS (CLI subcommand list, self-test next to `verify.sh`).

---

## 2026-11-15 (latest) - CPU frequency caps, Intel undervolting, panel overdrive

Three new control surfaces, two of them deliberately conservative. All three
go through the daemon: it owns every hardware write, and the offline suite
grew fake cpufreq policies and a fake OC mailbox to prove it.

### Added
- **CPU frequency configurator** (`msi-control freq`, the Frequency page): one
  cap for every core's `scaling_max_freq`, clamped to `cpuinfo_min..max`,
  lifting a policy's min when the cap drops below it, read back per policy
  by the daemon. Four quick slots store day-to-day caps in QSettings.
  Deliberately not part of the re-apply snapshot: the kernel resets the caps
  at boot and a cap is a live decision, not a machine state.
- **Intel undervolting** (`msi-control undervolt`, the Undervolting page):
  the ThrottleStop protocol, MSR `0x150` OC mailbox, five planes (core, gpu,
  cache, uncore, analogio), clamped to `0..-300 mV`. The encoding matches
  ThrottleStop byte for byte (`-175 mV` = `0xE9A00000`, checked on the
  dev machine too). Safety design exactly as requested:
  offsets are **volatile** (they clear at reboot and suspend, and nothing
  persists them daemon-side), a separate *Use at startup* button saves a
  set the **GUI re-applies when the app starts**, never a boot path; the
  page carries a permanent notice that the BIOS must have undervolting
  unlocked (Overclocking Lock / CFG Lock / Undervolt Protection off in the
  Advanced OC menu) and the daemon reports a locked BIOS (a write that
  does not read back) naming that route instead of claiming success.
  Writing needs `"AllowUndervolt": true` in the config plus `--yes` on the
  CLI / a confirmation dialog in the GUI.
- **Display overdrive** (`msi-control overdrive`, a Panel card on the
  Performance page): the Dragon Center panel toggle, one EC bit addressed
  per model. A model that proves it carries `features.display_overdrive`
  plus `ec.overdrive` (`address`, `bit`, optional whole-byte `off`/`on`);
  the write is a one-bit read-modify-write of the same `0x2e` that holds
  the webcam (bit 1) and the MUX state (bit 6). **The flag ships `false`
  for the GS66**: bit 4 was measured on a same-generation GP76 (Blur
  Busters forum) but never on `16V1EMS1`, and the repo's rule is that a
  register is proven or the feature stays off; the GUI and CLI say
  "no proven register" rather than flipping an unwatched bit.
- Settings can hide the Frequency and Undervolting sections entirely;
  a hidden section also leaves the sidebar and the `Ctrl+N` cycle.
- The sections were renumbered (`Ctrl+1..8`): Dashboard, Performance,
  Frequency, Undervolting, Fans, Battery, Profiles, Settings.
- Protocol: `freq` (show/set), `undervolt` (show/apply/reset, gated on
  `AllowUndervolt`), `overdrive` (show/set, model-gated); `status` carries
  freq facts, the overdrive state and the current voltage planes.
- 26 offline tests (TestFrequency, TestUndervolt, TestOverdrive and the
  pure-function additions; 173 total) plus GUI smoke checks for the three
  new pages (pending-cap state, slot store/clear, BIOS notice, startup-set
  save/delete, offline-disables-writes extended). Docs updated everywhere:
  README (three new sections + troubleshooting rows + feature list),
  APP_DETAILS (§2 diagram, the non-EC writes subsection, protocol, UI
  table, tests), models/README (`ec.overdrive` + `display_overdrive`),
  AGENTS (counts, ec table line, env table `MSI_CPU_DEV`).

### Changed
- The base test fixture now carries two fake cpufreq policies and a fake
  `/dev/cpu` tree (`MSI_CPU_DEV`), so a test run no longer depends on the
  host having (or not having) MSRs and the undervolt backend is always
  exercised offline.
- `Service.status()` gained `freq`, `overdrive` and `undervolt` keys and
  echoes `AllowUndervolt` in its config block.

---

## 2026-11-15 (later) - MUX: the phantom "next boot: Discrete" and the three-mode protocol

The settings app claimed "next boot: Discrete, reboot required" forever on a
machine that booted hybrid every time. It was not the BIOS being confused; it
was this program misreading a byte. Found by watching the UEFI variable get
rewritten one second after every boot.

### Fixed
- **A phantom graphics request on firmwares that carry none.** The program
  read byte 1 of `MsiDCVarData` as "the mode to apply at next POST", which is
  what that byte means on the Alpha 17 the reference tool was written for.
  On this machine (`16V1EMS1`) the firmware itself rewrites byte 1 right
  after POST to mirror the mode the BIOS applied; it holds the current mode,
  not a request, so a hybrid BIOS setting looked like a pending discrete
  switch that would never apply. A variable-side request is now only believed when the
  three-mode marker bit (byte 5 bit 4) is present, and firmwares without it
  get an honest "the variable carries no documented request" instead of a
  guess.

### Added
- **Three-mode protocol support** (newer Optimus firmware, the second backend
  of ElXreno/msi-gpu-switcher): the request lives in byte 5 bits 0-1
  (hybrid/discrete/integrated), bits 2-3 name the applied mode, bit 5 says
  whether integrated exists and bit 6 set means discrete is not supported.
  A switch is byte 5, the efivar write, the `0xd1` trigger and an
  acknowledge poll, exactly what MSI Center sends; the GUI disables the
  discrete button when the firmware flags it unsupported rather than writing
  a request that cannot be honoured. The legacy path (byte 1, trigger,
  `0x2e` bit 6) is kept for firmware that speaks it; the dialect is detected
  from the variable at runtime, never configured per model.
- Five daemon tests for the three-mode handshake (write facts, refusal,
  no-acknowledge error, revert), a caps-refused state in the GUI smoke test,
  and provenance in README, APP_DETAILS and the GS66 model README. A software
  discrete switch on `16V1EMS1` is still **unproven**, and the docs say so;
  the tested route on that machine is the BIOS setting.

---

## 2026-11-15 - minimalist UI pass (quiet surfaces, pill controls) + three repairs

The GUI keeps its structure (token themes, the one-rung control heights, the
one type scale, every invariant the smoke test pins) but reads differently:
near-flat chrome, cards that float on the background, pill-shaped buttons
and a rounded neutral sidebar selection, with the red accent kept as the
one saturated colour. Plus three small repairs found while auditing: a
registry re-parse on every GUI poll, a private-attribute reach-in inside the
daemon, and message-sniffing where the CLI decides "daemon not running".

### Changed (GUI look, no behaviour)
- **Quiet surface language.** New palettes: the light theme is the
  `#f5f5f7`-on-white settings-app look, the dark theme a near-black
  `#0a0a0c` with `#151518` cards. The header, sidebar and status bar now
  share the window background with no separator lines, so a page is a set
  of cards floating on one surface rather than panels inside a frame.
- **Pill buttons.** New `R_BTN` token (18 px, exactly half the 36 px control
  rung, pinned in the smoke test); every button is a full pill, spin boxes
  and line edits stay at the 10 px control radius. Sidebar selection is a
  rounded neutral pill (`surface3`) instead of the red left rule; the
  accent stays on the icon, so red keeps meaning "interactive", not
  "chrome".
- **Bigger radius, more air.** `R_CARD` 16 to 20, `R_INNER` 12 to 14,
  `PAGE_PAD` 20 to 24, `PAGE_GAP` 14 to 16; cards, progress bars and the
  slider groove slimmer (`H_BAR` 8 to 6, `R_BAR` 5 to 3); the slider handle
  grows to a 20 px white circle.
- **Sentence case everywhere a chip used to shout.** Card captions keep the
  caller's case instead of upper-casing (no tracking on lowercase), the
  temperature bands read "Warm" not "WARM", and the battery chip keeps the
  kernel's own spelling ("Not charging"). The smoke test's pinned labels
  were updated to match. The Cooler Boost hero keeps its all-caps brand
  name, deliberately.
### Fixed
- **The GUI re-parsed the whole model registry on every poll.**
  `SettingsTab.refresh()` called `core.model_errors()`, which re-opens and
  re-parses every `models/*/model.json`, every 1.5 s, on the GUI thread.
  The registry cannot change while the process lives, so the errors are
  read once at startup.
- **`msi-controld` reached into `MsiEc._p()`** to test for the debug
  interface; that is now the public `MsiEc.debug_present()`.
- **The CLI's `_set()` sniffed error text** ("No such file", "errno") to
  decide whether to print the "start the daemon" hint. `OSError` (transport
  failure, the actual offline case) and `ProtocolError` (the daemon
  answering "no") are now caught separately.
- **`check_int()`'s float clause** relied on operator precedence that was
  correct but easy to break while reading it; parenthesised.
- Screenshots in `tests/shots/` (dark and light) regenerated for the new
  look.

---

## 2026-10-19 - release preparation: licence, packaging, model-PR guides

The pass that turns the tree into something publishable: a licence, an rpm
and tarball build, and two step-by-step guides for the project's most common
contribution, adding your laptop's model.

### Added
- **`LICENSE`, GPL-2.0-or-later.** The repo had none, which is a blocker for
  any public release; the vendored driver is GPL-2.0-or-later in its own
  right, so the license file is copied from the GPL-2 text already carried
  in `third_party/msi-ec/` and README.md grew a License section stating
  what is ours, what is upstream's, and which licence copies from other
  projects may legally bring a register map in.
- **Release builds: `make release`.** Produces
  `dist/msi-control-<VERSION>.tar.gz` plus `msi-control`,
  `-driver-dkms` and `-gui` rpms (and a `src.rpm`) from `dist/`, and
  `dist/` is gitignored, so an artifact can never be committed while its
  version lives in `src/msi_control.py`. `VERSION` is read out of
  `src/msi_control.py` by the Makefile, so the tarball, the rpms and
  `msi-control --version` cannot drift apart.
- **`packaging/rpm/msi-control.spec`.** Three packages matching who needs
  what: `msi-control` (daemon, CLI, models, systemd unit + resume hook),
  `msi-control-driver-dkms` (the driver source under `/usr/src/` plus
  non-fatal DKMS build/install hooks: missing kernel-devel defers to the
  next boot instead of failing the transaction) and `msi-control-gui`
  (`python3-pyside6` dependency quarantined there). The unit and sleep
  hook are sed'd to `/usr` paths; README's packaged-install section says
  to `./uninstall.sh` a checkout install first, because a manual unit
  under `/etc/systemd/system` shadows the packaged one.
- **CONTRIBUTING.md § "Adding your model" is now the full user guide**:
  detect, draft, dynamic register identification with a diff table,
  proving each feature flag (including the MUX warning), `verify.sh`,
  and a complete PR checklist with what a reviewer accepts and rejects.
- **AGENTS.md § 6 gained agent-specific PR rules** for the same workflow:
  never run `verify.sh` unless the user asked; never invent a register;
  commit only the model directory; ship a `draft` status as a complete
  contribution.
- **`.gitignore` covers release and tool artifacts**: `rpmbuild/`,
  `*.rpm`, `*.tar.gz`, and the common test-tool caches, alongside the
  existing `dist/`.

### Fixed
- **`make check` failed on the smoke test's action-message assert** after
  the 2026-10-18 dash-to-parenthesis decoration pass changed
  "applied - nothing to change" to "applied (nothing to change)": the
  source no longer contains the dash, but the test still asserted the
  message *ends with* "nothing to change", which a trailing parenthesis
  drops. The test now asserts the full sentence, matching the UI string
  that ships instead of a fragment of it.

---

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
