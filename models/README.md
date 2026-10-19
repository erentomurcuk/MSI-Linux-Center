# The model registry

One directory per laptop model, each holding a `model.json`. This is what makes
MSI Linux Control about MSI laptops in general instead of about the one machine
it was written for. If your laptop is not fully supported, **this is the
directory you add to**; see [`../CONTRIBUTING.md`](../CONTRIBUTING.md) §
"Adding your model" and [`../AGENTS.md`](../AGENTS.md) §6.

```
models/
├── README.md                 this file: the format, field by field
└── gs66-stealth-10se/
    ├── model.json            the worked example
    └── README.md             how those registers were found and confirmed
```

The loader reads every `models/*/model.json` at import, validates it, and scores
each one against this machine's DMI identity and EC firmware. The best score
wins; matching nothing is a working **unsupported** state (see below), not a
crash.

---

## Where the directory is

Search order, first hit wins:

1. `$MSI_MODELS` if set, which is how the tests and a contributor trying a
   model out point somewhere else;
2. `<dir of msi_control.py>/models` (source tree: `src/../models`);
3. `/usr/local/lib/msi-control/models` (the installed location);
4. `/usr/lib/msi-control/models`.

`msi-control detect` prints the directory actually in use.

---

## The file, field by field

```jsonc
{
  "schema": 1,
  "id": "gs66-stealth-10se",
  "name": "MSI GS66 Stealth 10SE",
  "vendor": "Micro-Star International Co., Ltd.",
  "status": "stable",
  "added": "2026-10-18",

  "match": { … },
  "ec": { … },
  "charge": { "min": 20, "max": 100, "step": 10 },
  "features": { … },
  "profiles": { … },

  "provenance": ["…"],
  "notes": "…"
}
```

### `schema` (required)

Must be `1`. A different number is refused with a message saying which schemas
this build understands, so an old program reading a new file cannot mis-read it.

### `id` (required)

Lower-case letters, digits and hyphens (`^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`),
and it **must equal the directory name**. It is a lookup key and a path
component. Generate one from the product name with
`msi-control detect --write` (it slugs `"GS66 Stealth 10SE"` →
`gs66-stealth-10se`).

### `name`, `vendor`, `status`, `added`, `notes`, `provenance`

Informational. `name` is what the UI shows (defaults to the `id`); `vendor`
defaults to `Micro-Star International Co., Ltd.`; `status` is `draft`,
`testing` or `stable` (default `stable`).

`provenance` is a list of strings and it matters more than it looks: say where
each address came from: a project and section (YoyPa/isw `[16V1EMS1]`), a
driver table (`teackot/msi-ec CONF_G1_3`), a live dump, or your own measurement.
`notes` is for the one or two non-obvious facts about *this* firmware.

### `match` (required)

How this machine is recognised. A dict of three optional keys, each a string or
a list of strings, matched as **case-insensitive, anchored globs** against the
machine's world-readable identity:

| Key | Read from | Weight |
|---|---|---|
| `product_name` | `/sys/class/dmi/id/product_name` | 50 |
| `board_name` | `/sys/class/dmi/id/board_name` | 40 |
| `ec_firmware` | `/sys/devices/platform/msi-ec/fw_version` | 30 |

The scores are added; the highest total wins. At least one key must be present.
The product name is the strongest signal; the EC firmware is weaker because a
firmware family is shared across models, and it is what saves a machine whose
DMI string MSI changed between BIOS revisions.

**Pin the firmware with a wildcard, never a full revision.** `16V1EMS1.118`
becomes `16V1EMS1.*`; the trailing revision arrives with every firmware
update, so a pattern that pinned it would stop matching for everyone who
updated. Use the same wildcard for a DMI string only if the vendor really does
vary it.

### `ec` (optional, but it is the point)

The registers the `msi-ec` driver does not know. Two sub-blocks:

```jsonc
"ec": {
  "curve": {
    "cpu": { "temps": ["0x6a", …, "0x6f"], "speeds": ["0x72", …, "0x78"] },
    "gpu": { "temps": ["0x82", …, "0x87"], "speeds": ["0x8a", …, "0x90"] }
  },
  "rpm": { "numerator": 478000, "cpu": "0xcc", "gpu": "0xca" }
}
```

* Addresses are hex strings (`"0x6a"`) or decimal integers. Hex is the
  convention: it is how the EC documentation and the driver's `ec_get` speak.
* A fan curve is a **step function**: the fan runs at `speeds[0]` until the
  temperature passes `temps[0]`, then `speeds[1]`, and so on. So there is always
  **one more speed than threshold** (the GS66 is 6 thresholds / 7 speeds). The
  loader enforces `len(speeds) == len(temps) + 1`, and that both fans have the
  same shape. A model may use a different shape than the GS66's, as long as it
  is internally consistent.
