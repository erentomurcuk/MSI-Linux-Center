#!/usr/bin/env python3
"""Offline test suite for msi-control.

Runs entirely unprivileged against a fake sysfs tree and a RAM-backed EC,
so it exercises the real daemon (Service + Unix socket server + protocol),
the real validation code and the real CLI — without touching hardware.

    python3 tests/test_msi_control.py
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

# The registry is model-driven now, and "which model is this machine" is
# decided at *import* of msi_control.  A test run must not depend on the
# machine it happens to be on, so pin the model and the registry directory
# before the import — exactly the escape hatch MSI_MODEL/MSI_MODELS exist for.
REPO = os.path.dirname(HERE)
os.environ["MSI_MODELS"] = os.path.join(REPO, "models")
os.environ["MSI_MODEL"] = "gs66-stealth-10se"

import msi_control as core                      # noqa: E402
import msi_controld                                    # noqa: E402
from msi_controld import MsiEc, Service, Server, Handler  # noqa: E402
from msi_control import ValidationError         # noqa: E402

# --------------------------------------------------------------------------
# fake hardware
# --------------------------------------------------------------------------

# A plausible MSI EC image: isw's documented MSI fan curve.
FAKE_EC_INIT = {}
for i, v in enumerate([58, 63, 68, 73, 78, 83]):            # cpu temps 0x6a
    FAKE_EC_INIT[0x6A + i] = v
for i, v in enumerate([0, 50, 60, 65, 70, 80, 85]):         # cpu speeds 0x72
    FAKE_EC_INIT[0x72 + i] = v
for i, v in enumerate([53, 58, 63, 68, 73, 83]):            # gpu temps 0x82
    FAKE_EC_INIT[0x82 + i] = v
for i, v in enumerate([0, 55, 65, 70, 80, 85, 90]):         # gpu speeds 0x8a
    FAKE_EC_INIT[0x8A + i] = v
# tachometer periods: cpu 0xcc/0xcd = 0x012c (300), gpu 0xca/0xcb = 0x00fa (250)
FAKE_EC_INIT.update({0xCC: 0x01, 0xCD: 0x2C, 0xCA: 0x00, 0xCB: 0xFA})
# EC graphics state bit: 0x40 set = discrete, matching the real MSI 10SE.
FAKE_EC_INIT.update({core.MUX_EC_ADDR: core.MUX_EC_MASK, core.MUX_TRIGGER_ADDR: 0x00})


def write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def build_fake_sysfs(root: str):
    ec = os.path.join(root, "devices", "platform", "msi-ec")
    write(os.path.join(ec, "fw_version"), "16V1EMS1.118\n")
    write(os.path.join(ec, "fw_release_date"), "2022/08/20 13:53:21\n")
    write(os.path.join(ec, "available_shift_modes"),
          "eco\ncomfort\nsport\nturbo\n")
    write(os.path.join(ec, "shift_mode"), "comfort\n")
    write(os.path.join(ec, "available_fan_modes"), "auto\nsilent\nadvanced\n")
    write(os.path.join(ec, "fan_mode"), "auto\n")
    write(os.path.join(ec, "cooler_boost"), "off\n")
    write(os.path.join(ec, "webcam"), "off\n")
    write(os.path.join(ec, "webcam_block"), "off\n")
    write(os.path.join(ec, "fn_key"), "left\n")
    write(os.path.join(ec, "win_key"), "right\n")
    write(os.path.join(ec, "super_battery"), "off\n")
    write(os.path.join(ec, "cpu", "realtime_temperature"), "52\n")
    write(os.path.join(ec, "cpu", "realtime_fan_speed"), "45\n")
    write(os.path.join(ec, "gpu", "realtime_temperature"), "48\n")
    write(os.path.join(ec, "gpu", "realtime_fan_speed"), "40\n")
    os.makedirs(os.path.join(ec, "debug"), exist_ok=True)   # xfers are faked

    bat = os.path.join(root, "class", "power_supply", "BAT1")
    write(os.path.join(bat, "present"), "1\n")
    write(os.path.join(bat, "status"), "Discharging\n")
    write(os.path.join(bat, "capacity"), "67\n")
    write(os.path.join(bat, "energy_now"), "40000000\n")
    write(os.path.join(bat, "energy_full"), "60000000\n")
    write(os.path.join(bat, "charge_control_start_threshold"), "50\n")
    write(os.path.join(bat, "charge_control_end_threshold"), "60\n")
    write(os.path.join(root, "class", "power_supply", "ADP1", "online"), "0\n")

    # efivarfs + PCI: the two world-readable sources of the graphics mode.
    # One NVIDIA display controller and no Intel one, which is what the real
    # MSI 10SE looks like in discrete mode; tests that need the other answer
    # add an Intel device themselves.
    efivars = os.path.join(root, "firmware", "efi", "efivars")
    os.makedirs(efivars, exist_ok=True)
    with open(os.path.join(efivars, core.MUX_VAR_BASENAME), "wb") as fh:
        # 4 attribute bytes, then the payload: byte 0 = 0x01, byte 1 = 0x01
        # (mode = discrete), then padding, as the real variable reads.
        fh.write(b"\x07\x00\x00\x00" + bytes([0x01, 0x01]) + bytes(20))
    write(os.path.join(root, "bus", "pci", "devices", "0000:01:00.0", "vendor"),
          "0x10de\n")
    write(os.path.join(root, "bus", "pci", "devices", "0000:01:00.0", "class"),
          "0x030000\n")


def add_fake_igpu(root: str):
    """Pretend the firmware brought the Intel iGPU up (hybrid mode)."""
    write(os.path.join(root, "bus", "pci", "devices", "0000:00:02.0", "vendor"),
          "0x8086\n")
    write(os.path.join(root, "bus", "pci", "devices", "0000:00:02.0", "class"),
          "0x030000\n")


def no_ec_access(addr):
    """What ec_read does when the msi-ec debug interface is not there."""
    raise msi_controld.BackendError("no EC debug interface")


class FakeEc:
    """RAM-backed EC used in place of the sysfs debug interface."""

    def __init__(self):
        self.mem = dict(FAKE_EC_INIT)
        self.readonly: set[int] = set()

    def read(self, addr: int) -> int:
        return self.mem.get(addr, 0)

    def write(self, addr: int, val: int):
        if addr in self.readonly:
            return                          # an EC byte that ignores us
        self.mem[addr] = val


# --------------------------------------------------------------------------
# base fixture: fake sysfs + fake EC + live daemon in-process
# --------------------------------------------------------------------------

ENV_VARS = ("MSI_SYSFS", "MSI_SOCK", "MSI_CONFIG", "MSI_STATE")


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="msi-test-")
        self.sysfs = os.path.join(self.tmp, "sys")
        self.sock = os.path.join(self.tmp, "msi.sock")
        self.cfg = os.path.join(self.tmp, "config.json")
        self.state = os.path.join(self.tmp, "state")
        build_fake_sysfs(self.sysfs)

        self._env = {k: os.environ.get(k) for k in ENV_VARS}
        os.environ["MSI_SYSFS"] = self.sysfs
        os.environ["MSI_SOCK"] = self.sock
        os.environ["MSI_CONFIG"] = self.cfg
        os.environ["MSI_STATE"] = self.state

        self.ec = FakeEc()
        self._orig = (MsiEc._xfer_read, MsiEc._xfer_write)
        MsiEc._xfer_read = lambda self_, addr: self.ec.read(addr)
        MsiEc._xfer_write = lambda self_, addr, val: self.ec.write(addr, val)

        self.service = self.make_service(no_restore=True)
        self.server = Server(self.sock, Handler)
        self.server.service = self.service
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        deadline = time.time() + 3
        while not os.path.exists(self.sock) and time.time() < deadline:
            time.sleep(0.01)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        MsiEc._xfer_read, MsiEc._xfer_write = self._orig
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_service(self, no_restore=True, config=None) -> Service:
        if config is not None:
            write(self.cfg, json.dumps(config))
        return Service(Namespace(sysfs=self.sysfs, config=self.cfg,
                                 state=self.state, no_restore=no_restore))

    # convenience ----------------------------------------------------------

    def call(self, cmd):
        return core.call(cmd)

    def run_cli(self, *argv, expect_rc=0):
        """Drive the real `msi-control` against this fixture."""
        env = dict(os.environ)
        proc = subprocess.run(
            [sys.executable, os.path.join(SRC, "msi-control"), *argv],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, expect_rc,
                         f"cli {argv} rc={proc.returncode}\n"
                         f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc

    def read_attr(self, *rel) -> str:
        with open(os.path.join(self.sysfs, "devices", "platform", "msi-ec",
                               *rel), encoding="utf-8") as fh:
            return fh.read().strip()

    def bat_file(self, name) -> str:
        with open(os.path.join(self.sysfs, "class", "power_supply", "BAT1",
                               name), encoding="utf-8") as fh:
            return fh.read().strip()


# --------------------------------------------------------------------------
# validation (pure functions)
# --------------------------------------------------------------------------

class TestValidation(unittest.TestCase):
    def test_good_curve_passes(self):
        temps, speeds = core.check_curve([50, 60, 70, 80, 90, 95],
                                         [0, 40, 55, 65, 80, 90, 100])
        self.assertEqual(temps[0], 50)
        self.assertEqual(len(speeds), 7)

    def test_wrong_point_count(self):
        with self.assertRaises(ValidationError):
            core.check_curve([50, 60], [0, 40, 55, 65, 80, 90, 100])
        with self.assertRaises(ValidationError):
            core.check_curve([50, 60, 70, 80, 90, 95], [0, 40])

    def test_temps_must_increase(self):
        with self.assertRaises(ValidationError):
            core.check_curve([50, 60, 55, 80, 90, 95],
                             [0, 40, 55, 65, 80, 90, 100])

    def test_speeds_must_not_decrease(self):
        with self.assertRaises(ValidationError):
            core.check_curve([50, 60, 70, 80, 90, 95],
                             [0, 60, 55, 65, 80, 90, 100])

    def test_bool_is_not_a_speed(self):
        # True would silently become 1 without this guard.
        with self.assertRaises(ValidationError):
            core.check_curve([50, 60, 70, 80, 90, 95],
                             [True, 60, 60, 60, 60, 60, 60])

    def test_out_of_range(self):
        with self.assertRaises(ValidationError):
            core.check_curve([50, 60, 70, 80, 90, 150],
                             [0, 40, 55, 65, 80, 90, 100])
        with self.assertRaises(ValidationError):
            core.check_curve([50, 60, 70, 80, 90, 95],
                             [0, 40, 55, 65, 80, 90, 140])

    def test_charge_limits(self):
        self.assertEqual(core.check_charge_end(80), 80)
        for bad in (15, 101, "80", None, True, 80.5):
            with self.assertRaises(ValidationError):
                core.check_charge_end(bad)

    def test_profile_names(self):
        self.assertEqual(core.check_profile_name("quiet-office"), "quiet-office")
        for bad in ("", "../evil", "a" * 33, "with space", None, 5):
            with self.assertRaises(ValidationError):
                core.check_profile_name(bad)

    def test_curve_step_semantics(self):
        temps = [50, 60, 70]
        speeds = [0, 40, 60, 90]
        self.assertEqual(core.curve_speed_at(temps, speeds, 49), 0)
        self.assertEqual(core.curve_speed_at(temps, speeds, 50), 0)  # not > yet
        self.assertEqual(core.curve_speed_at(temps, speeds, 51), 40)
        self.assertEqual(core.curve_speed_at(temps, speeds, 71), 90)


# --------------------------------------------------------------------------
# degraded (daemon-less) reads
# --------------------------------------------------------------------------

class TestLocalStatus(DaemonTest):
    def test_local_status_is_partial_but_correct(self):
        st = core.local_status()
        self.assertTrue(st["partial"])
        self.assertTrue(st["driver"]["present"])
        self.assertEqual(st["driver"]["fw"], "16V1EMS1.118")
        self.assertEqual(st["shift"]["current"], "comfort")
        self.assertIn("turbo", st["shift"]["available"])
        self.assertEqual(st["cpu"]["temp"], 52)
        self.assertEqual(st["battery"]["pct"], 67)
        self.assertEqual(st["battery"]["charge_end"], 60)
        self.assertEqual(st["battery"]["charge_start"], 50)
        self.assertFalse(st["battery"]["ac_online"])
        # pieces that need an EC write are honestly reported as missing
        self.assertIsNone(st["cpu"]["rpm"])
        self.assertIsNone(st["curve"])


# --------------------------------------------------------------------------
# daemon over the real socket
# --------------------------------------------------------------------------

class TestProtocol(DaemonTest):
    def test_ping_and_status(self):
        self.assertTrue(self.call({"cmd": "ping"})["pong"])
        st = self.call({"cmd": "status"})
        self.assertFalse(st["partial"])
        self.assertEqual(st["driver"]["fw"], "16V1EMS1.118")
        # RPM: cpu period 0x012c = 300 -> 478000 // 300
        self.assertEqual(st["cpu"]["rpm"], 478000 // 300)
        self.assertEqual(st["gpu"]["rpm"], 478000 // 250)
        # curve comes from the fake EC image
        self.assertEqual(st["curve"]["cpu"]["temps"], [58, 63, 68, 73, 78, 83])
        self.assertEqual(st["curve"]["cpu"]["speeds"],
                         [0, 50, 60, 65, 70, 80, 85])
        self.assertIn("balanced", st["profiles"]["builtin"])

    def test_unknown_command(self):
        with self.assertRaises(core.ProtocolError) as ctx:
            self.call({"cmd": "frobnicate"})
        self.assertIn("unknown command", str(ctx.exception))

    def test_malformed_request(self):
        import socket as _s
        with _s.socket(_s.AF_UNIX, _s.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(self.sock)
            s.sendall(b"this is not json\n")
            resp = json.loads(s.makefile("r").readline())
        self.assertFalse(resp["ok"])
        self.assertIn("JSON", resp["error"])


class TestSettings(DaemonTest):
    def test_set_shift_roundtrip(self):
        self.call({"cmd": "set", "key": "shift", "value": "eco"})
        self.assertEqual(self.read_attr("shift_mode"), "eco")

    def test_set_shift_rejects_garbage(self):
        with self.assertRaises(core.ProtocolError) as ctx:
            self.call({"cmd": "set", "key": "shift", "value": "warp9"})
        self.assertIn("invalid shift mode", str(ctx.exception))
        self.assertEqual(self.read_attr("shift_mode"), "comfort")  # unchanged

    def test_set_fan_and_boost(self):
        self.call({"cmd": "set", "key": "fan", "value": "advanced"})
        self.call({"cmd": "set", "key": "cooler_boost", "value": True})
        self.assertEqual(self.read_attr("fan_mode"), "advanced")
        self.assertEqual(self.read_attr("cooler_boost"), "on")

    def test_set_saves_last_state(self):
        self.call({"cmd": "set", "key": "shift", "value": "turbo"})
        last = os.path.join(self.state, "last.json")
        self.assertTrue(os.path.exists(last))
        with open(last, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["shift"], "turbo")

    def test_charge_end(self):
        self.call({"cmd": "set", "key": "charge_end", "value": 80})
        self.assertEqual(self.bat_file("charge_control_end_threshold"), "80")
        with self.assertRaises(core.ProtocolError):
            self.call({"cmd": "set", "key": "charge_end", "value": 15})

    def test_unknown_key(self):
        with self.assertRaises(core.ProtocolError) as ctx:
            self.call({"cmd": "set", "key": "overclock", "value": 1})
        self.assertIn("unknown setting", str(ctx.exception))

    # -- "what changed" contract -----------------------------------------
    # The point of these is that a confirmation can never claim a change
    # that did not happen: the daemon reads the EC back before and after,
    # and the summary is derived from those two readings.

    def test_set_reports_was_now_and_summary(self):
        out = self.call({"cmd": "set", "key": "shift", "value": "sport"})
        self.assertEqual(out["was"], "comfort")     # the EC's boot state
        self.assertEqual(out["now"], "sport")
        self.assertTrue(out["changed"])
        self.assertEqual(out["summary"], "performance comfort -> sport")
        # The notification form must read as what the person did.
        self.assertEqual(out["activity"], "Set performance mode to sport")

    def test_set_to_same_value_says_so(self):
        self.call({"cmd": "set", "key": "shift", "value": "eco"})
        out = self.call({"cmd": "set", "key": "shift", "value": "eco"})
        self.assertFalse(out["changed"])
        self.assertEqual(out["summary"], "performance already eco")
        self.assertEqual(out["activity"], "Performance mode already eco")

    def test_charge_end_summary_is_a_percentage(self):
        # The fixture starts at 60; write 80 and the sentence must carry
        # both readings with units, because "60 -> 80" alone is ambiguous.
        out = self.call({"cmd": "set", "key": "charge_end", "value": 80})
        self.assertEqual(out["was"], 60)
        self.assertEqual(out["now"], 80)
        self.assertEqual(out["summary"], "charge limit 60% -> 80%")
        self.assertEqual(out["activity"], "Set charge limit to 80%")

    def test_charge_end_no_op_is_honest(self):
        out = self.call({"cmd": "set", "key": "charge_end", "value": 60})
        self.assertFalse(out["changed"])
        self.assertEqual(out["summary"], "charge limit already 60%")
        self.assertEqual(out["activity"], "Charge limit already 60%")

    def test_set_was_comes_from_the_file_not_the_request(self):
        # The boot state of fan_mode is "auto".  If the daemon ever started
        # reporting `was` by echoing something it invented, this is what
        # would catch it: the answer must be whatever the EC file said
        # *before* the write.
        self.assertEqual(self.read_attr("fan_mode"), "auto")
        out = self.call({"cmd": "set", "key": "fan", "value": "advanced"})
        self.assertEqual(out["was"], "auto")
        self.assertEqual(out["summary"], "fan mode auto -> advanced")
        self.assertEqual(out["activity"], "Set fan mode to advanced")

    def test_cooler_boost_boolean(self):
        out = self.call({"cmd": "set", "key": "cooler_boost", "value": True})
        self.assertEqual(out["summary"], "cooler boost off -> on")
        self.assertEqual(out["activity"], "Enabled cooler boost")

    def test_cooler_boost_off_is_disabled_not_enabled(self):
        self.call({"cmd": "set", "key": "cooler_boost", "value": True})
        out = self.call({"cmd": "set", "key": "cooler_boost", "value": False})
        self.assertEqual(out["activity"], "Disabled cooler boost")


class TestDiffHelpers(unittest.TestCase):
    """Pure-function coverage for the sentence builders in msi_control."""

    def test_describe_change(self):
        self.assertEqual(core.describe_change("shift", "eco", "sport"),
                         "performance eco -> sport")
        self.assertEqual(core.describe_change("fan", "auto", "auto"),
                         "fan mode already auto")
        self.assertEqual(core.describe_change("charge_end", 60, 80),
                         "charge limit 60% -> 80%")
        self.assertEqual(core.describe_change("cooler_boost", "off", "on"),
                         "cooler boost off -> on")

    def test_describe_change_never_says_none_to_a_value_silently(self):
        self.assertEqual(core.describe_change("charge_end", None, 60),
                         "charge limit unset -> 60%")

    def test_describe_change_unknown_key_falls_back_to_the_key(self):
        self.assertEqual(core.describe_change("mystery", 1, 2),
                         "mystery 1 -> 2")

    # The activity form is what a person reads in a notification or a status
    # bar: the diff says what moved on the machine, the activity says what
    # they just did.  Both are built from the same EC read-back, so a test
    # here pins the phrasing both front-ends share.

    def test_describe_activity_reads_as_what_the_person_did(self):
        self.assertEqual(core.describe_activity("shift", "comfort", "sport"),
                         "Set performance mode to sport")
        self.assertEqual(core.describe_activity("charge_end", 60, 80),
                         "Set charge limit to 80%")
        self.assertEqual(core.describe_activity("fan", "auto", "advanced"),
                         "Set fan mode to advanced")

    def test_describe_activity_booleans_say_enabled_or_disabled(self):
        # "Set cooler boost to on" is clunky; a toggle reads as an action.
        self.assertEqual(core.describe_activity("cooler_boost", "off", "on"),
                         "Enabled cooler boost")
        self.assertEqual(core.describe_activity("cooler_boost", True, False),
                         "Disabled cooler boost")

    def test_describe_activity_no_op_says_already(self):
        # A no-op must not read as if something happened.
        self.assertEqual(core.describe_activity("shift", "eco", "eco"),
                         "Performance mode already eco")
        self.assertEqual(core.describe_activity("charge_end", 60, 60),
                         "Charge limit already 60%")
        self.assertEqual(core.describe_activity("cooler_boost", "on", "on"),
                         "Cooler boost already on")

    def test_describe_activity_unknown_key_falls_back_to_the_key(self):
        self.assertEqual(core.describe_activity("mystery", 1, 2),
                         "Set mystery to 2")

    def test_describe_activity_no_op_matches_the_diff_no_op(self):
        # The two shapes must never disagree about *whether* a write was a
        # no-op: a diff reading "already" is an activity reading "already".
        for key, was, now in (("shift", "eco", "sport"),
                              ("shift", "eco", "eco"),
                              ("charge_end", 60, 80),
                              ("cooler_boost", "off", "on"),
                              ("fan", "auto", "auto")):
            self.assertEqual(was == now, " already " in
                             core.describe_activity(key, was, now),
                             f"{key}: {was} -> {now}")

    def test_curve_activity_counts_points(self):
        self.assertEqual(core.curve_activity("cpu", 3),
                         "Set CPU fan curve (3 points)")
        self.assertEqual(core.curve_activity("gpu", 1),
                         "Set GPU fan curve (1 point)")   # no stray plural
        self.assertEqual(core.curve_activity("cpu", 0),
                         "CPU fan curve unchanged")

    def test_diff_settings_only_reports_keys_that_moved(self):
        before = {"shift": "eco", "fan": "auto", "charge_end": 60}
        after = {"shift": "sport", "fan": "auto", "charge_end": 60}
        self.assertEqual(core.diff_settings(before, after),
                         ["performance eco -> sport"])

    def test_diff_settings_is_empty_when_nothing_moved(self):
        d = {"shift": "eco", "fan": "auto"}
        self.assertEqual(core.diff_settings(d, dict(d)), [])

    def test_diff_settings_order_is_stable(self):
        # A set() ordering would make the message order random per run.
        before = {"fan": "auto", "shift": "eco", "cooler_boost": "off"}
        after = {"fan": "auto", "shift": "sport", "cooler_boost": "on"}
        out = core.diff_settings(before, after)
        self.assertEqual(out, ["cooler boost off -> on",
                               "performance eco -> sport"])

    def test_curve_points_changed_counts_each_moved_entry(self):
        a = {"temps": [50, 60], "speeds": [0, 30, 40, 50, 60, 70, 80]}
        b = {"temps": [50, 60], "speeds": [0, 30, 40, 50, 60, 70, 80]}
        self.assertEqual(core.curve_points_changed(a, b), 0)
        b2 = {"temps": [50, 65], "speeds": [0, 30, 40, 50, 60, 70, 80]}
        self.assertEqual(core.curve_points_changed(a, b2), 1)
        b3 = {"temps": [50, 65], "speeds": [0, 30, 99, 50, 60, 70, 80]}
        self.assertEqual(core.curve_points_changed(a, b3), 2)

    def test_curve_points_changed_counts_length_differences(self):
        # temps differ only by 1 entry (length); speeds by 5 (length).
        # 1 + 5 = 6.
        a = {"temps": [50, 60], "speeds": [0, 30]}
        b = {"temps": [50, 60, 70], "speeds": [0, 30, 40, 50, 60, 70, 80]}
        self.assertEqual(core.curve_points_changed(a, b), 6)

    def test_temp_band_edges(self):
        self.assertEqual(core.temp_band(None), None)
        self.assertEqual(core.temp_band(49), core.BAND_COOL)
        self.assertEqual(core.temp_band(50), core.BAND_WARM)
        self.assertEqual(core.temp_band(70), core.BAND_HOT)
        self.assertEqual(core.temp_band(85), core.BAND_CRIT)


class TestCurves(DaemonTest):
    def test_write_curve_to_ec(self):
        temps = [50, 55, 60, 70, 80, 90]
        speeds = [0, 30, 50, 60, 80, 90, 100]
        out = self.call({"cmd": "curve", "which": "cpu",
                         "temps": temps, "speeds": speeds})
        self.assertEqual(out["temps"], temps)
        for addr, val in zip(core.CURVE_REGS["cpu"]["temps"], temps):
            self.assertEqual(self.ec.mem[addr], val)
        for addr, val in zip(core.CURVE_REGS["cpu"]["speeds"], speeds):
            self.assertEqual(self.ec.mem[addr], val)

    def test_curve_rejects_non_monotonic(self):
        with self.assertRaises(core.ProtocolError) as ctx:
            self.call({"cmd": "curve", "which": "gpu",
                       "temps": [50, 55, 60, 70, 80, 80],
                       "speeds": [0, 30, 50, 60, 80, 90, 100]})
        self.assertIn("strictly increase", str(ctx.exception))

    def test_ec_byte_that_ignores_us_is_caught(self):
        # Make one register read-only: verify-by-readback must complain.
        self.ec.readonly.add(0x73)
        with self.assertRaises(core.ProtocolError) as ctx:
            self.call({"cmd": "curve", "which": "cpu",
                       "temps": [50, 55, 60, 70, 80, 90],
                       "speeds": [0, 30, 50, 60, 80, 90, 100]})
        self.assertIn("rejected", str(ctx.exception))


class TestProfiles(DaemonTest):
    def test_save_apply_delete(self):
        # move the machine somewhere distinctive, snapshot it
        self.call({"cmd": "set", "key": "shift", "value": "sport"})
        self.call({"cmd": "set", "key": "fan", "value": "advanced"})
        self.call({"cmd": "profile", "action": "save-current", "name": "mine"})

        lst = self.call({"cmd": "profile", "action": "list"})
        self.assertIn("mine", lst["saved"])

        # scramble, then apply the snapshot
        self.call({"cmd": "set", "key": "shift", "value": "eco"})
        self.call({"cmd": "set", "key": "fan", "value": "auto"})
        out = self.call({"cmd": "profile", "action": "apply", "name": "mine"})
        self.assertEqual(out["applied"], "mine")
        self.assertEqual(self.read_attr("shift_mode"), "sport")
        self.assertEqual(self.read_attr("fan_mode"), "advanced")

        self.call({"cmd": "profile", "action": "delete", "name": "mine"})
        lst = self.call({"cmd": "profile", "action": "list"})
        self.assertNotIn("mine", lst["saved"])

    def test_builtin_profile_applies(self):
        out = self.call({"cmd": "profile", "action": "apply", "name": "silent"})
        self.assertEqual(self.read_attr("shift_mode"), "eco")
        self.assertEqual(self.read_attr("fan_mode"), "silent")
        # The charge limit is deliberately NOT part of "silent" — see the
        # two tests below.  The fake battery starts at 60 and must still be.
        self.assertEqual(self.bat_file("charge_control_end_threshold"), "60")
        self.assertEqual(out["warnings"], [])

    def test_performance_profiles_leave_the_charge_limit_alone(self):
        # Regression for a real footgun: every profile except "battery" used
        # to carry charge_end: 100, so applying one silently undid a limit
        # the user had deliberately picked.  The daemon's own journal
        # recorded it as:
        #     applied profile balanced: charge limit 60% -> 100%
        # A profile changes how fast the machine goes, not how full the
        # battery is allowed to get.
        for name in ("silent", "balanced", "gaming"):
            self.call({"cmd": "set", "key": "charge_end", "value": 80})
            out = self.call({"cmd": "profile", "action": "apply", "name": name})
            self.assertEqual(
                self.bat_file("charge_control_end_threshold"), "80",
                f"profile {name} moved the charge limit: {out}")
            self.assertFalse([c for c in (out["changes"] or [])
                              if "charge limit" in c], out["changes"])
            # and it must not be reported as an activity either — the
            # confirmation has to agree with the hardware.
            self.assertFalse([a for a in (out["activities"] or [])
                              if "charge limit" in a], out["activities"])

    def test_battery_profile_still_sets_the_limit(self):
        # The exception is the entire point of that profile.
        self.call({"cmd": "set", "key": "charge_end", "value": 80})
        out = self.call({"cmd": "profile", "action": "apply", "name": "battery"})
        self.assertEqual(self.bat_file("charge_control_end_threshold"), "60")
        self.assertIn("Set charge limit to 60%", out["activities"])

    def test_only_battery_carries_a_charge_end(self):
        # Pins the shape of the table itself, because the whole mechanism is
        # "the key is absent".  Restoring the key to tidy up the table would
        # silently re-open the footgun with every test still green.
        with_end = {n for n, p in core.BUILTIN_PROFILES.items()
                    if "charge_end" in p}
        self.assertEqual(with_end, {"battery"})
        self.assertEqual(core.BUILTIN_PROFILES["battery"]["charge_end"], 60)

    def test_profile_apply_reports_activities_not_just_diffs(self):
        # A profile apply must say what it did as sentences a person reads,
        # not only the `a -> b` diffs kept for scripts.
        out = self.call({"cmd": "profile", "action": "apply",
                         "name": "gaming"})
        self.assertTrue(out["activities"], out)
        self.assertIn("Set performance mode to sport", out["activities"])
        self.assertIn("Set fan mode to advanced", out["activities"])
        # and the joined form the status bar shows carries the profile name
        self.assertIn("Applied gaming", out["activity"])

    def test_profile_apply_that_changes_nothing_says_so_in_both_forms(self):
        self.call({"cmd": "profile", "action": "apply", "name": "balanced"})
        out = self.call({"cmd": "profile", "action": "apply",
                         "name": "balanced"})
        self.assertEqual(out["changes"], [])
        self.assertEqual(out["activities"], [])
        self.assertIn("nothing to change", out["activity"])

    def test_gaming_profile_writes_curve(self):
        self.call({"cmd": "profile", "action": "apply", "name": "gaming"})
        self.assertEqual(self.read_attr("shift_mode"), "sport")
        self.assertEqual(self.read_attr("fan_mode"), "advanced")
        # gaming profile's first cpu speed point lives at 0x72
        self.assertEqual(self.ec.mem[0x72], 0)

    def test_delete_builtin_rejected(self):
        with self.assertRaises(core.ProtocolError):
            self.call({"cmd": "profile", "action": "delete", "name": "silent"})

    def test_show_reads_profile_without_applying(self):
        # "show" is what the GUI uses to ghost the factory curve — it must
        # never touch the EC.
        before = dict(self.ec.mem)
        self.service.capture_factory()
        out = self.call({"cmd": "profile", "action": "show", "name": "factory"})
        self.assertIn("curve", out)
        self.assertEqual(out["curve"]["cpu"]["temps"], [58, 63, 68, 73, 78, 83])
        self.assertEqual(self.ec.mem, before)      # nothing was written
        # a built-in is readable too, and unknown names are refused
        out = self.call({"cmd": "profile", "action": "show", "name": "gaming"})
        self.assertEqual(out["shift"], "sport")
        with self.assertRaises(core.ProtocolError):
            self.call({"cmd": "profile", "action": "show", "name": "nope"})
    def test_apply_missing_profile(self):
        with self.assertRaises(core.ProtocolError) as ctx:
            self.call({"cmd": "profile", "action": "apply", "name": "nope"})
        self.assertIn("does not exist", str(ctx.exception))

    def test_hostile_profile_name(self):
        with self.assertRaises(core.ProtocolError):
            self.call({"cmd": "profile", "action": "apply",
                       "name": "../../etc/passwd"})

    def test_factory_profile_captured_once(self):
        self.service.capture_factory()
        first = None
        with open(os.path.join(self.state, "profiles", "factory.json"),
                  encoding="utf-8") as fh:
            first = fh.read()
        # mutate the EC, capture again — the original must be preserved
        self.ec.mem[0x72] = 99
        self.service.capture_factory()
        with open(os.path.join(self.state, "profiles", "factory.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(fh.read(), first)
        self.assertIn("factory",
                      self.call({"cmd": "profile", "action": "list"})["saved"])


class TestRestore(DaemonTest):
    def test_last_state_survives_restart(self):
        self.call({"cmd": "set", "key": "shift", "value": "turbo"})
        self.call({"cmd": "set", "key": "fan", "value": "silent"})
        # simulate a reboot: fresh service, restore enabled
        svc2 = self.make_service(no_restore=False)
        svc2.restore()
        self.assertEqual(self.read_attr("shift_mode"), "turbo")
        self.assertEqual(self.read_attr("fan_mode"), "silent")

    def test_default_profile_overrides_last(self):
        write(self.cfg, json.dumps({"DefaultProfile": "battery"}))
        self.call({"cmd": "set", "key": "shift", "value": "sport"})
        svc2 = Service(Namespace(sysfs=self.sysfs, config=self.cfg,
                                 state=self.state, no_restore=False))
        svc2.restore()
        self.assertEqual(self.read_attr("shift_mode"), "eco")
        self.assertEqual(self.bat_file("charge_control_end_threshold"), "60")

    def test_reapply_command(self):
        self.call({"cmd": "set", "key": "shift", "value": "eco"})
        # firmware "resets" the register behind our back
        write(os.path.join(self.sysfs, "devices", "platform", "msi-ec",
                           "shift_mode"), "sport\n")
        self.call({"cmd": "reapply"})
        self.assertEqual(self.read_attr("shift_mode"), "eco")


class TestRawGate(DaemonTest):
    def test_raw_disabled_by_default(self):
        with self.assertRaises(core.ProtocolError) as ctx:
            self.call({"cmd": "raw", "addr": 0xF4})
        self.assertIn("disabled", str(ctx.exception))

    def test_raw_when_enabled(self):
        svc = self.make_service(no_restore=True,
                                config={"AllowRaw": True})
        self.service = svc
        self.server.service = svc
        self.assertEqual(self.call({"cmd": "raw", "addr": 0xF4})["val"], 0x0D
                         if 0xF4 in self.ec.mem else
                         self.call({"cmd": "raw", "addr": 0xF4})["val"])
        out = self.call({"cmd": "raw", "addr": 0x98, "val": 0x80})
        self.assertEqual(out["val"], 0x80)
        self.assertEqual(self.ec.mem[0x98], 0x80)


# --------------------------------------------------------------------------
# CLI as a real subprocess
# --------------------------------------------------------------------------

class TestCli(DaemonTest):
    def test_status_json(self):
        proc = self.run_cli("status", "--json")
        st = json.loads(proc.stdout)
        self.assertEqual(st["shift"]["current"], "comfort")
        self.assertFalse(st["partial"])
        self.assertEqual(st["cpu"]["rpm"], 478000 // 300)

    def test_json_flag_before_the_subcommand_is_not_dropped(self):
        # Regression: `--json` placed BEFORE the subcommand used to be
        # silently ignored, printing human text.  argparse's set_defaults()
        # mutated the .default of the shared --json action (reused by every
        # subparser via parents=[common]) from SUPPRESS to False, so the
        # subparser wrote json=False into the namespace and clobbered a
        # `--json status`.  A script reading JSON off stdout would have got
        # prose with exit code 0 — the worst possible failure mode.
        proc = self.run_cli("--json", "status")
        st = json.loads(proc.stdout)          # raises if it is not JSON
        self.assertEqual(st["shift"]["current"], "comfort")

        proc = self.run_cli("--json", "mode")
        out = json.loads(proc.stdout)
        self.assertEqual(out["current"], "comfort")
        self.assertIn("sport", out["available"])

        proc = self.run_cli("--json", "boost")
        self.assertIn("cooler_boost", json.loads(proc.stdout))

    def test_json_flag_position_does_not_change_the_answer(self):
        # Both positions must produce the same parsed value, not just both
        # be JSON — a flag that quietly changed *which* command ran would
        # still parse fine here.
        before = json.loads(self.run_cli("--json", "mode").stdout)
        after = json.loads(self.run_cli("mode", "--json").stdout)
        self.assertEqual(before, after)

    def test_no_json_flag_still_prints_human_text(self):
        # The default must stay human: the fix must not have flipped the
        # whole CLI into machine output.
        proc = self.run_cli("mode")
        self.assertNotIn("{", proc.stdout.splitlines()[0])
        self.assertEqual(proc.stdout.splitlines()[0], "comfort")

    def test_show_paths_honour_json(self):
        # mode/fan/boost with no value used to ignore --json entirely.
        for cmd in ("mode", "fan", "boost"):
            proc = self.run_cli(cmd, "--json")
            json.loads(proc.stdout)          # raises unless it is JSON

    def test_mode_and_fan(self):
        self.run_cli("mode", "sport")
        self.assertEqual(self.read_attr("shift_mode"), "sport")
        self.assertEqual(self.run_cli("mode").stdout.splitlines()[0], "sport")
        self.run_cli("fan", "advanced")
        self.assertEqual(self.read_attr("fan_mode"), "advanced")

    def test_battery(self):
        self.run_cli("battery", "80")
        self.assertEqual(self.bat_file("charge_control_end_threshold"), "80")

    def test_bad_mode_value_fails(self):
        proc = self.run_cli("mode", "warp9", expect_rc=1)
        self.assertIn("invalid shift mode", proc.stderr)

    def test_curve_show_and_set(self):
        self.run_cli("curve", "gpu", "--temps", "55,60,65,70,75,85",
                     "--speeds", "0,50,60,70,80,90,100")
        for addr, val in zip(core.CURVE_REGS["gpu"]["temps"],
                             [55, 60, 65, 70, 75, 85]):
            self.assertEqual(self.ec.mem[addr], val)
        proc = self.run_cli("curve")
        self.assertIn("CPU curve", proc.stdout)
        self.assertIn("GPU curve", proc.stdout)

    def test_profile_flow(self):
        self.run_cli("mode", "eco")
        self.run_cli("profile", "save", "office")
        self.run_cli("mode", "sport")
        self.run_cli("profile", "apply", "office")
        self.assertEqual(self.read_attr("shift_mode"), "eco")
        proc = self.run_cli("profile", "list")
        self.assertIn("office", proc.stdout)

    def test_profile_show_does_not_apply(self):
        self.run_cli("mode", "eco")
        proc = self.run_cli("profile", "show", "gaming")
        out = json.loads(proc.stdout)
        self.assertEqual(out["shift"], "sport")       # gaming's content
        self.assertEqual(self.read_attr("shift_mode"), "eco")  # unchanged

    def test_status_without_daemon_is_degraded_not_broken(self):
        # kill the daemon: CLI must still answer from sysfs
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        proc = self.run_cli("status", "--json")
        st = json.loads(proc.stdout)
        self.assertTrue(st["partial"])
        self.assertEqual(st["shift"]["current"], "comfort")
        # and a write must fail with a helpful hint
        proc = self.run_cli("mode", "eco", expect_rc=1)
        self.assertIn("msi-controld", proc.stderr)


# --------------------------------------------------------------------------
# graphics mode (MUX)
#
# The switch is the one setting here that can survive a reboot in a state the
# user did not want, so the tests are as much about what it must *refuse* to
# do — write while disabled, write on a firmware that has no MUX, write
# without a backup — as about the bytes it writes.
# --------------------------------------------------------------------------

class TestMuxParsing(unittest.TestCase):
    def test_ec_dump_row_labels_give_the_address(self):
        # The real file is a table of 16 rows.  Addresses must come from the
        # row label, never from counting bytes: a header line or a changed
        # row width then shifts nothing.
        dump = ("|  0x0_ | 00 11 22 33 44 55 66 77 88 99 aa bb cc dd ee ff |\n"
                "|  0x2_ | 10 20 30 40 50 60 70 80 90 a0 b0 c0 d0 e0 5b ff |\n")
        vm = core.parse_ec_dump(dump)
        self.assertEqual(vm[0x00], 0x00)
        self.assertEqual(vm[0x0F], 0xFF)
        self.assertEqual(vm[0x20], 0x10)
        self.assertEqual(vm[0x2E], 0x5B)
        self.assertEqual(vm[0x2F], 0xFF)

    def test_ec_dump_ignores_junk(self):
        self.assertEqual(core.parse_ec_dump(""), {})
        self.assertEqual(core.parse_ec_dump("hello\n-----\n"), {})
        self.assertEqual(core.parse_ec_dump(None), {})

    def test_ec_byte_polarity(self):
        self.assertEqual(core.mux_from_ec_byte(0x5B), core.MUX_DISCRETE)
        self.assertEqual(core.mux_from_ec_byte(0x1B), core.MUX_HYBRID)
        # The webcam bit lives in the same byte and must not be mistaken
        # for the graphics bit.
        self.assertEqual(core.mux_from_ec_byte(0x02), core.MUX_HYBRID)
        self.assertIsNone(core.mux_from_ec_byte(None))

    def test_var_split_and_polarity(self):
        raw = b"\x07\x00\x00\x00" + bytes([0x01, 0x01]) + bytes(20)
        attrs, data = core.parse_mux_var(raw)
        self.assertEqual(attrs, 0x07)
        self.assertEqual(len(data), 22)
        self.assertEqual(core.mux_from_var(raw), core.MUX_DISCRETE)
        self.assertEqual(core.mux_from_var(b"\x07\x00\x00\x00\x01\x00"),
                         core.MUX_HYBRID)
        # absent / truncated must be None, never a guess
        self.assertIsNone(core.mux_from_var(None))
        self.assertIsNone(core.mux_from_var(b""))
        self.assertIsNone(core.mux_from_var(b"\x07\x00\x00\x00\x01"))

    def test_mode_validation(self):
        for good in core.MUX_MODES:
            self.assertEqual(core.check_mux_mode(good), good)
        for bad in ("Hybrid", "mixed", "", None, 1, True, "../x"):
            with self.assertRaises(ValidationError):
                core.check_mux_mode(bad)

    def test_activity_sentence_is_honest_about_the_reboot(self):
        s = core.graphics_activity(core.MUX_HYBRID, core.MUX_DISCRETE,
                                   core.MUX_HYBRID)
        self.assertIn("Hybrid", s)
        self.assertIn("reboot", s)
        # A no-op must not read as a switch.
        same = core.graphics_activity(core.MUX_DISCRETE, core.MUX_DISCRETE,
                                      core.MUX_DISCRETE)
        self.assertIn("already", same)
        self.assertNotIn("Switched", same)


class TestGraphicsRead(DaemonTest):
    def test_daemon_reports_all_three_sources(self):
        g = self.call({"cmd": "graphics"})
        # fake sysfs: EC bit set, var byte 1 = 1, no Intel display device
        self.assertEqual(g["ec_mode"], core.MUX_DISCRETE)
        self.assertEqual(g["var_mode"], core.MUX_DISCRETE)
        self.assertEqual(g["requested"], core.MUX_DISCRETE)
        self.assertEqual(g["effective"], core.MUX_DISCRETE)
        self.assertFalse(g["reboot_required"])
        self.assertTrue(g["supported"])
        self.assertFalse(g["enabled"])          # AllowMuxSwitch defaults off

    def test_status_carries_graphics(self):
        st = self.call({"cmd": "status"})
        self.assertIn("graphics", st)
        self.assertEqual(st["graphics"]["effective"], core.MUX_DISCRETE)
        self.assertIn("AllowMuxSwitch", st["config"])
        self.assertIn("SocketGroup", st["config"])

    def test_offline_view_still_states_the_mode(self):
        st = core.local_status(mux=True)
        self.assertEqual(st["graphics"]["effective"], core.MUX_DISCRETE)
        self.assertEqual(st["graphics"]["requested"], core.MUX_DISCRETE)
        # ...but it is honest that it could not read the EC register.
        self.assertIsNone(st["graphics"]["ec_bit"])
        self.assertFalse(st["graphics"]["enabled"])

    def test_offline_view_can_skip_the_graphics_block(self):
        # The daemon builds its own; paying for the PCI scan twice per poll
        # would be pure waste.
        self.assertIsNone(core.local_status(mux=False)["graphics"])

    def test_an_intel_gpu_flips_effective_and_raises_the_reboot_flag(self):
        # var says discrete (next boot), iGPU present (running now) — the
        # machine is mid-switch.
        add_fake_igpu(self.sysfs)
        svc = self.make_service()
        g = svc.handle({"cmd": "graphics"})
        self.assertEqual(g["effective"], core.MUX_HYBRID)
        self.assertEqual(g["requested"], core.MUX_DISCRETE)
        self.assertTrue(g["reboot_required"])
        # ...and it agrees with the unprivileged view.
        self.assertTrue(core.igpu_present())
        self.assertTrue(core.local_graphics()["reboot_required"])

    def test_igpu_absence_is_not_an_error(self):
        self.assertFalse(core.igpu_present())
        self.assertEqual(core.local_graphics()["igpu_present"], False)


class TestGraphicsGate(DaemonTest):
    def test_disabled_by_default(self):
        # Over the socket the daemon's refusal arrives in its wire form.
        with self.assertRaises(core.ProtocolError) as ctx:
            self.call({"cmd": "graphics", "action": "set", "mode": "hybrid"})
        self.assertIn("AllowMuxSwitch", str(ctx.exception))
        self.assertIn("disabled", str(ctx.exception))

    def test_refused_switch_changes_nothing(self):
        before = dict(self.ec.mem)
        with self.assertRaises(core.ProtocolError):
            self.call({"cmd": "graphics", "action": "set", "mode": "hybrid"})
        self.assertEqual(self.ec.mem, before)
        # and no backup was written for a switch that never happened
        self.assertFalse(os.path.exists(
            os.path.join(self.state, "mux-backup.json")))

    def test_revert_is_gated_too(self):
        with self.assertRaises(core.ProtocolError):
            self.call({"cmd": "graphics", "action": "revert"})

    def test_unknown_action_refused(self):
        with self.assertRaises(core.ProtocolError):
            self.call({"cmd": "graphics", "action": "detonate"})

    def test_bad_mode_refused(self):
        svc = self.make_service(config={"AllowMuxSwitch": True})
        with self.assertRaises(ValidationError):
            svc.handle({"cmd": "graphics", "action": "set", "mode": "both"})


class TestGraphicsWrite(DaemonTest):
    def setUp(self):
        super().setUp()
        self.svc = self.make_service(config={"AllowMuxSwitch": True})

    def var_raw(self) -> bytes:
        with open(core.mux_var_path(), "rb") as fh:
            return fh.read()

    def test_switch_to_hybrid_writes_all_three_places(self):
        out = self.svc.handle({"cmd": "graphics", "action": "set",
                               "mode": core.MUX_HYBRID})
        # 1. the UEFI variable the firmware reads at POST
        raw = self.var_raw()
        self.assertEqual(raw[4 + 1], 0x00)
        self.assertEqual(raw[:4], b"\x07\x00\x00\x00")   # attrs preserved
        self.assertEqual(len(raw), 26)                   # length preserved
        self.assertEqual(core.mux_from_var(raw), core.MUX_HYBRID)
        # 2. the EC state bit
        self.assertEqual(self.ec.mem[core.MUX_EC_ADDR] & core.MUX_EC_MASK, 0)
        # 3. the EC trigger, armed
        self.assertEqual(self.ec.mem[core.MUX_TRIGGER_ADDR],
                         core.MUX_TRIGGER_ARM)
        self.assertEqual(out["now"], core.MUX_HYBRID)
        self.assertEqual(out["was"], core.MUX_DISCRETE)
        self.assertTrue(out["changed"])
        self.assertIn("Hybrid", out["activity"])
        self.assertIn("reboot", out["activity"])

    def test_switch_to_discrete_sets_the_bit(self):
        self.svc.handle({"cmd": "graphics", "action": "set",
                         "mode": core.MUX_HYBRID})
        self.svc.handle({"cmd": "graphics", "action": "set",
                         "mode": core.MUX_DISCRETE})
        self.assertEqual(self.ec.mem[core.MUX_EC_ADDR] & core.MUX_EC_MASK,
                         core.MUX_EC_MASK)
        self.assertEqual(self.var_raw()[5], 0x01)

    def test_read_modify_write_preserves_the_neighbour_bits(self):
        # 0x2e bit 1 is the webcam.  A switch must not touch it.
        self.ec.mem[core.MUX_EC_ADDR] = core.MUX_EC_MASK | 0x02
        self.svc.handle({"cmd": "graphics", "action": "set",
                         "mode": core.MUX_HYBRID})
        self.assertEqual(self.ec.mem[core.MUX_EC_ADDR], 0x02)
        self.svc.handle({"cmd": "graphics", "action": "set",
                         "mode": core.MUX_DISCRETE})
        self.assertEqual(self.ec.mem[core.MUX_EC_ADDR], core.MUX_EC_MASK | 0x02)

    def test_the_rest_of_the_ec_is_untouched(self):
        before = dict(self.ec.mem)
        self.svc.handle({"cmd": "graphics", "action": "set",
                         "mode": core.MUX_HYBRID})
        changed = {a for a in set(before) | set(self.ec.mem)
                   if before.get(a) != self.ec.mem.get(a)}
        self.assertEqual(changed, {core.MUX_EC_ADDR, core.MUX_TRIGGER_ADDR})

    def test_backup_records_the_exact_previous_bytes(self):
        before_var = self.var_raw()
        before_ec = self.ec.mem[core.MUX_EC_ADDR]
        out = self.svc.handle({"cmd": "graphics", "action": "set",
                               "mode": core.MUX_HYBRID})
        self.assertEqual(out["backup"]["var"], before_var.hex())
        self.assertEqual(out["backup"]["ec_bit"], before_ec)
        self.assertEqual(out["backup"]["requested"], core.MUX_DISCRETE)
        # it is on disk, and it is what the status reports
        path = os.path.join(self.state, "mux-backup.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["var"], before_var.hex())
        self.assertEqual(self.svc.handle({"cmd": "graphics"})["backup"]["var"],
                         before_var.hex())

    def test_revert_restores_bytes_for_byte(self):
        before_var = self.var_raw()
        before_ec = self.ec.mem[core.MUX_EC_ADDR]
        self.svc.handle({"cmd": "graphics", "action": "set",
                         "mode": core.MUX_HYBRID})
        self.assertNotEqual(self.var_raw(), before_var)
        out = self.svc.handle({"cmd": "graphics", "action": "revert"})
        self.assertEqual(self.var_raw(), before_var)
        self.assertEqual(self.ec.mem[core.MUX_EC_ADDR], before_ec)
        self.assertEqual(out["now"], core.MUX_DISCRETE)
        self.assertIn("reboot", out["activity"])

    def test_revert_without_a_backup_is_an_error_not_a_shrug(self):
        with self.assertRaises(msi_controld.BackendError) as ctx:
            self.svc.handle({"cmd": "graphics", "action": "revert"})
        self.assertIn("backup", str(ctx.exception))

    def test_a_corrupt_backup_does_not_write_garbage(self):
        self.svc.handle({"cmd": "graphics", "action": "set",
                         "mode": core.MUX_HYBRID})
        path = os.path.join(self.state, "mux-backup.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"requested": "hybrid", "var": "not hex"}, fh)
        with self.assertRaises(msi_controld.BackendError):
            self.svc.handle({"cmd": "graphics", "action": "revert"})

    def test_switching_to_the_mode_you_are_already_in_is_a_no_op(self):
        before_var = self.var_raw()
        out = self.svc.handle({"cmd": "graphics", "action": "set",
                               "mode": core.MUX_DISCRETE})
        self.assertFalse(out["changed"])
        self.assertFalse(out["reboot_required"])
        self.assertIn("already", out["activity"])
        self.assertEqual(self.var_raw(), before_var)
        # ...and it did not overwrite a real backup with a no-op one
        self.assertFalse(os.path.exists(
            os.path.join(self.state, "mux-backup.json")))

    def test_repeat_switch_still_asks_for_the_reboot(self):
        # Requested == mode but the machine is still running the old one:
        # re-issuing must not claim "all done".
        add_fake_igpu(self.sysfs)
        svc = self.make_service(config={"AllowMuxSwitch": True})
        out = svc.handle({"cmd": "graphics", "action": "set",
                          "mode": core.MUX_DISCRETE})
        self.assertTrue(out["reboot_required"])
        self.assertEqual(out["graphics"]["effective"], core.MUX_HYBRID)

    def test_a_firmware_without_a_mux_is_refused(self):
        # Nothing at all to read: no variable, and no EC debug interface.
        os.remove(core.mux_var_path())
        MsiEc._xfer_read = lambda _s, addr: no_ec_access(addr)
        svc = self.make_service(config={"AllowMuxSwitch": True})
        with self.assertRaises(msi_controld.BackendError) as ctx:
            svc.handle({"cmd": "graphics", "action": "set",
                        "mode": core.MUX_HYBRID})
        self.assertIn("not available", str(ctx.exception))

    def test_short_variable_is_refused(self):
        with open(core.mux_var_path(), "wb") as fh:
            fh.write(b"\x07\x00\x00\x00\x01")
        svc = self.make_service(config={"AllowMuxSwitch": True})
        with self.assertRaises(msi_controld.BackendError) as ctx:
            svc.handle({"cmd": "graphics", "action": "set",
                        "mode": core.MUX_HYBRID})
        self.assertIn("too short", str(ctx.exception))


class TestEfivarWrite(unittest.TestCase):
    """The efivarfs write itself: one syscall, flag off, flag back on."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="msi-efi-")
        self.path = os.path.join(self.tmp, "MsiDCVarData")
        with open(self.path, "wb") as fh:
            fh.write(b"\x07\x00\x00\x00" + bytes([0x01, 0x01]) + bytes(20))
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_ioctl(self, fd, req, arg=b"\x00\x00\x00\x00"):
        if req == msi_controld.FS_IOC_GETFLAGS:
            self.calls.append(("get", struct.unpack("I", arg)[0]))
            return struct.pack("I", msi_controld.FS_IMMUTABLE_FL)
        if req == msi_controld.FS_IOC_SETFLAGS:
            flags = struct.unpack("I", arg)[0]
            self.calls.append(("set", flags))
            return 0
        raise AssertionError(f"unexpected ioctl {req:#x}")

    def test_immutable_is_cleared_around_the_write_and_restored(self):
        payload = b"\x07\x00\x00\x00" + bytes([0x01, 0x00]) + bytes(20)
        with mock.patch.object(msi_controld.fcntl, "ioctl",
                               side_effect=self.fake_ioctl):
            MsiEc._efivar_write(self.path, payload)
        self.assertEqual([c[0] for c in self.calls], ["get", "set", "set"])
        self.assertFalse(self.calls[1][1] & msi_controld.FS_IMMUTABLE_FL)
        self.assertTrue(self.calls[2][1] & msi_controld.FS_IMMUTABLE_FL)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), payload)

    def test_flag_goes_back_on_even_when_the_write_fails(self):
        with mock.patch.object(msi_controld.fcntl, "ioctl",
                               side_effect=self.fake_ioctl), \
             mock.patch.object(msi_controld.os, "write",
                               side_effect=OSError(errno.EIO, "no")), \
             self.assertRaises(msi_controld.BackendError):
            MsiEc._efivar_write(self.path, b"x")
        # A variable left permanently mutable is worse than a failed switch.
        self.assertEqual([c[0] for c in self.calls], ["get", "set", "set"])
        self.assertTrue(self.calls[2][1] & msi_controld.FS_IMMUTABLE_FL)

    def test_a_filesystem_without_inode_flags_still_writes(self):
        def enotty(fd, req, arg=b""):
            raise OSError(errno.ENOTTY, "not a tty")

        payload = b"\x07\x00\x00\x00" + bytes([0x01, 0x00]) + bytes(20)
        with mock.patch.object(msi_controld.fcntl, "ioctl", side_effect=enotty):
            MsiEc._efivar_write(self.path, payload)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), payload)

    def test_a_short_write_is_an_error_not_a_success(self):
        with mock.patch.object(msi_controld.fcntl, "ioctl",
                               side_effect=self.fake_ioctl), \
             mock.patch.object(msi_controld.os, "write", return_value=3), \
             self.assertRaises(msi_controld.BackendError) as ctx:
            MsiEc._efivar_write(self.path, b"0123456789")
        self.assertIn("short write", str(ctx.exception))


