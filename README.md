# MSI Linux Control

A Linux control centre for **MSI laptops** — the things MSI's Dragon Center
does on Windows, on Linux, without Dragon Center:

* **Battery charge threshold** ("stop charging at 60/80/100%") — the single
  biggest thing you can do for a laptop battery's long-term health.
* **Performance modes** — MSI's *shift* modes: eco / comfort / sport / turbo.
* **Fan modes** — auto / silent / advanced, plus **Cooler Boost** (both fans
  pinned at 100%).
* **Custom fan curves** — 6 temperature thresholds × 7 speed levels per fan,
  edited by dragging points on a graph.
* **Profiles** — save any of the above under a name, including a `factory`
  profile captured from the EC so you can always go back.
* **Graphics mode (MUX)** — run the panel off the Intel iGPU (hybrid) or the
  NVIDIA GPU alone (discrete). Off by default; read the warning first.
* **Live monitoring** — CPU/GPU temperature, fan duty and **RPM**.

Not every laptop is the same, so the laptop-specific facts live in a **model
registry** under `models/` — one small JSON file per model, holding that
machine's EC addresses, the features its firmware has, and its built-in
profiles. Not sure whether yours is supported?

```bash
msi-control detect          # what laptop am I, and is there a model for it?
```

A laptop with no model file still **works**: everything the `msi-ec` driver
exposes by itself — temperatures, shift and fan modes, Cooler Boost, the charge
limit — keeps going, and only the parts that need per-model registers (fan
curves, RPM, the graphics switch) are switched off rather than guessed at. The
GUI says so and points you at `detect`. Adding your model is a documented,
maybe-an-hour job — see [`CONTRIBUTING.md`](CONTRIBUTING.md) §
"Adding your model".

Three pieces, deliberately separate:

```
msi-control-gui  ──┐
msi-control ─┼──► Unix socket ──► msi-controld (root) ──► /sys/.../msi-ec + BAT*
            ┘        JSON             ▲
                                 msi-ec kernel driver
                                 (out-of-tree, debug=1)
```

Only **`msi-controld`** runs as root and only it writes to the EC. The GUI and the
CLI are unprivileged clients. Every value they send is validated, every EC
write is read back, and with the daemon down the GUI degrades to read-only
rather than lying to you.

---

## Why an out-of-tree driver

The kernel **does** ship an `msi-ec` driver (`drivers/platform/x86/msi-ec.c`,
`CONFIG_MSI_EC=m`), and on this machine it is already built. It is not enough:

* it exposes **only** the battery charge thresholds;
* it has no shift modes, no fan modes, no Cooler Boost, no curves, no RPM;
* it does not know this EC firmware.