* `rpm.numerator` is the tachometer constant: `RPM = numerator // period`,
  where the two bytes at `rpm.cpu` / `rpm.gpu` are a **period count**, not a
  speed. A wrong constant prints plausible-looking nonsense. Reproduce it from
  a source you trust and record that source in `provenance`.

Omit a block you have not identified. The `features` cross-check (below) then
keeps the matching flag off for you.

### `charge`

The charge-threshold bounds *this firmware* accepts:

```json
"charge": { "min": 20, "max": 100, "step": 10 }
```

`min` must be strictly below `max`; both are 1-100. The 20-100 / step-10
convention comes from the driver's own semantics (the start and end thresholds
are one EC byte, always 10 % apart) but a firmware that accepts a different
range can say so.

### `features`

What this firmware can actually do. All twelve keys are optional; anything
omitted falls back to the unsupported defaults (driver features `true`, the
register-backed ones `false`):

| Flag | Kind | Meaning |
|---|---|---|
| `shift` | driver | performance (shift) modes |
| `fan` | driver | fan modes |
| `cooler_boost` | driver | Cooler Boost |
| `charge_limit` | driver | battery charge threshold |
| `fan_curve` | register | custom fan curves: **requires `ec.curve`** |
| `rpm` | register | fan tachometers: **requires `ec.rpm`** |
| `mux` | register | graphics-mode (MUX) switch |
| `webcam`, `webcam_block`, `super_battery`, `fn_key`, `win_key` | driver | optional features only some firmwares have |

The first four are also checked at runtime against the attributes the driver
actually exposes, so a model cannot enable a control the driver does not offer.
`fan_curve: true` with no `ec.curve`, or `rpm: true` with no `ec.rpm`, is a
**load error**; a model may not claim a feature it has no registers for.

Only turn a flag on for something you have proven. `fan_curve` and `mux` in
particular write registers, and a guessed address is a machine that will not
boot. Note that **`mux` is a plain enable here**: the MUX addresses themselves
are not per-model; the whole MSI family shares one block in
`src/msi_control.py`, and a model only says whether its firmware supports it.

### `profiles`

This model's built-in profiles: the everyday bundles. They are model-specific
because a curve is. A profile is a partial setting bundle:

```jsonc
"profiles": {
  "silent":   {"shift": "eco",     "fan": "silent",   "cooler_boost": false},
  "balanced": {"shift": "comfort", "fan": "auto",     "cooler_boost": false},
  "gaming":   {"shift": "sport",   "fan": "advanced", "cooler_boost": false,
               "curve": {"cpu": {"temps": [58,63,68,73,78,83],
                                 "speeds": [0,50,60,65,70,80,85]},
                         "gpu": {"temps": [53,58,63,68,73,83],
                                 "speeds": [0,55,65,70,80,85,90]}}},
  "battery":  {"shift": "eco", "fan": "auto", "cooler_boost": false,
               "charge_end": 60}
}
```

* Recognised keys: `shift`, `fan`, `cooler_boost`, `curve` (cpu/gpu temps +
  speeds in real units, not addresses), and `charge_end`.
* A profile that does **not** mention a charge limit does **not** touch one.
  Only `battery` sets a limit on the GS66, on purpose: applying a performance
  profile must never silently undo a charge limit you chose for battery health.
* No profile may be named `factory`; that name is reserved for the snapshot
  captured from the live EC on first run, which is the guaranteed way back.
* Every profile is validated by the same `clean_profile` the daemon uses, so a
  bad curve in a model file fails at load with the file and the profile named.

---

## What "unsupported" means

A machine that matches no model loads the built-in `unknown` model, which
enables everything the `msi-ec` driver exposes by itself:

* **works**: temperatures, fan duty, shift modes, fan modes, Cooler Boost, the
  charge limit;
* **refused**: fan curves, RPM, and the graphics switch; these need registers
  only a model file can supply, and the program refuses rather than guesses.

The GUI shows an amber banner, the Fan tab becomes read-only with a notice, and
the Settings tab points at `msi-control detect`. Everything on the dashboard
except RPM keeps working. A half-written model is a real improvement; a guessed
register is not.

---

## Trying a model without installing it

```bash
MSI_MODELS=$PWD/models MSI_MODEL=my-model msi-control status
```

`MSI_MODELS` overrides the registry directory; `MSI_MODEL` forces one id. An id
that is not in the registry is a hard error, not a silent fallback; a typo must
not look like "unsupported".
