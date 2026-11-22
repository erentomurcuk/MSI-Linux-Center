#!/usr/bin/env python3
"""msi-controld, the privileged half of msi-control.

Runs as root (systemd system service), owns every EC write, and speaks a
tiny one-request-per-connection JSON protocol on a Unix socket:

    {"cmd":"status"}                        -> full state incl. RPM + fan curve
    {"cmd":"set","key":"shift","value":"eco"}
    {"cmd":"curve","which":"cpu","temps":[…6],"speeds":[…7]}
    {"cmd":"profile","action":"apply","name":"gaming"}
    {"cmd":"raw","addr":242}                (read; gated by AllowRaw)

Design rules:
  * never trust a client: every value is validated by msi_control;
  * every EC write is followed by a read-back, because an EC that silently
    ignores a byte is indistinguishable from one that accepted it;
  * all writes are serialised under one lock (the EC is a shared resource
    the kernel's ACPI code also talks to);
  * no subprocesses, ever: only sysfs reads/writes at fixed paths.

Run: msi-controld [--socket PATH] [--sysfs PATH] [--config PATH] [--state PATH]
           [--no-restore]
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import grp
import json
import logging
import os
import signal
import socket
import socketserver
import struct
import sys
import threading
import time


def _add_lib_path() -> str:
    """Find msi_control.py, whether run from the source tree or installed.

    Installed layout puts the shared module beside msi_controld.py in
    /usr/local/lib/msi-control, so this normally just resolves to the
    script's own directory; the extra candidates keep it working if the
    unit file ever changes how it invokes us.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    candidates = [here,
                  "/usr/local/lib/msi-control",
                  "/usr/lib/msi-control"]
    for c in candidates:
        if os.path.isfile(os.path.join(c, "msi_control.py")):
            c = os.path.realpath(c)
            if c not in sys.path:
                sys.path.insert(0, c)
            return c
    sys.path.insert(0, here)
    return here


_add_lib_path()

import msi_control as core                                     # noqa: E402
from msi_control import ValidationError                        # noqa: E402

log = logging.getLogger("msi-controld")


class BackendError(RuntimeError):
    """A hardware/driver-level failure.  Message is safe to show users."""


class NotSupported(ValidationError):
    """The active model does not declare a feature this request needs.

    A `ValidationError` so the socket layer answers with the same
    `{"ok": false, "error": …}` shape every other refusal uses, but a distinct
    class so a caller (and a test) can tell *"you are not allowed to do that"*
    from *"this laptop cannot do that at all"*.
    """

    @classmethod
    def feature(cls, name: str, model=None) -> "NotSupported":
        model = model or core.active_model()
        if model.supported:
            detail = (f"the {model.name} ({model.id}) model definition does "
                      f"not enable {name!r}")
        else:
            detail = (f"no model definition matches this machine yet, so "
                      f"{name!r} is off; run `msi-control detect` and "
                      f"contribute one")
        return cls(f"{detail}.  See models/README.md.")


# fs ioctl numbers for the inode flags, used to lift the immutable attribute
# off an efivarfs file and put it back (there is no os.chflags on Linux, and
# the daemon spawns no subprocesses).  Values are from <linux/fs.h>.
FS_IOC_GETFLAGS = 0x80086601
FS_IOC_SETFLAGS = 0x40086602
FS_IMMUTABLE_FL = 0x00000010


# --------------------------------------------------------------------------
# Direct EC backend: sysfs of the out-of-tree msi-ec driver (debug=1)
# --------------------------------------------------------------------------