The vendored driver is [**teackot/msi-ec**](https://github.com/teackot/msi-ec)
**v0.13**, which explicitly lists this machine's firmware:

```
16V1EMS1.118 // GS66 Stealth 10SE
```

and — critically — ships a **debug interface** (`debug/ec_get`, `debug/ec_set`,
`debug/ec_dump`) that is the *only* route to the fan-curve and tachometer
registers on this kernel. This box has no `ec_sys` module and
`CONFIG_ACPI_EC_DEBUGFS is not set`, so without that debug interface there is
no raw EC access at all. It is enabled by loading the module with `debug=1`,
which `install.sh` sets in `/etc/modprobe.d/msi-ec.conf`.

It installs into `/lib/modules/$(uname -r)/updates/drivers/platform/x86`,
which **shadows** the in-tree driver. That is intended and reversible
(`uninstall.sh`).

> The driver lives in `third_party/msi-ec/` — vendored, not a submodule, so
> the DKMS build needs no network.

---

## The EC register map (worked example: GS66, firmware family 16V1EMS1)

Every register below is one **model's** — the GS66 Stealth 10SE's — and in the
program it lives in [`models/gs66-stealth-10se/model.json`](models/gs66-stealth-10se/model.json),
not in the code. The section is kept here because it is the detailed write-up
of the map and the reasoning behind it; a model for another laptop is the same
shape with different addresses (see [`models/README.md`](models/README.md)).

Cross-checked between three independent sources: the [isw](https://github.com/YoyPa/isw)
project's `16V1EMS1` section (which carries a real EC dump from a GS66-10SE,
issue #220), teackot's `CONF_G1_3` configuration, and the in-tree driver's
charge-threshold semantics. All three agree.

| What | Address | Values |
|---|---|---|
| Performance (shift) mode | `0xf2` | eco `0xc2` · comfort `0xc1` · sport `0xc0` · turbo `0xc4` |
| Fan mode | `0xf4` | auto `0x0d` · silent `0x1d` · advanced `0x8d` |
| Cooler Boost | `0x98` | bit 7 |
| Charge threshold | `0xef` | end = raw − `0x80`; start = end − 10 |
| CPU temperature | `0x68` | °C |
| CPU fan duty | `0x71` | % |
| GPU temperature | `0x80` | °C |
| GPU fan duty | `0x89` | % |
| CPU curve — temps | `0x6a`–`0x6f` | 6 thresholds, °C |
| CPU curve — speeds | `0x72`–`0x78` | 7 levels, % |
| GPU curve — temps | `0x82`–`0x87` | 6 thresholds, °C |
| GPU curve — speeds | `0x8a`–`0x90` | 7 levels, % |
| CPU tachometer | `0xcc` | 2 bytes big-endian **period**; `RPM = 478000 // period` |
| GPU tachometer | `0xca` | same |

Not every register exists on every firmware revision: `super_battery`,
`webcam_block` and `fn_key`/`win_key` are **unsupported** on this config and
read back as absent. The GUI disables those controls rather than pretending.

### The fan-curve semantics are a step function

The fan runs at `speeds[0]` until the temperature *passes* `temps[0]`, then at
`speeds[1]`, and so on. Seven speeds, six thresholds. The GUI draws exactly
this shape (a staircase, not a line) because drawing an interpolation would
show you a curve the EC never follows.

### The `RPM = 478000 // period` constant

The tachometer bytes are a **period count**, not a speed. isw derives the
constant empirically; it is reproduced verbatim here because a wrong constant
prints plausible-looking nonsense (off by a factor, but still a believable
four-digit number).

---

## Install

```bash
./install.sh          # re-execs itself through pkexec for the root bits
```

Prerequisites, all checked before anything is written:

* **Secure Boot disabled** — an out-of-tree module will not load otherwise;
* `kernel-devel` for the *running* kernel (`uname -r`);
* `gcc`, `make`;
* `dkms` (the installer will `dnf install` it if missing, and falls back to a
  direct `make` + `depmod` if DKMS really will not work);
* `python3-pyside6` for the GUI — **only** the GUI needs it.

> The GUI is written against **PySide6** (the Qt for Python official binding).
> PyQt6 and PySide6 are API-compatible for everything used here; the
> difference is the import name and the licence (LGPL vs GPL).

What it does: builds and installs the driver via DKMS, writes
`/etc/modprobe.d/msi-ec.conf` (`options msi-ec debug=1`) and a
`modules-load.d` entry, copies the daemon/CLI/GUI to `/usr/local`, writes a
default `/etc/msi-control/config.json` (only if none exists), enables the
`msi-controld` systemd service, and installs a resume hook so settings
survive suspend.

`uninstall.sh` reverses all of it; `--purge` also removes your profiles and
config.

### Verify

```bash
msi-control status            # everything, from the daemon
msi-control detect            # is this laptop's model file present?
ls /sys/devices/platform/msi-ec/debug   # present ⇒ RPM + curves available
systemctl status msi-controld
```

---

## Use

```bash
msi-control status                       # full state
msi-control detect                       # identify this laptop + its model file
msi-control monitor                      # live view
msi-control mode sport                   # eco|comfort|sport|turbo
msi-control fan advanced                 # auto|silent|advanced
msi-control boost on                     # Cooler Boost
msi-control battery 80                   # stop charging at 80%
msi-control curve                        # show both curves
msi-control curve cpu --temps 58,63,68,73,78,83 --speeds 0,50,60,65,70,80,85
msi-control profile list
msi-control profile save quiet-office
msi-control profile apply gaming
msi-control graphics                      # graphics mode (MUX): hybrid | discrete
msi-control graphics discrete --yes       # switch it (reboot to apply)
msi-control reapply                      # push persisted state back into the EC
```

…or run `msi-control-gui` (also in your application menu as **MSI Linux Control**). It has
six sections, reached from a sidebar: Dashboard, Performance, Fans, Battery,
Profiles, Settings. The window, the menu entry and the tray all carry the same
mark — MSI's red shield with **Tux** on it, and nothing else: it is built to be
read at the 16–22 px a taskbar actually renders, where a tile and a model
number turn into a dark square with a smudge in it — and the tray's copy is
tinted by the hottest temperature and labelled with it, so you can read the
machine's heat without opening anything. The Performance section also carries
the graphics switch, for the same reason MSI Center puts it there.

If no model file matches your laptop, the window carries an amber banner saying
so, the Fans section goes read-only with a notice, and the Settings section
shows the model and where to get one — the app degrades to what the firmware
really supports instead of offering controls it cannot honour.

### The GUI

The interface is a header, a sidebar, and one page at a time — not a strip of
tabs — so the window reads as one system at any size:

* **MSI's colours, and one meaning per colour.** The accent is MSI red, and it
  is the app's only "this is selected / this is your value" colour. The
  green→amber→orange→red band is reserved for temperature, so a red bar always
  means *hot* and never *chosen* — which is why the battery tile, a percentage
  and not a temperature, wears the accent instead. The offline banner and the
  offline pill share one red, because they describe one condition.
* **Light and dark themes from one token set each.** The theme follows your
  desktop's colour scheme; set `MSI_THEME=light` or `MSI_THEME=dark` to force
  one. Every colour in the app comes from that set, so a theme change cannot
  leave a hardcoded grey stranded on a white background — and the *contrast*
  between text and its background is asserted in the test suite (the amber
  "hot" colour used to put white text at 2.9:1, which looks fine in a mockup
  and is unreadable in daylight). The two sets are asserted to define the same
  token keys, so a theme cannot silently lose a border.
* **One type scale, in pixels.** `setPointSize` and `setPixelSize` were mixed,
  so "the big number" rendered at two different heights depending on the page;
  every size now comes from one rung (`TYPE_MICRO` … `TYPE_DISPLAY`), and the
  smoke test asserts the five dashboard tiles agree.
* **One control height.** Every button, field, spin box and sidebar row is the
  same height, from a single token, because widgets used to carry their own
  minimum height — which left the battery page's `Apply` standing 10 px taller
  than the `60%` preset beside it. Only the cooler-boost button (the tab's one
  big switch) and the charge slider are deliberately different, and the smoke
  test measures all of it.
* **A connection pill that tells the truth.** It reads *connecting…* until the
  daemon has answered once, *daemon online* while it is up, and *daemon
  offline* when it is not — in which case a banner appears stating that every
  reading is read-only and how to start the daemon. The app degrades to
  read-only; it does not pretend. The firmware in use sits permanently at the
  right of the status bar, which is where it used to be printed as a message
  that the next poll overwrote.
* **Keyboard first.** `Ctrl+1…6` move between sections, `Ctrl+R` re-reads
  everything, `Ctrl+Q` quits. The fan-curve points can be placed and nudged
  entirely from the keyboard.
* **Nothing is written until you say so.** The charge slider and the curve
  editor both hold a pending value with Apply/Revert, so a slow drag never
  reprograms the charger and a half-finished curve never reaches the EC.
* **The fan curve is your setting, so it is drawn in the accent.** Where the
  machine is *now* is a separate marker in its temperature colour, labelled
  with the reading — the curve itself used to be tinted by the live
  temperature, so an unchanged curve changed colour from minute to minute.

Both themes are checked in under `tests/shots/` and `tests/shots/light/`.

### Built-in profiles

| Name | What it does |
|---|---|
| `silent` | eco + silent fans |
| `balanced` | comfort + auto fans (the everyday default) |
| `gaming` | sport + advanced fans + a custom curve |
| `battery` | eco + auto fans, **charge limit 60%** |
| `factory` | *captured from your EC on first run* — exactly what the laptop shipped with |

`factory` is written on first start by reading the EC's own curve, so it is
your guaranteed way back.

**Only `battery` sets a charge limit.** Applying `silent`, `balanced`,
`gaming` or `factory` leaves your charge limit exactly where it is. The limit
is a battery-health choice rather than a performance one, so a performance
profile has no business moving it — previously all three carried
`charge_end: 100` and applying one silently undid a limit you had deliberately
picked. The rule is uniform: **a profile that does not mention a charge limit
does not touch one.**

### Config — `/etc/msi-control/config.json`

The default is the repo's [`etc/config.json`](etc/config.json); `install.sh`
copies it in on first install and never overwrites an existing one.

```json
{
  "RestoreLast": true,        // re-apply last state at boot
  "DefaultProfile": null,     // apply this profile at boot instead (wins over RestoreLast)
  "AllowRaw": false,          // expose the raw EC peek/poke (`msi-control raw`)
  "AllowMuxSwitch": false,    // allow the graphics-mode (MUX) switch
  "SocketGroup": "wheel",     // who may talk to the daemon
  "ReapplyOnResume": true     // re-apply after suspend
}
```

`AllowRaw` is **off** by default on purpose: one wrong EC byte can crash the
embedded controller and take the machine with it. Everything the GUI and CLI
normally do goes through validated paths that read back after every write.

`AllowMuxSwitch` is **off** for a different reason — see below.

### Graphics mode — the MUX switch

A laptop with two GPUs has to pick which one drives the built-in panel:

| Mode | What it is | Who wants it |
|---|---|---|
| **Hybrid** | The Intel iGPU drives the panel; the NVIDIA GPU renders on demand and hands frames over (Optimus) | most of the time — the dGPU can power down, so the machine runs cooler and the battery lasts longer |
| **Discrete** | The NVIDIA GPU drives the panel directly; the iGPU is switched off | maximum frame rate, no compositing hop, every frame through the dGPU |

```bash
msi-control graphics                 # show it
msi-control graphics hybrid  --yes   # request it; takes effect at next boot
msi-control graphics revert          # undo the last switch
```

It needs `"AllowMuxSwitch": true` in the config **and** `--yes` on the command
line (the GUI asks with a dialog instead). A switch requires a **reboot** to
take effect, and this program will never reboot your machine for you.

#### ⚠️ Read this before you switch

* **The switch writes bytes outside the documented EC map** — register `0x2e`
  bit 6 (MSI's "PXCT" bit), the trigger byte at `0xd1`, and byte 1 of the UEFI
  variable `MsiDCVarData` (`dd96baaf-145e-4f56-b1cf-193256298e99`). These are
  the only addresses this program writes outside its documented setting map,
  and `msi-ec` itself has **no MUX support at all**, so none of it is
  vendor-documented for Linux.
* **The mechanism is reverse-engineered.** It is cross-checked against
  [`ElXreno/msi-gpu-switcher`](https://github.com/ElXreno/msi-gpu-switcher),
  which drives the identical trio — but that tool was developed and tested on
  an **AMD** MSI Alpha 17 C7VG, not on this model.
* **MSI's own MUX documentation does not list a 10th-generation MSI.**
  ArchWiki's *MSI GS66 11UX* page records reverse-engineering this switch
  **failing** for that machine. A wrong value can leave you with **no display
  at all**.
* **If the panel does not come up after a reboot, recovery means the
  firmware** — clear CMOS / reset the BIOS to defaults, which restores the
  hybrid setting. This program cannot help you from a machine with no screen.

Given all of that it is deliberately awkward to reach, it backs the current
value up to `/var/lib/msi-control/mux-backup.json` (the raw efivarfs image,
byte for byte) before it writes anything, `msi-control graphics revert`
puts that back, and it is **never** applied automatically — not at boot, not
on resume, not from a profile. A graphics switch is a decision, not a setting.

#### How it knows what mode you are in

Three independent sources, and they agree on this machine:

| Source | Read from | Says |
|---|---|---|
| EC state bit | register `0x2e` bit 6, via the daemon | what the EC currently holds |
| UEFI variable | `MsiDCVarData` byte 1 (world-readable) | what the firmware will do at the **next POST** |
| PCI bus | is there an Intel `0x8086` display controller? | what the machine is **actually running right now** |

The third one is the honest one: the firmware powers the iGPU down in discrete
mode, so its absence is an observation rather than a claim. The panel shows
both — `running now` (PCI) and `at next boot` (UEFI variable) — so a switch
you have requested but not yet rebooted into is visible as exactly that,
instead of the UI pretending the change already happened.

Reading all three needs no root (the variable and the PCI bus are
world-readable), so `msi-control status` still reports the mode with the
daemon down. Only the EC register needs the daemon, and the offline view says
so rather than guessing.

---

## Safety model

1. **Validation.** Every client value passes `msi_control.check_*` on the
   *daemon* side — the GUI and CLI validate too, but the daemon never trusts
   them. Curves must be 6 strictly-increasing temperatures and 7
   non-decreasing speeds, all in range; charge limits are 20–100.
2. **Read-back.** Every EC write is followed by a read. An EC that silently
   ignores a byte is indistinguishable from one that accepted it, so the
   daemon refuses to report success unless the byte really stuck.
3. **Serialisation.** All EC writes take one lock — the kernel's ACPI code
   talks to the same controller.
4. **Least privilege.** Only `msi-controld` is root. The socket is `0660
   root:wheel`, and `wheel` already implies admin, so socket access grants
   nothing new. **Nothing over HTTP can reach the EC** — there is no web
   surface at all.
5. **No subprocesses in the daemon.** It reads and writes files at fixed
   sysfs paths, nothing else.

---

## Layout

```
src/msi_control.py     model registry, EC map, validation, protocol  (shared)
src/msi_controld.py           the privileged daemon
src/msi-control        CLI (status · detect · monitor · mode · fan · boost ·
                       battery · curve · profile · graphics · reapply · raw · wait)
src/msi-control-gui           PySide6 GUI
models/<id>/model.json one laptop model — the per-model EC map + features
models/README.md       the model-file format, field by field
tests/test_msi_control.py     138 offline tests — fake sysfs + RAM-backed EC
tests/gui_smoke.py     headless GUI construction + behaviour + screenshots
tests/shots/           committed screenshots — dark, and light/ beside it
tools/                 hardware probes (need root) + fixup/recapture helpers
third_party/msi-ec/    the vendored teackot driver v0.13
systemd/               service unit + resume hook
modprobe/              msi-ec.conf (debug=1)
desktop/ icons/        menu entry + SVG icon
etc/config.json        the default config template install.sh installs
install.sh uninstall.sh verify.sh
Makefile               make check / test / smoke / verify / install …
README.md CONTRIBUTING.md AGENTS.md CHANGELOG.md TODO.md APP_DETAILS.md
```

### Tests

```bash
python3 tests/test_msi_control.py                     # 138 tests, no root, no hardware
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py   # needs PySide6

make check                                     # both of the above + a syntax check
make shots                                     # regenerate tests/shots/
make shots-light                               # regenerate tests/shots/light/
```

The GUI smoke test renders one PNG per section into `tests/shots/` and then
asserts behaviour a screenshot cannot show: WCAG contrast ratios, the battery
tab's pending-vs-applied state machine, keyboard access to the curve editor,
the wording of every confirmation, the dashboard's numbers against their
labels, the battery chip's colour policy, and the graphics-card states. It
also checks the things two eyeballs cannot: that the light and dark token sets
define **the same keys**, that the accent and its ink clear 4.5:1, that the
sidebar and the page stack are the same list, that every nav glyph draws
something, that **every button, field and sidebar row is the same height**
(one 10 px-taller button is invisible as a bug — it just looks emphasised),
and that the icon it ships and the mark it paints are the same silhouette,
with nothing behind the shield. `make shots` and `make shots-light` regenerate
the two checked-in sets.

The suite builds a **fake sysfs tree** and a **RAM-backed EC**, then runs the
real daemon in-process and drives it over the real socket, so protocol,
validation, profiles, restore-after-reboot and the CLI are all exercised
end-to-end. The environment variables `MSI_SYSFS`, `MSI_SOCK`,
`MSI_CONFIG` and `MSI_STATE` redirect every path, which is what makes this
possible without root.

It deliberately covers the ugly cases: an EC byte that refuses to be written
(read-back must catch it), a `True` that must not become fan speed 1, a
hostile profile name trying to escape its directory, the daemon socket being
cleaned up on SIGTERM, and the CLI still answering (in degraded mode) when the
daemon is down.

The graphics-mode tests are as much about what the switch **refuses** to do as
about the bytes it writes: it must not write while `AllowMuxSwitch` is off,
must not write without saving a backup, must not touch the neighbouring bits
of `0x2e` (bit 1 is the webcam), must not report a switch it did not read
back, and must not leave the UEFI variable mutable if the write fails.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `msi-control status` says the daemon is down | `systemctl status msi-controld`; check `journalctl -u msi-controld` |
| The window shows an amber "no model" banner | this laptop has no model file yet — run `msi-control detect`; fan curves, RPM and the graphics switch are off until you add one (see `CONTRIBUTING.md`) |
| `msi-control detect` reports `matched: NOTHING` | as above — temperatures, shift/fan modes, Cooler Boost and the charge limit still work; a draft is `msi-control detect --write DIR` |
| Status shows no RPM, no curves | the driver's `debug` interface is off — check `cat /sys/module/msi_ec/parameters/debug`; it needs `debug=1` **at load time** (`modprobe -r msi-ec && modprobe msi-ec`) |
| `permission denied` on the socket | you are not in `wheel` (or `SocketGroup` was changed) |
| Driver not present at all | `modprobe msi-ec`; if that fails, `dmesg \| tail -20` — most likely Secure Boot, or headers for a *different* kernel than the one running |
| DKMS build failed | `dkms build msi-ec/0.13 -k $(uname -r)` for the real error; the installer falls back to a direct build automatically |
| Settings reset after suspend | expected if the firmware resets the EC; the resume hook re-applies them — check `ReapplyOnResume` is still `true` |
| GUI does not start | `python3 -c 'import PySide6'` — install `python3-pyside6`. The CLI works regardless |
| The graphics switch is greyed out | it is off by default — set `"AllowMuxSwitch": true` in the config and restart `msi-controld` |
| Switched the graphics mode but nothing changed | that is by design: it applies at the **next reboot**. The card says `reboot required` until it does |
| No display after a graphics switch | reset the firmware (clear CMOS / BIOS defaults) from an external screen, then `msi-control graphics revert` |