class TestGraphicsCli(DaemonTest):
    def test_show_prints_both_modes(self):
        proc = self.run_cli("graphics")
        self.assertIn("Graphics", proc.stdout)
        self.assertIn("Discrete", proc.stdout)
        self.assertIn("running", proc.stdout)
        self.assertIn("next boot", proc.stdout)

    def test_show_json_is_the_graphics_block(self):
        out = json.loads(self.run_cli("graphics", "--json").stdout)
        self.assertEqual(out["effective"], core.MUX_DISCRETE)
        self.assertIn("reboot_required", out)

    def test_status_shows_a_graphics_line(self):
        self.assertIn("Gfx", self.run_cli("status").stdout)

    def test_switch_without_yes_refuses_and_says_why(self):
        proc = self.run_cli("graphics", "hybrid", expect_rc=1)
        self.assertIn("--yes", proc.stderr)
        self.assertIn("reboot", proc.stderr)
        # nothing was written
        self.assertEqual(self.read_attr("shift_mode"), "comfort")

    def test_switch_with_yes_reports_the_disabled_config(self):
        proc = self.run_cli("graphics", "hybrid", "--yes", expect_rc=1)
        self.assertIn("AllowMuxSwitch", proc.stderr)

    def test_a_bad_mode_is_rejected_by_argparse(self):
        proc = self.run_cli("graphics", "quantum", expect_rc=2)
        self.assertIn("invalid choice", proc.stderr)