class MsiEc:
    def __init__(self, sysfs_root: str | None = None):
        if sysfs_root:
            os.environ["MSI_SYSFS"] = sysfs_root   # keeps core.* in sync
        self.lock = threading.Lock()
        self._bat = None
        # How long to wait for the three-mode firmware's acknowledge bit once
        # a switch request is armed, and how often to poll it.  Attribute
        # values (not module constants) so the tests can shorten the wait.
        self.mux_ack_timeout = 5.0
        self.mux_ack_interval = 0.1

    # -- sysfs helpers ----------------------------------------------------

    @property
    def root(self) -> str:
        return core.msi_ec_dir()

    def _p(self, *rel) -> str:
        return os.path.join(self.root, *rel)

    def _debug(self, *rel) -> str:
        return self._p("debug", *rel)

    def available(self, name: str) -> list:
        text = core._read_opt(self._p(f"available_{name}_modes")) or ""
        return [ln for ln in text.splitlines() if ln.strip()]

    def read(self, *rel) -> str:
        try:
            return core._read(self._p(*rel))
        except OSError as e:
            raise BackendError(f"cannot read {'/'.join(rel)}: {e.strerror}") from e

    def debug_present(self) -> bool:
        """Is the driver's debug interface mounted?  (needs debug=1)"""
        return os.path.isdir(self._p("debug"))

    def read_opt(self, *rel):
        return core._read_opt(self._p(*rel))

    def write(self, value: str, *rel) -> None:
        path = self._p(*rel)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(value if value.endswith("\n") else value + "\n")
        except OSError as e:
            raise BackendError(
                f"cannot write {'/'.join(rel)}: {e.strerror or e}") from e

    # -- raw EC bytes (via the driver's debug interface) -------------------

    def _check_ec_debug(self) -> None:
        if not os.path.isdir(self._p("debug")):
            raise BackendError(
                "EC debug interface unavailable; load msi-ec with debug=1 "
                "(install.sh sets this up in /etc/modprobe.d/msi-ec.conf)")

    def _xfer_read(self, addr: int) -> int:
        """One sysfs read of EC[addr].  Split out so tests can fake the bus."""
        try:
            with open(self._debug("ec_get"), "w", encoding="utf-8") as fh:
                fh.write(f"{addr:02x}\n")
            return int(core._read(self._debug("ec_get")), 16)
        except OSError as e:
            raise BackendError(f"EC read failed at {addr:#04x}: {e}") from e
        except ValueError as e:
            raise BackendError(f"EC read returned garbage at {addr:#04x}") from e

    def _xfer_write(self, addr: int, val: int) -> None:
        """One sysfs write of EC[addr]=val.  No verification here."""
        try:
            with open(self._debug("ec_set"), "w", encoding="utf-8") as fh:
                fh.write(f"{addr:02x}={val:02x}\n")
        except OSError as e:
            raise BackendError(f"EC write failed at {addr:#04x}: {e}") from e

    def ec_read(self, addr: int) -> int:
        """Read one EC byte.  Needs msi-ec loaded with debug=1."""
        self._check_ec_debug()
        if not 0 <= addr <= 0xFF:
            raise ValidationError("EC address out of range 0x00..0xff")
        return self._xfer_read(addr)

    def ec_write(self, addr: int, val: int, verify: bool = True) -> None:
        self._check_ec_debug()
        if not 0 <= addr <= 0xFF or not 0 <= val <= 0xFF:
            raise ValidationError("EC address/value out of range 0x00..0xff")
        self._xfer_write(addr, val)
        if verify:
            got = self.ec_read(addr)
            if got != val:
                raise BackendError(
                    f"EC rejected write {addr:#04x}={val:02x} (read back {got:02x})")

    # -- battery -----------------------------------------------------------

    def battery_dir(self) -> str:
        bat = core.find_battery()
        if not bat:
            raise BackendError("no battery found in power_supply")
        return bat

    def charge_end(self) -> int | None:
        bat = core.find_battery()
        if not bat:
            return None
        return core._read_int(os.path.join(bat, "charge_control_end_threshold"))

    def set_charge_end(self, pct: int) -> None:
        pct = core.check_charge_end(pct)
        bat = self.battery_dir()
        path = os.path.join(bat, "charge_control_end_threshold")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"{pct}\n")
        except OSError as e:
            raise BackendError(f"cannot set charge threshold: {e}") from e
        # read back
        got = core._read_int(path)
        if got is not None and got != pct:
            raise BackendError(
                f"charge threshold not accepted (wanted {pct}, read {got})")

    # -- fans ---------------------------------------------------------------

    def curve(self, which: str) -> dict:
        core.check_choice(which, ("cpu", "gpu"), "fan curve")
        if not core.feature("fan_curve"):
            raise NotSupported.feature("fan_curve")
        regs = core.CURVE_REGS[which]
        with self.lock:
            temps = [self.ec_read(a) for a in regs["temps"]]
            speeds = [self.ec_read(a) for a in regs["speeds"]]
        return {"temps": temps, "speeds": speeds}

    def set_curve(self, which: str, temps, speeds) -> dict:
        core.check_choice(which, ("cpu", "gpu"), "fan curve")
        if not core.feature("fan_curve"):
            raise NotSupported.feature("fan_curve")
        temps, speeds = core.check_curve(temps, speeds)
        regs = core.CURVE_REGS[which]
        with self.lock:
            for addr, val in zip(regs["temps"], temps):
                self.ec_write(addr, val)
            for addr, val in zip(regs["speeds"], speeds):
                self.ec_write(addr, val)
        return {"temps": temps, "speeds": speeds}

    def rpm(self, which: str) -> int | None:
        core.check_choice(which, ("cpu", "gpu"), "fan")
        if not core.feature("rpm"):
            raise NotSupported.feature("rpm")
        addr = core.RPM_REGS[which]
        try:
            hi, lo = self.ec_read(addr), self.ec_read(addr + 1)
        except BackendError:
            return None
        period = (hi << 8) | lo
        if period == 0:
            return 0
        return core.RPM_NUMERATOR // period

    # -- graphics mode (MUX) ------------------------------------------------
    #
    # Everything here writes bytes the driver's G1_3 config does not describe
    # (see the comment on MUX_EC_ADDR in msi_control).  The EC half is a
    # read-modify-write of exactly one bit, so the neighbouring bits (0x2e
    # bit 1 is the webcam) survive untouched, and every write goes through
    # ec_write, which reads the byte back and refuses to report success unless
    # it stuck.

    def mux_ec_bit(self) -> int | None:
        """Raw EC 0x2e, or None when the debug interface is unavailable."""
        try:
            return self.ec_read(core.MUX_EC_ADDR)
        except (BackendError, ValidationError):
            return None

    def mux_var_raw(self) -> bytes | None:
        try:
            with open(core.mux_var_path(), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def mux_var_write(self, mode: str) -> bool:
        """Request `mode` in the legacy byte-1 slot of the variable.

        True if the bytes moved.  Only valid on firmware that does NOT carry
        the three-mode marker (the Alpha 17's 17KK family; see the MUX block
        in msi_control for the provenance, and why this byte is never read
        back as a mode).  Three-mode firmware must go through
        `_mux_apply_three_mode`, which writes byte 5 and needs the EC ack.
        """
        raw = self.mux_var_raw()
        if raw is None:
            raise BackendError(
                f"{core.mux_var_path()} is not present; this firmware has no "
                f"{core.MUX_VAR_NAME} variable, so the graphics mode cannot be "
                "requested")
        if core.mux_var_three_mode(raw):
            raise BackendError(
                "this firmware speaks the three-mode protocol; the request "
                "belongs in data byte 5, not byte 1")
        attrs, data = core.parse_mux_var(raw)
        if len(data) <= core.MUX_VAR_LEGACY_BYTE:
            raise BackendError(
                f"{core.MUX_VAR_NAME} is too short to hold a mode "
                f"({len(data)} data bytes)")
        want = 0x01 if mode == core.MUX_DISCRETE else 0x00
        if data[core.MUX_VAR_LEGACY_BYTE] == want:
            return False
        new = bytearray(data)
        new[core.MUX_VAR_LEGACY_BYTE] = want
        self._efivar_write(core.mux_var_path(), attrs.to_bytes(4, "little")
                           + bytes(new))
        return True

    @staticmethod
    def _inode_flags(fd: int) -> int | None:
        """Inode flags of an open file, or None if the fs has no such notion."""
        try:
            return struct.unpack(
                "I", fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack("I", 0)))[0]
        except OSError as e:
            # A filesystem that cannot report inode flags also cannot carry
            # the immutable attribute, so there is nothing to clear and the
            # write below is safe to attempt.  Anything else is a real error.
            if e.errno in (errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL):
                return None
            raise

    @classmethod
    def _efivar_write(cls, path: str, payload: bytes) -> None:
        """Replace one efivarfs file in place.

        Two constraints, both of which bite silently if ignored: efivarfs
        wants the 4 attribute bytes plus the payload delivered in a *single*
        write() (a buffered or split write is refused), and the file is
        normally marked immutable, so the flag has to come off first and go
        back on afterwards.  Both are plain ioctls and a raw os.write: no
        `chattr`, no subprocess, which is the daemon's standing rule.

        The flag goes back on in a `finally`, so a failed write cannot leave
        the variable permanently mutable.
        """
        fd = os.open(path, os.O_RDWR)
        written = 0
        try:
            flags = cls._inode_flags(fd)
            immutable = bool(flags is not None and flags & FS_IMMUTABLE_FL)
            if immutable:
                fcntl.ioctl(fd, FS_IOC_SETFLAGS,
                            struct.pack("I", flags & ~FS_IMMUTABLE_FL))
            try:
                written = os.write(fd, payload)
            finally:
                if immutable:
                    fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("I", flags))
        except OSError as e:
            raise BackendError(
                f"cannot write {path}: {e.strerror or e}") from e
        finally:
            os.close(fd)
        if written != len(payload):
            raise BackendError(
                f"short write to {path}: {written} of {len(payload)} bytes")

    def mux_apply(self, mode: str) -> None:
        """Write the switch, in the protocol this firmware actually speaks.

        Three-mode firmware (the marker set in data byte 5) gets the MSI
        Center handshake: the request into byte 5, then the EC trigger, then
        a wait for the firmware's own acknowledge bit, which is raised by
        its SMI handler once the request is *stored*.  Everything else gets
        the legacy trio (variable byte 1, trigger, state bit), which is as
        far as the 17KK provenance reaches.  Neither path reboots and
        neither is reversible without one; the caller owns the confirmation.
        """
        raw = self.mux_var_raw()
        if raw is not None and core.mux_var_three_mode(raw):
            self._mux_apply_three_mode(raw, mode)
            return
        self.mux_var_write(mode)
        self._mux_write_ec(core.MUX_EC_MASK if mode == core.MUX_DISCRETE else 0)

    def _mux_apply_three_mode(self, raw: bytes, mode: str) -> None:
        """The MSI Center write: byte 5, trigger, acknowledge."""
        attrs, data = core.parse_mux_var(raw)
        if len(data) <= core.MUX_VAR_GPU_BYTE:
            raise BackendError(
                f"{core.MUX_VAR_NAME} is too short to hold the three-mode "
                f"byte ({len(data)} data bytes)")
        gpu = data[core.MUX_VAR_GPU_BYTE]
        caps = core.mux_caps_from_var(raw)
        if mode == core.MUX_DISCRETE and caps["discrete"] is False:
            raise BackendError(
                "the variable says this firmware does not offer discrete "
                "mode (its capability bit is clear), so the switch would "
                "ask for something the machine cannot do")
        code = 0x01 if mode == core.MUX_DISCRETE else 0x00
        new = bytearray(data)
        new[core.MUX_VAR_GPU_BYTE] = \
            (gpu & ~core.MUX_VAR_REQ_MASK) | (code & core.MUX_VAR_REQ_MASK)
        self._efivar_write(core.mux_var_path(),
                           attrs.to_bytes(4, "little") + bytes(new))
        # A stored request is only a request if the firmware kept it; efivarfs
        # would happily report our own write back to us on firmware that
        # rewrites the variable, so this check is about bytes, not trust.
        got = self.mux_var_raw()
        if got is None or got[core.MUX_VAR_SKIP + core.MUX_VAR_GPU_BYTE] \
                != new[core.MUX_VAR_GPU_BYTE]:
            # Put the exact previous image back; a half-staged request is the
            # worst state to leave behind, because nothing on screen can
            # distinguish it from a made-up-our-mind one.
            self._efivar_write(core.mux_var_path(), raw)
            raise BackendError(
                "the firmware did not keep the graphics request in "
                f"{core.MUX_VAR_NAME}; the variable was restored")
        self._mux_arm_trigger()
        if not self._mux_wait_ack():
            # The request is staged but the firmware never stored it.  Say
            # so loudly: reporting success here is how a switch looks done
            # while nothing was committed.
            raise BackendError(
                "the firmware did not acknowledge the graphics request "
                f"within {self.mux_ack_timeout:.0f}s (EC "
                f"{core.MUX_TRIGGER_ADDR:#04x} bit 1 stayed low).  The "
                "request is still in the variable; revert it or leave it "
                "for the next POST")

    def _mux_arm_trigger(self) -> None:
        """Ask the EC to act on the stored request (its _Q90 / SMI path).

        Read-modify-write of exactly the two trigger bits: arm the request
        and clear a stale acknowledge, so the wait below can only see an ack
        this request produced.  The read-back verifies our own bits only:
        the acknowledge bit belongs to the firmware and a fast SMI handler
        is allowed to raise it between our write and our read, which the
        generic verify-every-byte rule would misread as a rejection.
        """
        with self.lock:
            trig = self.ec_read(core.MUX_TRIGGER_ADDR)
            armed = ((trig & ~core.MUX_TRIGGER_MASK) | core.MUX_TRIGGER_ARM)
            if armed != trig:
                self.ec_write(core.MUX_TRIGGER_ADDR, armed, verify=False)
            got = self.ec_read(core.MUX_TRIGGER_ADDR)
            if got & core.MUX_TRIGGER_ARM != core.MUX_TRIGGER_ARM:
                raise BackendError(
                    f"EC rejected the switch request at "
                    f"{core.MUX_TRIGGER_ADDR:#04x} (request bit stayed low, "
                    f"read back {got:02x})")

    def _mux_wait_ack(self) -> bool:
        """Poll the EC until the firmware acknowledges, or the deadline.

        Each read takes the EC lock for its own duration only, so the poll
        never holds the lock across a sleep and starves other writers.
        """
        deadline = time.monotonic() + self.mux_ack_timeout
        while True:
            try:
                with self.lock:
                    cur = self.ec_read(core.MUX_TRIGGER_ADDR)
            except (BackendError, ValidationError):
                return False
            if cur & core.MUX_TRIGGER_ACK:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.mux_ack_interval)

    def _mux_write_ec(self, want_bit: int) -> None:
        """Arm the trigger, then set 0x2e bit 6 to `want_bit`.

        Read-modify-write, so the bits this program has no business in
        (0x2e bit 1 is the webcam) are carried through untouched.
        """
        self._mux_arm_trigger()
        with self.lock:
            cur = self.ec_read(core.MUX_EC_ADDR)
            new = (cur & ~core.MUX_EC_MASK) | (want_bit & core.MUX_EC_MASK)
            if new != cur:
                self.ec_write(core.MUX_EC_ADDR, new)

    def mux_restore(self, ec_bit: int | None, raw: bytes | None) -> None:
        """Put a backed-up UEFI image and EC bit back.

        The backed-up image says which protocol it came from: a three-mode
        variable is put back with the same handshake it was taken from
        (image, trigger, ack) and the 0x2e state bit is deliberately left
        alone there, because the firmware owns it; a legacy variable gets
        the image plus the bit, which is the state it was in."""
        three_mode = core.mux_var_three_mode(raw)
        if three_mode:
            if raw is not None:
                self.mux_var_restore(raw)
                self._mux_arm_trigger()
                if not self._mux_wait_ack():
                    raise BackendError(
                        "the image was restored but the firmware did not "
                        "acknowledge it, so the restore may not be stored; "
                        "check the daemon log and the variable before "
                        "rebooting")
            return
        if raw is not None:
            self.mux_var_restore(raw)
        if ec_bit is not None:
            self._mux_write_ec(ec_bit)

    def mux_var_restore(self, raw: bytes) -> None:
        """Put a previously saved efivarfs image back, byte for byte."""
        self._efivar_write(core.mux_var_path(), raw)

    # -- display overdrive ---------------------------------------------------

    def overdrive(self) -> int | None:
        """The overdrive bit of the active model, or None when unsupported.

        Only the bit named by the model file is read; the rest of the byte
        (webcam, MUX state) belongs to other features and is not decoded
        here.
        """
        regs = core.OVERDRIVE_REGS
        if not regs:
            return None
        with self.lock:
            val = self.ec_read(regs["address"])
        return (val >> regs["bit"]) & 1

    def set_overdrive(self, on: bool) -> dict:
        """Toggle the model's overdrive bit with a one-bit read-modify-write.

        The GS66's 0x2e carries the webcam at bit 1 and the MUX state at bit
        6, which is exactly why this is a single-bit RMW and nothing wider.
        Writable override values from the model file (off/on, for firmwares
        whose EC treats the whole byte as owned) win over bit twiddling.
        """
        regs = core.OVERDRIVE_REGS
        if not regs:
            raise NotSupported("this model has no proven display overdrive "
                              "register")
        with self.lock:
            cur = self.ec_read(regs["address"])
            want = bool(on)
            if regs.get("on") is not None and want:
                val = regs["on"]
            elif regs.get("off") is not None and not want:
                val = regs["off"]
            else:
                val = (cur | (1 << regs["bit"])) if want \
                    else (cur & ~(1 << regs["bit"]))
            if val != cur:
                self.ec_write(regs["address"], val)   # read-back inside
        return {"address": regs["address"], "bit": regs["bit"],
                "was": (cur >> regs["bit"]) & 1, "now": 1 if want else 0,
                "same": val == cur}

    # -- modes ---------------------------------------------------------------

    # -- settings: read one key the same way `set` writes it ---------------

    def get(self, key: str):
        """Current value of a setting, in the same shape `set` accepts.

        Read back from the hardware, never from what a client asked for -
        this is what makes "shift comfort -> sport" an honest sentence.
        """
        if key == "charge_end":
            return self.charge_end()
        if key == "shift":
            return self.read_opt("shift_mode")
        if key == "fan":
            return self.read_opt("fan_mode")
        if key in ("cooler_boost", "webcam", "webcam_block", "super_battery",
                   "fn_key", "win_key"):
            return self.read_opt(key)
        raise ValidationError(f"unknown setting {key!r}")

    def set(self, key: str, value) -> dict:
        """Write one setting; return what it was and what it is now."""
        with self.lock:
            was = self.get(key)
            if key == "shift":
                choices = self.available("shift")
                value = core.check_choice(str(value), choices, "shift mode")
                self.write(value, "shift_mode")
                got = self.read_opt("shift_mode")
            elif key == "fan":
                choices = self.available("fan")
                value = core.check_choice(str(value), choices, "fan mode")
                self.write(value, "fan_mode")
                got = self.read_opt("fan_mode")
            elif key in ("cooler_boost", "webcam", "webcam_block",
                         "super_battery"):
                if isinstance(value, bool):
                    value = "on" if value else "off"
                value = core.check_choice(str(value).lower(),
                                          ("on", "off"), key)
                self.write(value, key)
                got = self.read_opt(key)
            elif key in ("fn_key", "win_key"):
                value = core.check_choice(str(value).lower(),
                                          ("left", "right"), key)
                self.write(value, key)
                got = self.read_opt(key)
            elif key == "charge_end":
                self.set_charge_end(value)
                now = self.charge_end()
                return {"key": key, "was": was, "now": now,
                        "changed": was != now}
            else:
                raise ValidationError(f"unknown setting {key!r}")
        if got is not None and got.strip() != str(value):
            raise BackendError(
                f"{key} not accepted (wanted {value!r}, read {got.strip()!r})")
        now = got.strip() if got else got
        return {"key": key, "was": was, "now": now, "changed": was != now}


