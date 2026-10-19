# Contributing to MSI Linux Control

Thanks for looking. This is a small, opinionated project that controls real
hardware, so the bar is *"prove it"*, not *"it compiles"*. Everything below
exists because something here has already bitten someone once.

Before you start, read [`APP_DETAILS.md`](APP_DETAILS.md); it is the single
map of the system: the model registry, the EC register map, the socket
protocol, the daemon's design rules, and the hardware gotchas that are already
paid for. If you are an AI agent (or working with one), [`AGENTS.md`](AGENTS.md)
is the same map written for that workflow. This file is the *how to work here*.

The single most common contribution is **adding a model for a laptop that is
not the dev machine**; if that is why you are here, skip to
[Adding your model](#adding-your-model).

---

## The one rule that matters

**The daemon owns every EC write, and every write is read back.**

Nothing in this project may write to the embedded controller except
`msi_controld.py`, through the validated path. If you are tempted to poke a byte
from the GUI, the CLI, a script, or a test, stop; that is the one way this
program can hard-crash the machine. New registers, if any, go in a model file
(see below) and are written through the daemon like everything else.

Read-modify-write **one bit at a time** for any register you share with another
function. `0x2e` is a live example: bit 6 is the MUX switch and bit 1 is the
webcam, and a whole-byte poke would drag the webcam along with it.

---

## Setting up

No build step. The programs are Python and are run from the checkout:

```bash
python3 tests/test_msi_control.py                              # daemon + CLI, offline
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py    # GUI, headless
```

or via the Makefile, which just names those:

```bash
make check        # compile + tests + GUI smoke
make test         # the offline suite only
make smoke        # GUI build + one screenshot per tab
make shots-light  # the same, light theme
make verify       # live hardware checks: root, safe and reversible
```

`tests/test_msi_control.py` needs **no root and no hardware**: it builds a fake
sysfs tree and a RAM-backed EC and runs the *real* daemon over the *real*
socket. `gui_smoke.py` needs PySide6 and a Qt platform plugin (`offscreen` is
enough).

---

## Adding your model

This program knows about one laptop by default (an MSI GS66 Stealth 10SE).
Making it work fully on **your** MSI laptop means adding a model file under
`models/`. Everything the `msi-ec` driver exposes (temperatures, shift and fan
modes, Cooler Boost, the charge limit) already works without one; a model file
adds the fan curves, RPM and the graphics switch, which need per-model EC
registers the driver does not know.

A model file is small and it is honest: it says which features you *proved*
and which you did not. A half model is a real improvement over none, so work
through the steps below to whatever depth you can, then open the pull request
anyway. The full field-by-field reference is [`models/README.md`](models/README.md);
`models/gs66-stealth-10se/` is the worked example.

### Step 0: read your laptop's identity

```bash
msi-control detect              # what laptop am I, and is it supported?
msi-control detect --json       # the same, machine readable
msi-control detect --ec-dump    # + the 256-register map (needs msi-ec debug=1)
```

`detect` needs no root and no daemon. Note what it prints: the DMI strings
(`product_name`, `board_name`), the EC firmware revision (a glob like
`16V1EMS1.*`), which driver attributes exist on your machine, and whether any
registry entry matches. If the GS66's firmware family matches yours, add a
comment on the matching model rather than a new file.

### Step 1: draft the file

```bash
msi-control detect --write /tmp       # writes /tmp/<id>/model.json
```

The draft is valid and modest: the `match` block filled in from your
hardware, the `ec` block empty, `fan_curve`/`rpm`/`mux` all `false`, and the
everyday profiles in place. Ship this draft even at commit-time if you
stopped here: it makes your laptop's temperatures, modes and charge limit
usable and prevents nobody from guessing anything.

### Step 2: find the missing registers

Fan curves and RPM need this model's addresses. Change **one** thing at a
time on the machine and diff `msi-control detect --ec-dump` before and after:

| What you touch | Which byte(s) move |
|---|---|
| one point of the CPU fan curve | one of the CPU `temps` or `speeds` registers |
| one point of the GPU fan curve | one of the GPU `temps` or `speeds` registers |
| a fan speed in the moment | as above (the live `speeds` block) |
| load (holding a temperature) | that temperature's own register |
| nothing (read back twice) | the tachometer bytes, `rpm.cpu` / `rpm.gpu` |

Two things the GS66 README records and yours should too: the
tachometer is `RPM = numerator // period`, with the `period` byte read at the
`rpm` address, and the `numerator` was measured against a fan whose real
speed is known (find it in your vendor's spec sheet or a full-speed run).
Also: every `speeds` block's first byte is the speed **below** the first
threshold, so it owns no curve point.

Other projects carry maps for many MSI models: YoyPa/isw (its `[Firmware]`
sections are real dumps), the `msi-ec` driver's in-tree `CONF_*` tables, and
forks of this program. **Check the licence before copying anything**: a map
whose licence forbids redistribution cannot be brought into a GPL project no
matter how good the addresses are. Record whatever you use in the model's
`provenance` field, with a link.

### Step 3: prove each feature flag, honestly

A feature flag is a claim that a write read back true. For each one you want
`true`:

* `fan_curve`: set a point through this program, dump, confirm the register
  moved, set it back and confirm it moved back;
* `rpm`: read some RPM values during at least three different fan speeds and
  the numerator reproduces them;
* `mux`: **read the [warning](README.md#graphics-mode-the-mux-switch) first**;
  the GS66's method (UEFI variable + EC bit + PCI check) must be confirmed on
  your machine, and this is the one flag where a wrong value sends you to
  BIOS recovery. If you cannot prove it, leave it `false` and note why.

Anything you cannot prove stays `false`; the unsupported state is designed
for exactly that.

### Step 4: write the model's README

Copy the shape of `models/gs66-stealth-10se/README.md`: which addresses you
found, how (a diff, a reference project, a measurement), what values you read
back, anything non-obvious about your firmware. The next person with your
laptop needs the *provenance*, not just the numbers.

### Step 5: test it

```bash
make check                             # the whole stack, with your model present
MSI_MODEL=<id> msi-control status      # does it load and match?
sudo ./verify.sh                       # on your machine, root, reversible
```

`verify.sh` walks every mode, the charge limit and a fan-curve write-back
round-trip, and restores what it changes. Run it on a machine you can afford
to have briefly mis-set. Paste its output into the PR: it is the
write-back test a `fan_curve: true` model needs.

### Step 6: open the pull request

One model per PR, one branch:

```bash
git checkout -b add-model-<your-id>
git add models/<your-id>/
git commit --file=- <<'MSG'
Add model <your-id>

<one sentence: which laptop, which firmware, provenance summary>
MSG
git push -u origin add-model-<your-id>
```

A complete PR has:

* the model directory: `model.json` **and** a README of how it was made;
* `make check` passing with the model present in the tree;
* the `msi-control detect` output in the description (edit out your
  serial number and hostname first);
* the `sudo ./verify.sh` output if any register-backed flag is `true`;
* a short statement of which feature flags you turned on and how each was
  proved (a sentence per flag is fine);
* `status` set honestly: `draft` while you are still confirming, `testing`
  once your own machine runs it, `stable` only after the write-back tests
  passed on your hardware.

A reviewer will reject, kindly but briskly: a `fan_curve: true` in a PR that
mentions no write-back test; a register list with no provenance; a flag
turned on "because the values were already sitting in a dump"; or a second
model squeezed into a PR that already has one. A PR that says "here is my
draft, curves are still `false`, I could not find the tachometer" is accepted
with thanks.

If you are working with an AI agent, [`AGENTS.md`](AGENTS.md) carries the
same procedure written for that workflow, including what it may and may not
touch (it will not write to your EC outside `verify.sh`).

---

## Before you open a change

1. **Back up what you are about to touch.** Copy the file you are editing
   somewhere outside the tree first (a `~/backup-pre-*/` directory is the
   convention this project has used). A changelog entry can describe a change;
   only a copy can undo one.
2. **Update the docs in the same change.** `README.md` is the user-facing
   write-up, `APP_DETAILS.md` is the maintainer map, `TODO.md` is the live
   list, and `CHANGELOG.md` gets the row. A change that leaves the docs
   describing the old behaviour is a bug in the docs.
3. **Report the toolkit.** The GUI is **PySide6** (the user asked for PyQt6;
   PySide6 was substituted because it was already installed). Note that if your
   change touches the toolkit.

---

## Tests

`tests/test_msi_control.py` is not a formality; it is where the ugly cases live,
and they are all there because one of them shipped:

* an EC byte that **refuses to be written** (read-back must catch it);
* a Python `True` that must **not** become fan speed 1 (`isinstance(True, int)`
  is `True`);
* a **hostile profile name** trying to escape its directory;
* the daemon **cleaning up its socket on SIGTERM**;
* the **CLI still answering** (degraded) when the daemon is down;
* the MUX switch **refusing** to write while disabled, refusing to write
  without a backup, and never touching the neighbouring webcam bit;
* a model file with a **bad register** or a feature flag it cannot back with a
  register (validation must name the file and the field, not write a guess);
* a laptop with **no matching model** (every register-backed control must be
  refused, and the sysfs ones must keep working).

Add a test for anything you add. A behaviour that only a human can check will
be broken again within a month.

`tests/gui_smoke.py` is the other half; it asserts what a screenshot cannot
show: **WCAG contrast ratios**, the battery tab's pending-vs-applied state
machine, keyboard access to the curve editor, the exact wording of every
confirmation, and the dashboard's numbers against their labels. If you change a
label, a colour, or a state machine, extend it here.

### Scripted edits

Both the test suite and the smoke test take the same view: a test must
**never touch the user's real config**. `gui_smoke.py` redirects
`XDG_CONFIG_HOME` into the shots directory before anything constructs a
`QSettings`, and drops any stale file first. Do not remove that; without it a
test run rewrites `~/.config/msi-control/msi-control.conf` and opens the
developer's next real window on whatever tab the test ended on.

---

## Style

* **stdlib-only for the daemon and the CLI.** They have to work in a recovery
  shell with nothing installed. PySide6 is a GUI-only dependency.
* **No subprocesses in the daemon.** It reads and writes files at fixed sysfs
  paths through Python, and nothing else.
* Comments explain *why*, not *what*: the reason a value was chosen, the bug a
  guard prevents, the measurement behind a constant. `# increment i` is noise;
  `# speeds[0] is the speed below the first threshold, so it owns no point` is
  the kind of thing that saves the next reader an hour.
* Keep the runbook voice: plain sentences, real numbers, and the measurement
  written next to the conclusion so the next person can re-check it.
* **Plain prose, never typographic dashes.** The project never uses em-dashes,
  en-dashes or a Unicode minus sign in source, comments, docs or UI strings;
  where a dash would go, reach for a comma, a colon, parentheses or a separate
  sentence instead. The tree was written that way on 2026-10-18, and
  `third_party/msi-ec/` is upstream's text, left alone. It is the one style
  rule a reviewer will send a change back for, so it is worth a moment before
  you commit.
* Paths and constants are imported from `msi_control.py`, never re-typed. If
  the CLI and the GUI disagree about what "warm" means, they are both reading
  the same helper; that is the point.

---

## Commits & the changelog

There is no enforced commit format. What *is* enforced is `CHANGELOG.md`: every
upgrade, fix, or config change gets a row, in the same session, before the work
is called done. The row says what changed and, where it matters, **why** -
that file is the archaeology the next contributor reads. Old rows keep the old
names (`GS66 Center`, `gs66-centerd`); they are history, not stale references.

---

## Licence & provenance

The vendored driver in `third_party/msi-ec/` is
[**teackot/msi-ec**](https://github.com/teackot/msi-ec) **v0.13**, GPL-2.0, kept
in-tree (not a submodule) so the DKMS build needs no network. Keep its licence
and headers intact, and **do not rename anything inside it**; its device
strings and file names are upstream's, not ours. The MUX mechanism is
cross-checked against
[`ElXreno/msi-gpu-switcher`](https://github.com/ElXreno/msi-gpu-switcher).
