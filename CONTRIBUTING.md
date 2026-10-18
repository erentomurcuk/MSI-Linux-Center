# Contributing to GS66 Center

Thanks for looking. This is a small, opinionated project that controls real
hardware, so the bar is *"prove it"*, not *"it compiles"*. Everything below
exists because something here has already bitten someone once.

Before you start, read [`APP_DETAILS.md`](APP_DETAILS.md) — it is the single
map of the system: the EC register map, the socket protocol, the daemon's
design rules, and the hardware gotchas that are already paid for. This file is
only the *how to work here*; that one is the *what is here*.

---

## The one rule that matters

**The daemon owns every EC write, and every write is read back.**

Nothing in this project may write to the embedded controller except
`gs66d.py`, through the validated path. If you are tempted to poke a byte from
the GUI, the CLI, a script, or a test, stop — that is the one way this program
can hard-crash the machine. New registers, if any, go in `gs66_common.py`'s
map and are written through the daemon like everything else.

Read-modify-write **one bit at a time** for any register you share with another
function. `0x2e` is a live example: bit 6 is the MUX switch and bit 1 is the
webcam, and a whole-byte poke would drag the webcam along with it.

---

## Setting up

No build step. The three programs are Python and are run from the checkout:

```bash
python3 tests/test_gs66.py                              # daemon + CLI, offline
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py    # GUI, headless
```

or via the Makefile, which just names those:

```bash
make check        # compile + tests + GUI smoke
make test         # the offline suite only
make smoke        # GUI build + one screenshot per tab
make verify       # live hardware checks — root, safe and reversible
```

`tests/test_gs66.py` needs **no root and no hardware**: it builds a fake sysfs
tree and a RAM-backed EC and runs the *real* daemon over the *real* socket.
`gui_smoke.py` needs PySide6 and a Qt platform plugin (`offscreen` is enough).

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

`tests/test_gs66.py` is not a formality — it is where the ugly cases live, and
they are all there because one of them shipped:

* an EC byte that **refuses to be written** (read-back must catch it);
* a Python `True` that must **not** become fan speed 1 (`isinstance(True, int)`
  is `True`);
* a **hostile profile name** trying to escape its directory;
* the daemon **cleaning up its socket on SIGTERM**;
* the **CLI still answering** (degraded) when the daemon is down;
* the MUX switch **refusing** to write while disabled, refusing to write
  without a backup, and never touching the neighbouring webcam bit.

Add a test for anything you add. A behaviour that only a human can check will
be broken again within a month.

`tests/gui_smoke.py` is the other half: it asserts what a screenshot cannot
show — **WCAG contrast ratios**, the battery tab's pending-vs-applied state
machine, keyboard access to the curve editor, the exact wording of every
confirmation, and the dashboard's numbers against their labels. If you change a
label, a colour, or a state machine, extend it here.

### Scripted edits

Both the test suite and the smoke test take the same view: a test must
**never touch the user's real config**. `gui_smoke.py` redirects
`XDG_CONFIG_HOME` into the shots directory before anything constructs a
`QSettings`, and drops any stale file first. Do not remove that — without it a
test run rewrites `~/.config/gs66-center/gs66-center.conf` and opens the
developer's next real window on whatever tab the test ended on.

---

## Style

* **stdlib-only for the daemon and the CLI.** They have to work in a recovery
  shell with nothing installed. PySide6 is a GUI-only dependency.
* **No subprocesses in the daemon.** It reads and writes files at fixed sysfs
  paths through Python, and nothing else.
* Comments explain *why*, not *what* — the reason a value was chosen, the bug a
  guard prevents, the measurement behind a constant. `# increment i` is noise;
  `# speeds[0] is the speed below the first threshold, so it owns no point` is
  the kind of thing that saves the next reader an hour.
* Keep the runbook voice: plain sentences, real numbers, and the measurement
  written next to the conclusion so the next person can re-check it.
* Paths and constants are imported from `gs66_common.py`, never re-typed. If
  the CLI and the GUI disagree about what "warm" means, they are both reading
  the same helper — that is the point.

---

## Commits & the changelog

There is no enforced commit format. What *is* enforced is `CHANGELOG.md`: every
upgrade, fix, or config change gets a row, in the same session, before the work
is called done. The row says what changed and, where it matters, **why** —
that file is the archaeology the next contributor reads.

---

## Licence & provenance

The vendored driver in `third_party/msi-ec/` is
[**teackot/msi-ec**](https://github.com/teackot/msi-ec) **v0.13**, GPL-2.0, kept
in-tree (not a submodule) so the DKMS build needs no network. Keep its licence
and headers intact. The MUX mechanism is cross-checked against
[`ElXreno/msi-gpu-switcher`](https://github.com/ElXreno/msi-gpu-switcher).