# --------------------------------------------------------------------------
# CPU frequency backend: cpufreq sysfs (no EC involvement, but still
# root-only to write, so the daemon owns it like every other write)
# --------------------------------------------------------------------------

class CpuFreq:
    """The `scaling_max_freq` of every policy, as one knob.

    Each policy file read back after the write, because that is the
    house rule for every hardware write.  One footnote from real
    hardware: Intel cpufreq (intel_pstate + HWP on an i7-10875H) applies
    a new limit *asynchronously*: `write(2)` returns before the sysfs
    attribute reflects the value, so an immediate read-back sees the
    previous cap and reports a refusal for a write that did stick.  The
    read-back is therefore a short poll with a deadline, not one shot;
    a genuine refusal still surfaces, it just has to outlast the wait.
    """

    # Generous next to the observed few-millisecond application time, so a
    # busy machine (16 policies hammered during a load spike) is not
    # declared broken; 80 ms per policy, 1.6 s for the whole set worst-case.
    APPLY_WAIT_S = 0.08
    APPLY_POLL_S = 0.002

    def __init__(self):
        self.lock = threading.Lock()

    def _path(self, policy: str, name: str) -> str:
        return os.path.join(core.cpufreq_dir(), policy, name)

    def facts(self) -> dict:
        with self.lock:
            return core.cpufreq_facts()

    @staticmethod
    def _bound(facts: dict, key: str) -> int:
        v = facts.get(key)
        if not v:
            raise BackendError("cpufreq did not report CPU frequency bounds; "
                               f"({key} missing)")
        return v

    def set_all_max(self, khz: int) -> dict:
        """One max frequency for every core.  khz in kilohertz."""
        facts = self.facts()
        if not facts.get("present"):
            raise BackendError("cpufreq is unavailable on this machine")
        hi = self._bound(facts, "cpuinfo_max_khz")
        lo = self._bound(facts, "cpuinfo_min_khz")
        wanted = core.check_freq_khz(khz, lo, hi, "max frequency")
        pols = core.cpufreq_policies()
        with self.lock:
            stops = []
            for pol in pols:
                cur = core.read_khz(self._path(pol, "scaling_max_freq"))
                # min must never overtake max: lift the floor when the
                # requested max is below the current floor.
                floor = core.read_khz(self._path(pol, "scaling_min_freq"))
                if floor is not None and floor > wanted:
                    with open(self._path(pol, "scaling_min_freq"), "w",
                              encoding="utf-8") as fh:
                        fh.write(f"{wanted}\n")
                with open(self._path(pol, "scaling_max_freq"), "w",
                          encoding="utf-8") as fh:
                    fh.write(f"{wanted}\n")
                # Poll rather than read once: the kernel applies the new
                # limit after write(2) returns (see the class docstring).
                got = None
                deadline = time.monotonic() + self.APPLY_WAIT_S
                while True:
                    got = core.read_khz(self._path(pol, "scaling_max_freq"))
                    if got == wanted or time.monotonic() >= deadline:
                        break
                    time.sleep(self.APPLY_POLL_S)
                if got != wanted:
                    stops.append(f"core {pol}: read back "
                                 f"{core.khz_to_ghz(got)} GHz")
            if stops:
                raise BackendError("; ".join(stops))
            now = core.cpufreq_facts()
        return {"max_khz": wanted, "policies": len(pols),
                "max_khz_applied": now.get("max_khz"),
                "summary": (f"max frequency {core.khz_to_ghz(wanted)} GHz "
                            f"on {len(pols)} cores")}


