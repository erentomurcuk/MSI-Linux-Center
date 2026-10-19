# MSI GS66 Stealth 10SE: the worked example

This is the model the program was written for, and the reference for what a
complete, tested `model.json` looks like. Read [`../README.md`](../README.md)
for the format itself; this file is the **provenance**: where each address came
from and how it was confirmed. The next person with this laptop needs to be
able to re-check it, not just trust it.

| Fact | Value |
|---|---|
| Product name | `GS66 Stealth 10SE` |
| Board name | `MS-16V1` |
| Vendor | `Micro-Star International Co., Ltd.` |
| EC firmware | `16V1EMS1.118` (matched as `16V1EMS1.*`) |
| BIOS | `E16V1IMS.112` |
| Match score | 120 = 50 (product) + 40 (board) + 30 (EC firmware) |

## Where the registers came from

Three independent sources, and they agree:

* **[YoyPa/isw](https://github.com/YoyPa/isw)**: its `[16V1EMS1]` section plus
  its `MSI_ADDRESS_DEFAULT` profile, which carries a **real EC dump from a
  GS66-10SE** (isw issue #220). This is the primary source for the curve,
  tachometer and charge addresses.
* **[teackot/msi-ec](https://github.com/teackot/msi-ec)** `CONF_G1_3`, which
  lists the firmware explicitly: `16V1EMS1.118 // GS66 Stealth 10SE`. This is
  the vendored driver in `third_party/msi-ec/`.
* **The in-tree** `drivers/platform/x86/msi-ec.c` charge semantics: the start
  and end thresholds are one EC byte, always 10 % apart, range 20-100.

## The map

| Purpose | Address | Notes |
|---|---|---|
| CPU curve: thresholds | `0x6a`-`0x6f` | 6 thresholds, °C |
| CPU curve: speeds | `0x72`-`0x78` | 7 speeds, % |
| GPU curve: thresholds | `0x82`-`0x87` | 6 thresholds, °C |
| GPU curve: speeds | `0x8a`-`0x90` | 7 speeds, % |
| CPU tachometer | `0xcc` | 16-bit big-endian **period**; `RPM = 478000 // period` |
| GPU tachometer | `0xca` | same |

Shift mode (`0xf2`), fan mode (`0xf4`), Cooler Boost (`0x98` bit 7) and the
charge threshold (`0xef`) are **not** in this file: the `msi-ec` driver exposes
them through sysfs, so they are model-independent (see `../README.md` §
`features`).

The graphics-mode (MUX) addresses are also not here. `0x2e` bit 6, the trigger
at `0xd1`, and the UEFI variable `MsiDCVarData` are shared across the MSI
family, so they live once in `src/msi_control.py`; this model only sets
`"mux": true`.

## What was confirmed on hardware, not just copied

* ✅ **The tachometer constant.** Cooler Boost drives `0xcc` from ~3700 to
  **~8100**, which is the top of this laptop's fan range, so `478000 // period`
  is right and the scale is real (measured 2026-10-16).
* ✅ **Cooler Boost does not update the duty registers.** With boost on,
  `0x71`/`0x89` stayed at `70` while the tachometers read ~8100 RPM. So the
  reported `fan_pct` is the curve's *request*, not what the fan is doing, which
  is why the dashboard shows RPM beside it. This is documented here so nobody
  "fixes" the duty read to match the tacho.
* ❌ **`0xc8` is not a third fan.** It was suspected to be one
  (LibreHardwareMonitor reports three on this model). It is not: under Cooler
  Boost `0xca`/`0xcc` jump to ~8100 while `0xc8` sits at ~1900. `CONF_G1_3`
  maps exactly two fans. No third-fan reading is exposed; an unidentified
  register painted as a fan is a guess wearing a fact's clothes.

## Feature flags, and why each is what it is

| Flag | Value | Why |
|---|---|---|
| `shift`, `fan`, `cooler_boost`, `charge_limit` | `true` | driver-provided; exposed on this firmware |
| `fan_curve`, `rpm` | `true` | the registers above; curve write-back verified by `verify.sh` |
| `mux` | `true` | the shared MUX block; the switch itself is still opt-in (`AllowMuxSwitch`) |
| `webcam`, `webcam_block`, `super_battery`, `fn_key`, `win_key` | `false` | not present on this config; they read back as absent, so the GUI disables them instead of pretending |

## The built-in profiles

`silent` / `balanced` / `gaming` / `battery` are defined in `model.json`. The
`gaming` curve is this laptop's starting shape, not a claim about yours; a
curve is per-model, which is exactly why profiles live in the model file.
`factory` is **not** here: it is captured from the live EC on first run, so it
is always the machine's own shipping state rather than a hard-coded guess.