# --------------------------------------------------------------------------
# daemon main(): socket lifecycle as a real subprocess
# --------------------------------------------------------------------------

class TestDaemonProcess(unittest.TestCase):
    def test_start_ping_stop(self):
        tmp = tempfile.mkdtemp(prefix="msi-proc-")
        old = {k: os.environ.get(k) for k in ("MSI_SOCK", "MSI_CONFIG",
                                              "MSI_STATE")}
        try:
            sysfs = os.path.join(tmp, "sys")
            sock = os.path.join(tmp, "d.sock")
            build_fake_sysfs(sysfs)
            env = dict(os.environ)
            env.update({"MSI_SYSFS": sysfs, "MSI_SOCK": sock,
                        "MSI_CONFIG": os.path.join(tmp, "cfg.json"),
                        "MSI_STATE": os.path.join(tmp, "state")})
            # the *parent* must look at the same socket as the child
            os.environ["MSI_SOCK"] = sock
            proc = subprocess.Popen(
                [sys.executable, os.path.join(SRC, "msi_controld.py"),
                 "--no-restore", "--wait", "2", "-v"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True)
            try:
                deadline = time.time() + 10
                ready = False
                while time.time() < deadline:
                    try:
                        data = core.send_cmd({"cmd": "ping"}, timeout=1)
                        if data.get("ok"):
                            ready = True
                            break
                    except (OSError, core.ProtocolError):
                        time.sleep(0.1)
                self.assertTrue(ready, "daemon did not come up")
                st = core.call({"cmd": "status"})
                self.assertEqual(st["driver"]["fw"], "16V1EMS1.118")
            finally:
                proc.send_signal(signal.SIGTERM)
                try:
                    out = proc.communicate(timeout=10)[0]
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out = proc.communicate()[0]
            # SIGTERM is handled cleanly: rc 0, not killed by a signal
            self.assertEqual(proc.returncode, 0)
            # socket must be cleaned up
            self.assertFalse(os.path.exists(sock))
            self.assertIn("listening on", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# --------------------------------------------------------------------------
# The model registry
# --------------------------------------------------------------------------

class TestRegistry(unittest.TestCase):
    """The model files, and the names they publish into msi_control."""

    def test_tests_pin_a_model_regardless_of_the_machine(self):
        # A test run must never depend on the laptop it is running on; the
        # sysfs here is fake and the DMI unknown.  MSI_MODEL is the pin.
        self.assertEqual(core.MODEL.id, "gs66-stealth-10se")
        self.assertTrue(core.model_supported())

    def test_the_gs66_registers_are_the_documented_ones(self):
        # This is the one place a register address may be spelled; the whole
        # point of the registry is that a wrong address is caught here rather
        # than written into somebody's EC.
        c = core.CURVE_REGS
        self.assertEqual(c["cpu"]["temps"],
                         [0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F])
        self.assertEqual(c["cpu"]["speeds"],
                         [0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78])
        self.assertEqual(c["gpu"]["temps"],
                         [0x82, 0x83, 0x84, 0x85, 0x86, 0x87])
        self.assertEqual(c["gpu"]["speeds"],
                         [0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x90])
        self.assertEqual((core.N_TEMPS, core.N_SPEEDS), (6, 7))
        self.assertEqual(core.RPM_REGS, {"cpu": 0xCC, "gpu": 0xCA})
        self.assertEqual(core.RPM_NUMERATOR, 478000)
        self.assertEqual((core.CHARGE_MIN, core.CHARGE_MAX), (20, 100))

    def test_the_model_declares_the_features_the_gs66_has(self):
        for name in ("shift", "fan", "cooler_boost", "charge_limit",
                     "fan_curve", "rpm", "mux"):
            self.assertTrue(core.feature(name), name)
        # Nothing here has been proven about these on the GS66.
        for name in ("webcam", "webcam_block", "super_battery"):
            self.assertFalse(core.feature(name), name)

    def test_builtin_profiles_are_all_valid(self):
        self.assertEqual(sorted(core.BUILTIN_PROFILES),
                         ["balanced", "battery", "gaming", "silent"])
        for name, prof in core.BUILTIN_PROFILES.items():
            cleaned = core.clean_profile(prof)
            self.assertIn("shift", cleaned, name)

    def test_model_json_has_no_pseudo_comment_keys(self):
        # `// comment` keys are a JSON5 habit; json.load would accept the
        # string key and the loader would silently ignore it.  Prose belongs
        # in the README beside the file.
        path = os.path.join(REPO, "models", "gs66-stealth-10se", "model.json")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    self.assertFalse(str(k).startswith("//"),
                                     f"pseudo-comment key {k!r}")
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)

    def test_the_mux_addresses_live_in_core_not_in_a_model(self):
        # Every model that enables "mux" drives the same trio, so the
        # addresses must be shared rather than copied into each model file.
        for name in ("MUX_EC_ADDR", "MUX_TRIGGER_ADDR", "MUX_EC_MASK"):
            self.assertTrue(hasattr(core, name), name)
        path = os.path.join(REPO, "models", "gs66-stealth-10se", "model.json")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for name in ("MUX_EC_ADDR", "MUX_TRIGGER_ADDR", "\"0x2e\""):
            self.assertNotIn(name, text)


class TestModelSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="msi-models-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, model_id, data):
        d = os.path.join(self.tmp, model_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "model.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _minimal(self, model_id, **match):
        return {"schema": 1, "id": model_id, "name": model_id.title(),
                "match": match or {"product_name": ["*"]},
                "ec": {}, "features": {}}

    def test_a_missing_registry_yields_the_unsupported_model(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        model = core.select_model(hint="", root=empty)
        self.assertFalse(model.supported)
        self.assertEqual(model.id, core.UNKNOWN_ID)
        # and the unsupported model is deliberately conservative
        self.assertTrue(model.feature("shift"))
        self.assertFalse(model.feature("fan_curve"))
        self.assertFalse(model.feature("rpm"))
        self.assertFalse(model.feature("mux"))

    def test_an_explicit_unknown_id_is_a_loud_error(self):
        # A typo must not look like "a laptop the registry has never heard
        # of" — that would hide a broken model file behind a blank screen.
        with self.assertRaises(core.ModelError):
            core.select_model(hint="does-not-exist", root=self.tmp)

    def test_id_must_match_the_directory(self):
        self._write("right-name", self._minimal("wrong-name"))
        _models, errors = core.load_models(self.tmp)
        self.assertTrue(any("does not match its directory" in e
                            for e in errors), errors)

    def test_a_broken_file_is_reported_never_raised(self):
        good = os.path.join(self.tmp, "good")
        os.makedirs(good, exist_ok=True)
        with open(os.path.join(good, "model.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        models, errors = core.load_models(self.tmp)
        self.assertEqual(models, [])
        self.assertTrue(errors)
        # select_model on a registry with no usable model is still a state,
        # not a crash.
        self.assertFalse(core.select_model(hint="", root=self.tmp).supported)

    def test_the_strongest_match_wins(self):
        self._write("alpha", {"schema": 1, "id": "alpha", "name": "Alpha",
                              "match": {"product_name": ["GS66 Stealth 10SE"]},
                              "ec": {}, "features": {}})
        self._write("beta", {"schema": 1, "id": "beta", "name": "Beta",
                             "match": {"product_name": ["GS66 Stealth 10SE"],
                                       "board_name": ["MS-16V1"]},
                             "ec": {}, "features": {}})
        facts = {"product_name": "GS66 Stealth 10SE", "board_name": "MS-16V1"}
        self.assertGreater(core.match_score(
            core.load_model_file(os.path.join(self.tmp, "beta", "model.json")),
            facts),
            core.match_score(core.load_model_file(
                os.path.join(self.tmp, "alpha", "model.json")), facts))
        self.assertEqual(core.select_model(hint="beta", root=self.tmp).id,
                         "beta")

    def test_a_model_that_claims_a_feature_without_registers_is_rejected(self):
        with self.assertRaises(core.ModelError):
            core.Model({"schema": 1, "id": "x", "name": "X",
                        "match": {"product_name": ["*"]}, "ec": {},
                        "features": {"fan_curve": True}})
        with self.assertRaises(core.ModelError):
            core.Model({"schema": 1, "id": "x", "name": "X",
                        "match": {"product_name": ["*"]}, "ec": {},
                        "features": {"rpm": True}})

    def test_an_unknown_feature_flag_is_rejected(self):
        with self.assertRaises(core.ModelError):
            core.Model({"schema": 1, "id": "x", "name": "X",
                        "match": {"product_name": ["*"]}, "ec": {},
                        "features": {"teleport": True}})

    def test_curve_shape_must_be_consistent_between_fans(self):
        with self.assertRaises(core.ModelError):
            core.Model({
                "schema": 1, "id": "x", "name": "X",
                "match": {"product_name": ["*"]},
                "ec": {"curve": {
                    "cpu": {"temps": [0x6A, 0x6B], "speeds": [0, 1, 2]},
                    "gpu": {"temps": [0x82], "speeds": [0, 1]},
                }},
                "features": {"fan_curve": True}})

    def test_a_factory_named_builtin_profile_is_rejected(self):
        with self.assertRaises(core.ModelError):
            core.Model({"schema": 1, "id": "x", "name": "X",
                        "match": {"product_name": ["*"]}, "ec": {},
                        "profiles": {"factory": {"shift": "eco"}}})


class TestDetectAndDraft(unittest.TestCase):
    FACTS = {"sys_vendor": "Micro-Star International Co., Ltd.",
             "product_name": "GS66 Stealth 10SE",
             "product_version": "REV:1.0",
             "board_vendor": "Micro-Star International Co., Ltd.",
             "board_name": "MS-16V1", "board_version": "REV:1.0",
             "bios_version": "E16V1IMS.112",
             "ec_firmware": "16V1EMS1.118",
             "ec_firmware_date": "2021-02-04T16:46:37"}

    def test_model_id_from_name(self):
        self.assertEqual(core.model_id_from_name("GS66 Stealth 10SE"),
                         "gs66-stealth-10se")
        self.assertEqual(core.model_id_from_name("  MSI  ??  "),
                         "msi")
        self.assertEqual(core.model_id_from_name(""), "my-msi-laptop")

    def test_firmware_glob_drops_the_revision(self):
        # A pinned revision stops matching the moment the user updates the
        # firmware, which is the opposite of a registry entry's job.
        self.assertEqual(core.firmware_glob("16V1EMS1.118"), "16V1EMS1.*")
        self.assertEqual(core.firmware_glob("16V1EMS1"), "16V1EMS1")
        self.assertEqual(core.firmware_glob(""), "")

    def test_the_draft_is_valid_and_modest(self):
        draft = core.draft_model(self.FACTS)
        # It must survive the same validation a hand-written model does.
        model = core.Model(draft)
        self.assertEqual(model.id, "gs66-stealth-10se")
        # and it must claim nothing that needs a register nobody has found
        for name in ("fan_curve", "rpm", "mux"):
            self.assertFalse(model.feature(name), name)
        for name in ("shift", "fan", "cooler_boost", "charge_limit"):
            self.assertTrue(model.feature(name), name)
        # the match rules come from the hardware
        self.assertEqual(model.match["board_name"], ["MS-16V1"])
        self.assertEqual(model.match["ec_firmware"], ["16V1EMS1.*"])

    def test_write_draft_writes_the_file_and_refuses_to_clobber(self):
        tmp = tempfile.mkdtemp(prefix="msi-draft-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dest = os.path.join(tmp, "my-machine")
        path = core.write_draft(dest, self.FACTS)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["id"], "gs66-stealth-10se")
        with self.assertRaises(core.ModelError):
            core.write_draft(dest, self.FACTS)

    def test_render_detect_names_the_machine_and_the_ec_map(self):
        report = {
            "machine": self.FACTS,
            "driver": {"present": True, "debug": True,
                       "shift_modes": ["eco", "sport"], "fan_modes": ["auto"],
                       "attributes": ["shift_mode"], "missing_attributes": []},
            "registry": {"dir": "/x/models", "models": ["gs66-stealth-10se"],
                         "errors": []},
            "matched": {"id": "gs66-stealth-10se",
                        "name": "MSI GS66 Stealth 10SE", "score": 120},
            "candidates": [{"id": "gs66-stealth-10se",
                            "name": "MSI GS66 Stealth 10SE", "score": 120}],
            "ec_dump": {"readable": True, "bytes": {"00": "00", "2e": "40"}},
        }
        text = core.render_detect(report)
        self.assertIn("GS66 Stealth 10SE", text)
        self.assertIn("EC dump (256 registers", text)
        # the byte at 0x2e is rendered somewhere in the 0x20 row
        self.assertIn("40", text)

    def test_render_detect_says_so_when_nothing_matches(self):
        report = {
            "machine": {"product_name": "MSI Unknown"},
            "driver": {"present": True, "debug": False, "shift_modes": [],
                       "fan_modes": [], "attributes": [],
                       "missing_attributes": []},
            "registry": {"dir": "/x/models", "models": [], "errors": []},
            "matched": None, "candidates": [],
        }
        text = core.render_detect(report)
        self.assertIn("NOTHING", text)


class TestUnsupportedModelGating(unittest.TestCase):
    """With the unsupported model active, register-backed controls refuse."""

    def setUp(self):
        self._patch = mock.patch.object(core, "MODEL", core.Model.unknown())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_core_features_are_off(self):
        self.assertFalse(core.feature("fan_curve"))
        self.assertFalse(core.feature("rpm"))
        self.assertFalse(core.feature("mux"))
        # driver-provided controls stay available
        self.assertTrue(core.feature("shift"))
        self.assertTrue(core.feature("cooler_boost"))

    def test_the_daemon_refuses_a_curve_write(self):
        ec = MsiEc()
        from msi_controld import NotSupported
        with self.assertRaises(NotSupported):
            ec.set_curve("cpu", [1, 2, 3, 4, 5, 6], [0, 1, 2, 3, 4, 5, 6])
        with self.assertRaises(NotSupported):
            ec.curve("cpu")
        with self.assertRaises(NotSupported):
            ec.rpm("cpu")

    def test_the_refusal_explains_itself(self):
        from msi_controld import NotSupported
        with self.assertRaises(NotSupported) as ctx:
            raise NotSupported.feature("fan_curve")
        text = str(ctx.exception)
        self.assertIn("detect", text)
        self.assertIn("models/README.md", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)