# --------------------------------------------------------------------------
# Undervolt backend: the OC mailbox in /dev/cpu/*/msr
# --------------------------------------------------------------------------

class Undervolt:
    """ MSR 0x150 on every logical CPU: the ThrottleStop/undervolt protocol.

    The mailbox is per-thread state, so every command goes to every MSR
    device; georgewhewell/undervolt does the same and it reads back
    identically from any thread of a core.  A write that does not read
    back is the *only* signal a machine's BIOS has not unlocked
    undervolting, so it is reported as such, not as a silent success -
    the house read-back rule is what makes the "locked" case visible at
    all.
    """

    def __init__(self):
        self.lock = threading.Lock()

    def _msr_files(self) -> list:
        root = core.dev_cpu_dir()
        out = []
        try:
            names = sorted(os.listdir(root),
                           key=lambda n: int(n) if n.isdigit() else 1 << 30)
        except OSError:
            return []
        for n in names:
            path = os.path.join(root, n, "msr")
            # exists, not isfile: on the real kernel /dev/cpu/*/msr is a
            # *character device*, and os.path.isfile() is False for one,
            # which made every real machine report "no MSR devices" while
            # the offline fixtures (regular files) kept passing.  exists()
            # accepts both shapes.
            if os.path.exists(path):
                out.append(path)
        return out

    def available(self) -> bool:
        return bool(self._msr_files())

    @staticmethod
    def _wr(path: str, value: int) -> None:
        # A 64-bit little-endian write; pread/pwrite-style seeks keep the
        # per-thread offset independent of what anything else did to the fd.
        try:
            fd = os.open(path, os.O_WRONLY)
        except OSError as e:
            # A node that cannot be opened (permissions, or ENXIO once the
            # driver is gone) is a backend fact, not an internal error:
            # status degrades the undervolt block to "unavailable" and the
            # CLI can name it.  Without this, one unopenable node would
            # sink a whole machine's status payload.
            raise BackendError(
                f"cannot open {path}: {e.strerror or e}") from e
        try:
            os.lseek(fd, core.UV_MSR, os.SEEK_SET)
            os.write(fd, value.to_bytes(8, "little"))
        finally:
            os.close(fd)

    @staticmethod
    def _rd(path: str) -> int:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as e:
            raise BackendError(
                f"cannot open {path}: {e.strerror or e}") from e
        try:
            os.lseek(fd, core.UV_MSR, os.SEEK_SET)
            return int.from_bytes(os.read(fd, 8), "little")
        finally:
            os.close(fd)

    def read_planes(self) -> dict | None:
        """Current offsets in mV per plane; None when there is no MSR."""
        if not self._msr_files():
            return None
        with self.lock:
            out = {}
            for plane, _idx in core.UV_PLANES:
                got = self.read_plane(plane)
                out[plane] = got[0] if got else None
            return out

    def apply(self, values: dict, fill_missing: bool = True) -> dict:
        """Write offsets to the voltage planes, and read every one back.

        All five planes by default, zeros included: the mailbox is
        command-plus-data per plane, so "not mentioned" cannot mean "left
        alone" here the way it does for nice named settings, and writing
        0 is also how an earlier offset is cleared.  fill_missing=False is
        the profile shape: only the planes the caller named are touched, so
        a profile that caps the core voltage never silently clears a cache
        offset some other profile set.
        """
        clean = core.check_uv_values(values, fill=fill_missing)
        files = self._msr_files()
        if not files:
            raise BackendError("no MSR devices under /dev/cpu/*/msr; "
                               "load the msr module or use a kernel that "
                               "creates them")
        rejected = []
        with self.lock:
            for plane, idx in core.UV_PLANES:
                if plane not in clean:
                    continue
                mv = clean[plane]
                data = core.uv_encode(mv)
                for path in files:
                    self._wr(path,
                             core.UV_CMD_WRITE | (idx << core.UV_PLANE_SHIFT))
                    self._wr(path, data)
                got = self.read_plane(plane)
                if got is None or not got or got[0] != mv:
                    read = f"{got[0]} mV" if got else "nothing"
                    rejected.append(f"{plane}: asked {mv} mV, read back "
                                    f"{read}")
        if rejected:
            raise BackendError(
                "the CPU refused the voltage offset(s) - most firmwares "
                "ship with undervolting locked: enable it in the BIOS "
                "(Advanced / OC menu: Overclocking Lock, CFG Lock and "
                "Undervolt Protection off).  Refused: "
                + "; ".join(rejected))
        return clean

    def read_plane(self, plane: str) -> list | None:
        """Offsets of `plane` as read from every thread, shallow-sorted.

        All threads carry the same offset on any healthy write, so the
        list is normally one value long; the caller sees the shallowest
        (closest to 0) when they differ so the UI can never report a
        deeper undervolt than the machine is really running.  None means
        no MSR devices; [] means the plane read back nothing usable.
        """
        files = self._msr_files()
        if not files:
            return None
        idx = dict(core.UV_PLANES)[plane]
        vals = []
        for path in files:
            self._wr(path, core.UV_CMD_READ | (idx << core.UV_PLANE_SHIFT))
            vals.append(core.uv_decode(self._rd(path)))
        return sorted(vals, reverse=True)


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

class Profiles:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, core.check_profile_name(name) + ".json")

    def list(self) -> dict:
        files = {}
        for fn in sorted(os.listdir(self.dir)):
            if fn.endswith(".json"):
                files[fn[:-5]] = fn
        return {
            "builtin": sorted(core.BUILTIN_PROFILES),
            "saved": sorted(files),
        }

    def load(self, name: str) -> dict:
        name = core.check_profile_name(name)
        if name in core.BUILTIN_PROFILES:
            prof = dict(core.BUILTIN_PROFILES[name])
            prof["name"] = name
            prof["builtin"] = True
            return prof
        try:
            with open(self._path(name), "r", encoding="utf-8") as fh:
                prof = json.load(fh)
        except FileNotFoundError:
            raise ValidationError(f"profile {name!r} does not exist") from None
        except (OSError, json.JSONDecodeError) as e:
            raise BackendError(f"cannot read profile {name!r}: {e}") from e
        if not isinstance(prof, dict):
            raise BackendError(f"profile {name!r} is corrupt")
        prof["name"] = name
        prof["builtin"] = False
        return prof

    def save(self, name: str, data: dict) -> None:
        core.check_profile_name(name)
        prof = core.clean_profile(data)
        prof["name"] = name
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(prof, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self._path(name))

    def delete(self, name: str) -> None:
        name = core.check_profile_name(name)
        if name in core.BUILTIN_PROFILES:
            raise ValidationError("built-in profiles cannot be deleted")
        try:
            os.unlink(self._path(name))
        except FileNotFoundError:
            raise ValidationError(f"profile {name!r} does not exist") from None


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

class Service:
    def __init__(self, args):
        self.args = args
        if args.sysfs:
            os.environ["MSI_SYSFS"] = args.sysfs
        if args.config:
            os.environ["MSI_CONFIG"] = args.config
        if args.state:
            os.environ["MSI_STATE"] = args.state
        self.cfg = core.load_config()
        self.ec = MsiEc()
        self.freq = CpuFreq()
        self.uv = Undervolt()
        self.profiles = Profiles(os.path.join(core.state_dir(), "profiles"))
        os.makedirs(core.state_dir(), exist_ok=True)
        self._igpu: bool | None = None      # one look per daemon lifetime

    # -- state --------------------------------------------------------------

    def status(self) -> dict:
        st = core.local_status(mux=False)
        st["partial"] = False
        debug = self.ec.debug_present()
        st["driver"]["debug"] = debug
        if debug and core.feature("rpm"):
            for which in ("cpu", "gpu"):
                try:
                    st[which]["rpm"] = self.ec.rpm(which)
                except BackendError:
                    st[which]["rpm"] = None
        if debug and core.feature("fan_curve"):
            st["curve"] = {}
            for which in ("cpu", "gpu"):
                try:
                    st["curve"][which] = self.ec.curve(which)
                except BackendError:
                    st["curve"][which] = None
        st["profiles"] = self.profiles.list()
        st["graphics"] = self.graphics()
        st["freq"] = self.freq.facts()
        st["overdrive"] = self.overdrive_status()
        try:
            st["undervolt"] = {"planes": self.uv.read_planes(),
                               "msr_present": self.uv.available()}
        except BackendError:
            st["undervolt"] = {"planes": None, "msr_present": False}
        st["config"] = {
            "RestoreLast": bool(self.cfg.get("RestoreLast")),
            "DefaultProfile": self.cfg.get("DefaultProfile"),
            "AllowRaw": bool(self.cfg.get("AllowRaw")),
            "AllowMuxSwitch": bool(self.cfg.get("AllowMuxSwitch")),
            "AllowUndervolt": bool(self.cfg.get("AllowUndervolt")),
            "SocketGroup": str(self.cfg.get("SocketGroup", "wheel")),
            "ReapplyOnResume": bool(self.cfg.get("ReapplyOnResume")),
        }
        return st

    def overdrive_status(self) -> dict:
        """What the panel/overdrive card needs: support first, then value."""
        out = {"supported": bool(core.OVERDRIVE_REGS), "on": None}
        if out["supported"]:
            try:
                out["on"] = bool(self.ec.overdrive())
            except (BackendError, ValidationError):
                out["on"] = None
        return out

    def snapshot(self) -> dict:
        """What we would restore at next boot: only keys that exist here.

        The graphics mode is deliberately absent.  It is not a per-session
        setting: re-requesting it at every boot would fight the firmware for a
        value that only changes on an explicit, reboot-backed switch, and a
        RestoreLast loop that flipped the MUX on a plain restart is the last
        thing this program should do.
        """
        st = self.status()
        snap = {}
        if st["shift"]["current"]:
            snap["shift"] = st["shift"]["current"]
        if st["fan"]["current"]:
            snap["fan"] = st["fan"]["current"]
        if st["cooler_boost"] in ("on", "off"):
            snap["cooler_boost"] = st["cooler_boost"] == "on"
        if st["battery"] and st["battery"]["charge_end"] is not None:
            snap["charge_end"] = st["battery"]["charge_end"]
        if st.get("curve"):
            snap["curve"] = st["curve"]
        return snap

    def save_last(self) -> None:
        try:
            snap = self.snapshot()
            tmp = os.path.join(core.state_dir(), "last.json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, indent=2, sort_keys=True)
            os.replace(tmp, os.path.join(core.state_dir(), "last.json"))
        except Exception as e:                       # noqa: BLE001
            log.warning("could not save last state: %s", e)

    # -- graphics mode (MUX) -------------------------------------------------

    def graphics(self) -> dict:
        """The graphics-mode picture, from all three independent sources.

        `effective` is what the running machine is doing (an Intel display
        controller exists or it does not); `requested` is what the firmware
        will do at the next POST (the UEFI variable, falling back to the EC
        state bit when the variable is absent).  The two differ only in the
        window between a switch and the reboot that applies it, which is
        exactly what `reboot_required` reports, and it is deliberately not
        derived from "we just wrote something".
        """
        if self._igpu is None:
            # The iGPU cannot appear or vanish without a reboot, so reading it
            # once per daemon lifetime is not a cache; it is the truth, and
            # it keeps a 30-device PCI walk off the 1.5 s poll.
            self._igpu = core.igpu_present()
        igpu = self._igpu
        ec_bit = self.ec.mux_ec_bit()
        raw = self.ec.mux_var_raw()
        ec_mode = core.mux_from_ec_byte(ec_bit)
        var_mode = core.mux_from_var(raw)
        effective = None
        if igpu is not None:
            effective = core.MUX_HYBRID if igpu else core.MUX_DISCRETE
        # The request comes from whichever source can name one: the variable
        # only when the firmware speaks three-mode, else the EC bit, else (and
        # only then) what the running machine is doing.  On a firmware where
        # the variable names nothing, the honest "next boot at all" is the
        # machine's own current state, never a phantom pending switch.
        requested = (var_mode if var_mode is not None
                     else ec_mode if ec_mode is not None else effective)
        return {
            "supported": ec_bit is not None or raw is not None,
            "enabled": bool(self.cfg.get("AllowMuxSwitch")),
            "effective": effective,
            "requested": requested,
            "reboot_required": bool(effective and requested
                                    and effective != requested),
            "ec_bit": ec_bit,
            "ec_mode": ec_mode,
            "var_mode": var_mode,
            "var_three_mode": core.mux_var_three_mode(raw),
            "var_applied": core.mux_applied_from_var(raw),
            "caps": core.mux_caps_from_var(raw),
            "var_present": raw is not None,
            "var_path": core.mux_var_path(),
            "igpu_present": igpu,
            "backup": self._mux_backup_read(),
        }

    def _mux_backup_path(self) -> str:
        return os.path.join(core.state_dir(), "mux-backup.json")

    def _mux_backup_read(self) -> dict | None:
        try:
            with open(self._mux_backup_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _mux_backup_write(self, before: dict) -> dict:
        """Record the pre-switch state so the switch can be undone by hand.

        Kept as the *raw* efivarfs image, not as a mode: if a future firmware
        revision packs something else into the neighbouring bytes, replaying
        the exact bytes is the only restore that is faithful.
        """
        raw = self.ec.mux_var_raw()
        rec = {
            "t": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "requested": before.get("requested"),
            "effective": before.get("effective"),
            "ec_bit": before.get("ec_bit"),
            "var": raw.hex() if raw is not None else None,
        }
        os.makedirs(core.state_dir(), exist_ok=True)
        path = self._mux_backup_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return rec

    def set_graphics(self, mode: str) -> dict:
        """Request a graphics mode.  Validates, backs up, writes, reads back.

        Never reboots: a switch that rebooted the machine on its own would be
        the single worst thing this program could do to a user mid-task.  The
        reply carries the outstanding reboot, and the front-ends say so.
        """
        if not self.cfg.get("AllowMuxSwitch"):
            raise ValidationError(
                "the graphics (MUX) switch is disabled; set "
                f"\"AllowMuxSwitch\": true in {core.config_path()} first")
        if not core.feature("mux"):
            raise NotSupported.feature("mux")
        mode = core.check_mux_mode(mode)
        before = self.graphics()
        if not before["supported"]:
            raise BackendError(
                "this firmware exposes neither the EC graphics bit nor the "
                f"{core.MUX_VAR_NAME} variable; the switch is not available "
                "on this machine")
        was = before["requested"]
        if was == mode and before["effective"] == mode:
            return {
                "mode": mode, "was": was, "now": was, "changed": False,
                "reboot_required": False,
                "summary": f"graphics mode already {core.MUX_LABELS[mode]}",
                "activity": core.graphics_activity(mode, was, was),
                "graphics": before,
            }
        backup = self._mux_backup_write(before)
        self.ec.mux_apply(mode)
        after = self.graphics()
        if after["requested"] != mode:
            # The EC took the byte but the variable did not, or vice versa.
            # Say so loudly rather than reporting a switch that did not happen.
            raise BackendError(
                f"the graphics mode did not stick (asked for {mode}, read "
                f"back {after['requested']}).  The running session is "
                "unchanged; check the EC register and the UEFI variable")
        log.info("graphics mode %s -> %s (reboot required to apply)",
                 was, mode)
        return {
            "mode": mode, "was": was, "now": mode,
            "changed": was != mode,
            "reboot_required": after["reboot_required"],
            "summary": (f"graphics mode {was} -> {mode}" if was != mode
                        else f"graphics mode already {mode}"),
            "activity": core.graphics_activity(mode, was, mode),
            "backup": backup,
            "graphics": after,
        }

    def revert_graphics(self) -> dict:
        """Undo the last graphics switch from the recorded backup."""
        if not self.cfg.get("AllowMuxSwitch"):
            raise ValidationError(
                "the graphics (MUX) switch is disabled; set "
                f"\"AllowMuxSwitch\": true in {core.config_path()} first")
        if not core.feature("mux"):
            raise NotSupported.feature("mux")
        rec = self._mux_backup_read()
        if not rec:
            raise BackendError("no graphics-mode backup has been taken yet")
        target = rec.get("requested")
        if target not in core.MUX_MODES:
            raise BackendError(
                f"the graphics-mode backup names an unknown mode: {target!r}")
        raw = rec.get("var")
        blob = None
        if raw:
            try:
                blob = bytes.fromhex(raw)
            except ValueError:
                raise BackendError(
                    "the graphics-mode backup is corrupt (bad var hex)") \
                    from None
        ec_bit = rec.get("ec_bit")
        before = self.graphics()
        self.ec.mux_restore(ec_bit if isinstance(ec_bit, int) else None, blob)
        after = self.graphics()
        log.info("graphics mode restored to %s (reboot required to apply)",
                 target)
        return {
            "mode": target, "was": before["requested"], "now": after["requested"],
            "changed": before["requested"] != after["requested"],
            "reboot_required": after["reboot_required"],
            "summary": f"graphics mode restored to {target}",
            "activity": (f"Restored graphics mode to {core.MUX_LABELS[target]}"
                         "; reboot to apply"),
            "graphics": after,
        }

    def _settings_of(self, prof: dict) -> list:
        """The scalar settings a profile would touch, in a stable order."""
        return [k for k in ("shift", "fan", "cooler_boost", "charge_end")
                if prof.get(k) is not None]

    def _read_settings(self, keys) -> dict:
        out = {}
        for key in keys:
            try:
                out[key] = self.ec.get(key)
            except (ValidationError, BackendError):
                out[key] = None
        return out

    def _apply_profile_dict(self, prof: dict) -> tuple:
        """Apply one profile.

        Returns (warnings, changes, activities).  `changes` is a list of
        human sentences ("performance comfort -> sport", "cpu curve (3
        points)") computed by reading the EC back before and after, never
        by echoing the request.  `activities` is the same information phrased
        as what was *done* ("Set performance mode to sport"), which is what
        a status bar or notification should say; the two come from the same
        read-back so they can never disagree.  A setting the profile does
        not mention is left out of both entirely rather than reported as
        "unchanged".  Raises on hard errors.
        """
        warnings = []
        keys = self._settings_of(prof)
        before = self._read_settings(keys)

        if prof.get("shift") is not None:
            self.ec.set("shift", prof["shift"])
        if prof.get("fan") is not None:
            self.ec.set("fan", prof["fan"])
        curve = (prof.get("curve") or {}) if core.feature("fan_curve") else {}
        curve_notes = []
        curve_acts = []
        for which in ("cpu", "gpu"):
            if which in curve and curve[which]:
                old = None
                try:
                    old = self.ec.curve(which)
                except BackendError:
                    pass
                new = {"temps": curve[which]["temps"],
                       "speeds": curve[which]["speeds"]}
                self.ec.set_curve(which, new["temps"], new["speeds"])
                moved = core.curve_points_changed(old, new) \
                    if old is not None else 0
                if moved:
                    curve_notes.append(f"{which} curve ({moved} points)")
                    curve_acts.append(core.curve_activity(which, moved))
        if prof.get("cooler_boost") is not None:
            self.ec.set("cooler_boost", bool(prof["cooler_boost"]))
        # `charge_end` is optional in a profile on purpose (see
        # BUILTIN_PROFILES): absent means "leave the charge limit alone".
        # Only "battery" carries one, because a limit is a battery-health
        # choice rather than a performance one, and every other profile
        # silently resetting it to 100% undid a deliberate setting.
        if prof.get("charge_end") is not None:
            try:
                self.ec.set_charge_end(prof["charge_end"])
            except BackendError as e:
                # e.g. no battery currently present; profile is otherwise live
                warnings.append(str(e))

        # The two CPU-side keys follow the same optional rule as charge_end:
        # absent means leave it alone.  A *named* cap/offset is the profile
        # doing its job, and unlike the re-apply snapshot (which must never
        # resurrect caps the kernel reset at boot) it is applied here.
        if prof.get("freq_khz") is not None:
            before_hz = core.cpufreq_facts().get("max_khz")
            try:
                self.freq.set_all_max(prof["freq_khz"])
                after_hz = core.cpufreq_facts().get("max_khz")
                if before_hz != after_hz:
                    curve_notes.append(
                        f"max frequency {core.khz_to_ghz(before_hz)} -> "
                        f"{core.khz_to_ghz(after_hz)} GHz")
                    curve_acts.append(f"Set CPU max frequency to "
                                       f"{core.khz_to_ghz(after_hz)} GHz")
            except (BackendError, ValidationError) as e:
                warnings.append(f"frequency cap skipped: {e}")
        if prof.get("undervolt") is not None:
            if not self.cfg.get("AllowUndervolt"):
                warnings.append("voltage offsets skipped: undervolting is "
                                "off in the config (set AllowUndervolt)")
            else:
                before_uv = self.uv.read_planes()
                try:
                    self.uv.apply(prof["undervolt"], fill_missing=False)
                    after_uv = self.uv.read_planes() or {}
                    base = before_uv or {}
                    moved = sum(1 for p, v in after_uv.items()
                                if base.get(p) != v)
                    if moved:
                        curve_notes.append(f"voltage offsets ({moved} plane"
                                           f"{'s' if moved != 1 else ''})")
                        curve_acts.append("Applied voltage offsets")
                except (BackendError, ValidationError) as e:
                    warnings.append(f"voltage offsets skipped: {e}")

        after = self._read_settings(keys)
        changes = core.diff_settings(before, after) + curve_notes
        acts = [core.describe_activity(k, before.get(k), after.get(k))
                for k in sorted(set(before) | set(after))
                if before.get(k) != after.get(k)]
        acts += curve_acts
        return warnings, changes, acts

    def apply_profile(self, name: str) -> tuple:
        prof = self.profiles.load(name)
        warnings, changes, acts = self._apply_profile_dict(prof)
        self.save_last()
        log.info("applied profile %s: %s", name,
                 "; ".join(changes) or "no change")
        return warnings, changes, acts

    # -- boot / resume ------------------------------------------------------

    def restore(self) -> None:
        if self.args.no_restore:
            log.info("state restore disabled by --no-restore")
            return
        default = self.cfg.get("DefaultProfile")
        try:
            if default:
                warnings, changes, _acts = self.apply_profile(default)
                for w in warnings:
                    log.warning("%s", w)
                log.info("applied default profile %r: %s", default,
                         "; ".join(changes) or "no change")
                return
            if self.cfg.get("RestoreLast"):
                last = os.path.join(core.state_dir(), "last.json")
                if os.path.exists(last):
                    with open(last, "r", encoding="utf-8") as fh:
                        snap = json.load(fh)
                    warnings, changes, _acts = \
                        self._apply_profile_dict(snap)
                    for w in warnings:
                        log.warning("%s", w)
                    log.info("restored last state: %s",
                             "; ".join(changes) or "no change")
                else:
                    self.capture_factory()
                    log.info("no saved state yet; captured factory curve")
        except (ValidationError, BackendError) as e:
            log.warning("restore failed: %s", e)

    def capture_factory(self) -> str:
        """Save the untouched firmware curve once, so it can always come back.

        Returns what actually happened: "captured", "exists" (one was
        already saved, nothing was overwritten) or "unsupported", because a
        button that says "Captured factory profile" when it silently did
        nothing is a small lie, and this one is easy to hit twice.
        """
        if not core.feature("fan_curve"):
            # Nothing to capture: this model has no curve registers defined.
            return "unsupported"
        if os.path.exists(self.profiles._path("factory")):
            return "exists"
        try:
            data = {"curve": {w: self.ec.curve(w) for w in ("cpu", "gpu")}}
            data["fan"] = self.ec.read_opt("fan_mode")
            data["shift"] = self.ec.read_opt("shift_mode")
            self.profiles.save("factory", data)
            log.info("captured factory profile from EC")
            return "captured"
        except Exception as e:                       # noqa: BLE001
            log.warning("could not capture factory profile: %s", e)
            return "failed"

    def reapply(self) -> tuple:
        """After resume: put the persisted state back.  Returns (warnings,
        changes, activities): a resume that changes nothing says so, instead
        of claiming it did something."""
        if not self.cfg.get("ReapplyOnResume", True):
            return [], [], []
        default = self.cfg.get("DefaultProfile")
        if default:
            return self.apply_profile(default)
        last = os.path.join(core.state_dir(), "last.json")
        if os.path.exists(last):
            with open(last, "r", encoding="utf-8") as fh:
                snap = json.load(fh)
            warnings, changes, acts = self._apply_profile_dict(snap)
            self.save_last()
            log.info("re-applied state after resume: %s",
                     "; ".join(changes) or "no change")
            return warnings, changes, acts
        return [], [], []

    # -- hardware self-test ----------------------------------------------------

    def selftest(self) -> dict:
        """`verify.sh` as a daemon command: reversible round-trips, restored.

        Every check writes real settings and completes only when they read
        back, then everything captured at the start is put back whatever
        happened on the way.  It lives on Service so that every write is one
        of its own validated, read-back primitives; nothing here touches
        the EC by a new route.  The report is machine-shaped (a flat list
        of checks) so the CLI can print it and the GUI can render it, with
        a paste-ready markdown body for a pull request.
        """
        results = []

        def check(section: str, name: str, ok, detail: str = ""):
            results.append({"section": section, "name": name,
                            "ok": bool(ok), "detail": str(detail or "")})

        st = self.status()

        # -- reads: which machine, which driver
        drv = st.get("driver") or {}
        model = st.get("model") or core.MODEL.summary()
        check("driver", "msi-ec driver bound", bool(drv.get("present")),
              drv.get("fw") or "no msi-ec sysfs directory")
        check("driver", "EC debug interface", bool(drv.get("debug")),
              "present" if drv.get("debug") else
              "missing: RPM, curves and register reads need debug=1")
        check("model", "model file matched", bool(model.get("supported")),
              f"matched: {model.get('id')}" if model.get("supported")
              else "no model file matches; register-backed features are off")

        # -- setting round-trips: set-then-verify, restore last
        def walk(key: str, section: str, modes, orig):
            for mode in modes:
                err = ""
                got = None
                try:
                    self.ec.set(key, mode)
                    got = self.ec.get(key)
                except (BackendError, ValidationError, NotSupported) as e:
                    err = str(e)
                check(section, f"{key} {mode} -> read back "
                      f"{got if got is not None else 'nothing'}",
                      got == mode, err)
            if orig is not None:
                err = ""
                got = None
                try:
                    self.ec.set(key, orig)
                    got = self.ec.get(key)
                except (BackendError, ValidationError, NotSupported) as e:
                    err = str(e)
                check(section, f"{key} restored to {orig}", got == orig, err)

        walk("shift", "shift mode", (st.get("shift") or {}).get("available")
             or [], (st.get("shift") or {}).get("current"))
        walk("fan", "fan mode", (st.get("fan") or {}).get("available") or [],
             (st.get("fan") or {}).get("current"))

        boost_orig = st.get("cooler_boost")
        for want in (True, False):
            err = ""
            got = None
            expect = "on" if want else "off"
            try:
                self.ec.set("cooler_boost", want)
                got = self.ec.get("cooler_boost")
            except (BackendError, ValidationError, NotSupported) as e:
                err = str(e)
            check("cooler boost", f"cooler boost {expect} -> read back "
                  f"{got if got is not None else 'nothing'}",
                  got == expect, err)
        if boost_orig in ("on", "off"):
            err = ""
            got = None
            try:
                self.ec.set("cooler_boost", boost_orig == "on")
                got = self.ec.get("cooler_boost")
            except (BackendError, ValidationError, NotSupported) as e:
                err = str(e)
            check("cooler boost", f"cooler boost restored to {boost_orig}",
                  got == boost_orig, err)

        # -- charge threshold: write two values, verify the pairing rule,
        # -- restore the original (clean)
        orig_ce = (st.get("battery") or {}).get("charge_end")
        if orig_ce is None:
            check("charge limit", "battery charge threshold", False,
                  "no battery exposing charge_control_end_threshold")
        else:
            def read_start() -> "int | None":
                try:
                    # battery_dir charges BackendError; the walk is
                    # allowed to run a battery-less leg (a laptop on the
                    # dock with the battery out) without crashing
                    path = os.path.join(self.ec.battery_dir(),
                                        "charge_control_start_threshold")
                except BackendError:
                    return None
                return core._read_int(path)

            for pct in (80, 60):
                err = ""
                end = start = None
                pre_start = read_start()
                try:
                    self.ec.set("charge_end", pct)
                    end = self.ec.charge_end()
                    start = read_start()
                except (BackendError, ValidationError) as e:
                    err = str(e)
                good = end == pct
                check("charge limit", f"charge limit {pct} -> EC says {end}",
                      good, err)
                if not good:
                    continue
                # The start threshold is the driver's doing, not ours: msi-ec
                # normally keeps it 10 under the limit, but a firmware that
                # pins it (or a plain test fixture) is not a failed write, so
                # a start holding its own value passes with a note instead of
                # a false red on hardware we cannot influence.
                if start == pct - 10:
                    check("charge limit",
                          f"start threshold tracks the limit ({pct - 10})",
                          True, "")
                elif pre_start is None or start == pre_start:
                    check("charge limit",
                          f"start threshold held at {start}, not paired "
                          "to the limit by this driver", True, "")
                else:
                    check("charge limit",
                          f"start threshold is {start}, expected "
                          f"{pct - 10} or {pre_start}", False, "")
            err = ""
            got = None
            try:
                self.ec.set("charge_end", orig_ce)
                got = self.ec.charge_end()
            except (BackendError, ValidationError) as e:
                err = str(e)
            check("charge limit", f"charge limit restored to {orig_ce}",
                  got == orig_ce, err)

        # -- fan curve write-back (re-writing what is already there)
        if not core.feature("fan_curve"):
            check("fan curves", "curve write-back", True,
                  "skipped: no proven curve registers for this model")
        else:
            try:
                cur = self.ec.curve("cpu")
                self.ec.set_curve("cpu", cur["temps"], cur["speeds"])
                back = self.ec.curve("cpu")
                same = back == cur
                check("fan curves", "cpu curve re-written unchanged", same,
                      f"{cur['temps']} / {cur['speeds']}" if same else
                      "the write-back did not stick")
            except (BackendError, ValidationError, NotSupported) as e:
                check("fan curves", "cpu curve write-back", False, str(e))

        # -- cpufreq write-back (re-capping every core where it already is)
        freq = st.get("freq") or {}
        if not freq.get("present"):
            check("cpu frequency", "cpufreq write-back", True,
                  "skipped: no cpufreq on this machine")
        elif freq.get("max_khz") is None:
            check("cpu frequency", "cpufreq write-back", True,
                  "skipped: the cores currently disagree on their caps")
        else:
            try:
                self.freq.set_all_max(freq["max_khz"])
                check("cpu frequency", "cpufreq write-back", True,
                      f"every core re-capped at "
                      f"{core.khz_to_ghz(freq['max_khz'])} GHz")
            except (BackendError, ValidationError) as e:
                check("cpu frequency", "cpufreq write-back", False, str(e))

        # -- sanity: what the sensors say must be plausible
        for which in ("cpu", "gpu"):
            d = st.get(which) or {}
            r = d.get("rpm")
            if not core.feature("rpm"):
                check("sanity", f"{which} RPM", True,
                      "skipped: no proven tachometer registers")
            else:
                check("sanity", f"{which} RPM plausible at {r}",
                      r is None or 0 <= r < 10000,
                      "" if r is None or 0 <= r < 10000 else
                      "implausible tachometer count")
            t = d.get("temp")
            check("sanity", f"{which} temperature plausible at {t}",
                  t is None or 0 <= t <= 110,
                  "" if t is None or 0 <= t <= 110 else
                  "implausible temperature")

        # -- informational: the MUX agrees with itself unless a switch is
        # -- pending; the OC mailbox, if this kernel exposes one
        if core.feature("mux") and st.get("graphics"):
            g = st["graphics"]
            if g.get("reboot_required"):
                check("graphics mode", "running vs requested mode", True,
                      "a switch is pending; the reboot applies it")
            else:
                check("graphics mode", "running vs requested mode", True,
                      f"both sources say {g.get('effective')}")
        uv = st.get("undervolt") or {}
        if uv.get("msr_present"):
            planes = uv.get("planes") or {}
            active = sum(1 for v in planes.values() if v)
            check("undervolting", "OC mailbox reads", True,
                  f"{active} active offset"
                  f"{'s' if active != 1 else ''}")
        else:
            check("undervolting", "OC mailbox reads", True,
                  "skipped: no MSR devices (is the msr module loaded?)")

        passed = sum(1 for r in results if r["ok"])
        ok_all = passed == len(results)
        log.info("self-test: %d/%d checks passed", passed, len(results))
        return {"ok": ok_all, "results": results,
                "summary": (f"self-test: {passed}/{len(results)} checks "
                            "passed" if ok_all else
                            f"self-test: {len(results) - passed} check"
                            f"{'s' if len(results) - passed != 1 else ''} "
                            f"failed of {len(results)}"),
                "markdown": core.selftest_markdown(st, results)}

    # -- command dispatch -----------------------------------------------------

    def handle(self, cmd: dict) -> dict:
        if not isinstance(cmd, dict):
            raise ValidationError("request must be a JSON object")
        name = cmd.get("cmd")
        if name == "ping":
            return {"pong": True, "pid": os.getpid()}
        if name == "status":
            return self.status()
        if name == "set":
            key = cmd.get("key")
            if not isinstance(key, str):
                raise ValidationError("missing setting key")
            res = self.ec.set(key, cmd.get("value"))
            self.save_last()
            res["summary"] = core.describe_change(key, res["was"], res["now"])
            res["activity"] = core.describe_activity(
                key, res["was"], res["now"])
            return res
        if name == "curve":
            which = cmd.get("which", "cpu")
            before = self.ec.curve(which)
            out = self.ec.set_curve(which, cmd.get("temps"), cmd.get("speeds"))
            self.save_last()
            moved = core.curve_points_changed(before, out)
            out.update({
                "which": which,
                "changed": moved > 0,
                "summary": (f"{which} curve ({moved} points)"
                            if moved else f"{which} curve unchanged"),
                "activity": core.curve_activity(which, moved),
            })
            return out
        if name == "profile":
            action = cmd.get("action")
            if action == "list":
                return self.profiles.list()
            if action == "show":
                # read-only: what a profile contains, without applying it
                prof = self.profiles.load(cmd.get("name"))
                prof.pop("name", None)
                prof.pop("builtin", None)
                return prof
            if action == "apply":
                name_ = cmd.get("name")
                warnings, changes, acts = self.apply_profile(name_)
                return {"applied": name_, "warnings": warnings,
                        "changes": changes,
                        "activities": acts,
                        "summary": (f"applied {name_}: " + "; ".join(changes))
                        if changes else f"applied {name_}: nothing to change",
                        "activity": (f"Applied {name_}: " + "; ".join(acts))
                        if acts else f"Applied {name_}: nothing to change"}
            if action == "save":
                name_ = cmd.get("name")
                self.profiles.save(name_, cmd.get("data") or {})
                return {"saved": core.check_profile_name(name_)}
            if action == "delete":
                self.profiles.delete(cmd.get("name"))
                return {"deleted": cmd.get("name")}
            if action == "save-current":
                name_ = cmd.get("name")
                st = self.status()
                data = {
                    "shift": st["shift"]["current"],
                    "fan": st["fan"]["current"],
                    "cooler_boost": st["cooler_boost"] == "on",
                }
                if st.get("curve"):
                    data["curve"] = st["curve"]
                if st["battery"] and st["battery"]["charge_end"] is not None:
                    data["charge_end"] = st["battery"]["charge_end"]
                # The CPU-side state is saved the same way: a named, saved
                # bundle may carry it; the built-ins and the re-apply
                # snapshot still do not.
                if (st.get("freq") or {}).get("max_khz") is not None:
                    data["freq_khz"] = st["freq"]["max_khz"]
                planes = (st.get("undervolt") or {}).get("planes") or {}
                live = {p: mv for p, mv in planes.items() if mv}
                if live:
                    data["undervolt"] = live
                self.profiles.save(name_, data)
                return {"saved": core.check_profile_name(name_)}
            raise ValidationError(f"unknown profile action {action!r}")
        if name == "capture-factory":
            result = self.capture_factory()
            return {"captured": "factory", "result": result}
        if name == "freq":
            action = cmd.get("action", "show")
            if action == "show":
                return self.freq.facts()
            if action == "set":
                # Not folded into the re-apply snapshot deliberately: the
                # kernel resets scaling_max_freq at boot anyway, and a
                # frequency cap is a live decision (plugged in vs battery),
                # not a machine state like the shift mode is.
                return self.freq.set_all_max(cmd.get("max_khz"))
            raise ValidationError(f"unknown freq action {action!r}")
        if name == "undervolt":
            action = cmd.get("action", "show")
            if action == "show":
                return {"planes": self.uv.read_planes(),
                        "msr_present": self.uv.available(),
                        "allowed": bool(self.cfg.get("AllowUndervolt"))}
            # The write is deliberately gated: an offset that is too deep
            # freezes the machine with the fans spinning, and the BIOS
            # unlock it rests on is a conscious act.  Nothing about it
            # persists on the daemon side; the GUI re-applies a saved set
            # when the *app* starts, which is the whole stability design.
            if not self.cfg.get("AllowUndervolt"):
                raise ValidationError(
                    "undervolting is disabled (set AllowUndervolt in "
                    f"{core.config_path()})")
            if action == "apply":
                clean = self.uv.apply(cmd.get("values") or {})
                return {"applied": clean,
                        "summary": "; ".join(
                            f"{p} {clean[p]} mV" for p, _ in core.UV_PLANES),
                        "volatile": "the offsets clear at reboot and sleep"}
            if action == "reset":
                clean = self.uv.apply({p: 0 for p, _ in core.UV_PLANES})
                return {"applied": clean, "summary": "all offsets back to 0",
                        "volatile": "the offsets clear at reboot and sleep"}
            raise ValidationError(f"unknown undervolt action {action!r}")
        if name == "overdrive":
            action = cmd.get("action", "show")
            if action == "show":
                return self.overdrive_status()
            if action == "set":
                out = self.ec.set_overdrive(cmd.get("on") is True)
                out.update({"supported": True,
                            "summary": ("display overdrive on"
                                        if out["now"] else
                                        "display overdrive off")})
                return out
            raise ValidationError(f"unknown overdrive action {action!r}")
        if name == "graphics":
            action = cmd.get("action", "show")
            if action == "show":
                return self.graphics()
            if action == "set":
                return self.set_graphics(cmd.get("mode"))
            if action == "revert":
                return self.revert_graphics()
            raise ValidationError(f"unknown graphics action {action!r}")
        if name == "reapply":
            warnings, changes, acts = self.reapply()
            return {"warnings": warnings, "changes": changes,
                    "activities": acts,
                    "summary": ("re-applied: " + "; ".join(changes))
                    if changes else "re-applied: nothing to change",
                    "activity": ("Re-applied settings: " + "; ".join(acts))
                    if acts else "Re-applied settings: nothing to change"}
        if name == "selftest":
            # The same reversible round-trip verify.sh performs, inside the
            # daemon where the writes already live: every mode walked, every
            # value read back, everything restored.  The client gates it
            # deliberately (--yes / a dialog), exactly like the graphics
            # switch.
            return self.selftest()
        if name == "raw":
            if not self.cfg.get("AllowRaw"):
                raise ValidationError(
                    "raw EC access is disabled (set AllowRaw in "
                    f"{core.config_path()})")
            addr = core.check_int(cmd.get("addr"), 0, 0xFF, "EC address")
            if cmd.get("val") is None:
                with self.ec.lock:
                    val = self.ec.ec_read(addr)
                return {"addr": addr, "val": val}
            val = core.check_int(cmd.get("val"), 0, 0xFF, "EC value")
            with self.ec.lock:
                self.ec.ec_write(addr, val)
            return {"addr": addr, "val": val}
        raise ValidationError(f"unknown command {name!r}")


# --------------------------------------------------------------------------
# Socket server
# --------------------------------------------------------------------------

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(64 * 1024)
            if not line:
                return
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError as e:
                resp = {"ok": False, "error": f"bad JSON: {e}"}
            else:
                try:
                    data = self.server.service.handle(cmd)
                    resp = {"ok": True, "data": data}
                except (ValidationError, BackendError) as e:
                    resp = {"ok": False, "error": str(e)}
                except Exception as e:               # noqa: BLE001
                    log.exception("internal error handling %r", cmd)
                    resp = {"ok": False, "error": f"internal error: {e}"}
            self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


def already_running(path: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(path)
        return True
    except OSError:
        return False


def wait_for_driver(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isdir(core.msi_ec_dir()):
            return True
        time.sleep(0.5)
    return os.path.isdir(core.msi_ec_dir())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="msi-controld", description=__doc__)
    ap.add_argument("--socket", default=None, help="socket path")
    ap.add_argument("--sysfs", default=None, help="sysfs root (testing)")
    ap.add_argument("--config", default=None, help="config path")
    ap.add_argument("--state", default=None, help="state directory")
    ap.add_argument("--no-restore", action="store_true",
                    help="do not re-apply saved state at startup")
    ap.add_argument("--wait", type=float, default=60.0,
                    help="seconds to wait for the msi-ec driver (default 60)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s: %(message)s", stream=sys.stdout)

    if args.socket:
        os.environ["MSI_SOCK"] = args.socket
    path = core.sock_path()

    # Single instance: talk to an existing daemon before touching its socket.
    if already_running(path):
        log.error("another msi-controld is already listening on %s", path)
        return 1

    service = Service(args)

    if not args.sysfs:
        log.info("waiting up to %.0fs for the msi-ec driver", args.wait)
        if not wait_for_driver(args.wait):
            log.warning("msi-ec driver not present yet; will report "
                        "'driver absent' until it appears")

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

    server = Server(path, Handler)
    server.service = service

    # Socket permissions: root:<SocketGroup> 0660.  Wheel membership already
    # implies admin, so this grants no new privilege to anyone who gets it.
    try:
        gid = grp.getgrnam(str(service.cfg.get("SocketGroup", "wheel"))).gr_gid
    except KeyError:
        gid = 10  # wheel on every mainstream distro
    if os.geteuid() == 0:
        os.chown(path, 0, gid)
    os.chmod(path, 0o660)
    log.info("listening on %s", path)

    service.restore()

    def _stop(signum, frame):
        log.info("signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
