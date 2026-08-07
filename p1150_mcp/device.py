# -*- coding: utf-8 -*-
"""
MIT License

Owns the single P1150 connection for the life of the MCP server process.

The server is long lived on purpose.  A developer sets the supply voltage once,
connects the probe, and then spends an hour editing and re-flashing firmware
while the target stays powered.  Re-connecting per tool call would drop power to
the target every time, and pay the calibration cost repeatedly.

Everything that has a mandatory ordering (vout before probe, timebase before
acquisition, stop before close) is enforced here rather than exposed, so the
agent cannot sequence it wrongly.
"""
import os
import time
import threading
from timeit import default_timer as timer

import numpy as np

from pxxxx import PXXXX, PxxxxAPI, ACQ_FORMAT_NUMPY

from . import config, analysis

SAMPLE_RATE = 125_000

# MCP-facing aux channel name -> key in the driver's acquisition dict.
AUX_KEYS = {"A0": "a0", "D0": "d0", "D1": "d1"}

# Trigger sources for the aux inputs.  A0A is the analog input; D0/D1 are the
# digital ones.  TRIG_SRC_D0S (the serial decode on D0) is deliberately absent:
# nothing in this server configures or reports that stream.
AUX_TRIG_SRC = {
    "A0": PxxxxAPI.TRIG_SRC_A0A,
    "D0": PxxxxAPI.TRIG_SRC_D0,
    "D1": PxxxxAPI.TRIG_SRC_D1,
}

# Trigger levels are in the units the channel reports, which for D0/D1 is the
# driver's rescaled millivolts (~100 low, ~900 high) and NOT 0/1 -- a level of
# 0.5 would sit below the low rail and fire immediately, every time.

# Chunk length for streaming captures.  200 ms is the value the P1150 examples
# use for ACQUIRE_MODE_LOGGER: long enough that the per-chunk overhead is small,
# short enough that a stop request is honoured promptly.
STREAM_TIMEBASE = PxxxxAPI.TBASE_SPAN_200MS
STREAM_CHUNK_S = 0.200

CONNECT_ATTEMPTS = 2
CHUNK_TIMEOUT_S = 10.0

# Window length of each timebase span, in seconds.  Needed because the sample
# rate is only 125 kSps for spans up to one second: the acquisition buffer holds
# 125,000 samples, so a longer span necessarily arrives decimated.  Reporting a
# duration in milliseconds -- which is the whole basis of the inrush test -- has
# to use the rate the capture actually came back at, not the nominal one.
TBASE_SECONDS = {
    "TBASE_SPAN_10MS": 0.010, "TBASE_SPAN_20MS": 0.020,
    "TBASE_SPAN_50MS": 0.050, "TBASE_SPAN_100MS": 0.100,
    "TBASE_SPAN_200MS": 0.200, "TBASE_SPAN_500MS": 0.500,
    "TBASE_SPAN_1S": 1.0, "TBASE_SPAN_2S": 2.0,
    "TBASE_SPAN_5S": 5.0, "TBASE_SPAN_10S": 10.0,
}

# Settling time between arming the trigger and closing the probe relay for an
# inrush measurement.  acquisition_start returns once the command is accepted;
# this makes sure the instrument is genuinely waiting on its trigger before the
# event it is waiting for is created, since the event lasts a millisecond and
# does not come round again.
ARM_SETTLE_S = 0.05

# The P1150's over-current limit at power-on.  Used as the assumed limit when a
# capture is analysed without one having been set in this session.
OVC_DEFAULT_MA = 3200

# Two channels at 125k samples/s * 4 bytes = 1 MB/s retained.  The cap stops an
# agent that forgets to call capture_stop from consuming all memory: the 900 s
# default is ~900 MB.  Lower P1150_MAX_CAPTURE_S on a memory-constrained bench.
DEFAULT_MAX_CAPTURE_S = float(os.environ.get("P1150_MAX_CAPTURE_S") or 900)


class DeviceError(RuntimeError):
    pass


def scan() -> list:
    """Every P1150 attached to this machine, identified by serial number.

    A serial number is the only durable way to address a P1150: COM port names
    are assigned by the OS and move when the unit is re-plugged or a second one
    is added, so a port that worked yesterday can be a different instrument
    today.  The whole sweep costs tens of milliseconds -- enumeration is a USB
    descriptor read and the ping is one short exchange -- so connect() runs it
    to name the device it is about to open rather than trusting a port.

    The port this session already holds is reported from the details captured
    at connect time instead of being re-opened.  A serial port has exactly one
    owner, so pinging it here would fail and the connected instrument would
    disappear from its own device list.
    """
    devices = []
    for port in PXXXX.list_ports():
        if SESSION.is_connected() and port == SESSION.port:
            d = SESSION.details
            devices.append({
                "serial": d.get("serial_hash"), "port": port,
                "model": d.get("model"), "firmware": d.get("version"),
                "application": d.get("app"),
                "bootloader": d.get("app") == "a51",
                "in_use_by_this_server": True,
            })
            continue

        dev = PXXXX(port=port)
        try:
            ok, r = dev.ping()
        finally:
            dev.close()
        if not ok:
            continue
        p = r[-1]
        devices.append({
            "serial": p["serial_hash"], "port": port,
            "model": p["model"], "firmware": p["version"],
            "application": p["app"],
            # Both the bootloader (a51) and the application (a43) answer a
            # ping, so the answer alone does not mean the unit is ready; this
            # is what makes the difference between a connect that takes 20 ms
            # and one that takes 15 seconds.
            "bootloader": p["app"] == "a51",
        })
    return devices


def _describe(found: list) -> str:
    """One-line rendering of scan() output, for an error message."""
    return ", ".join(f"{d['serial']} on {d['port']}" for d in found) or "none"


class _ConnectProgress:
    """Turns the driver's per-phase progress into one monotonic 0-100 bar.

    ez_connect reports (percent, phase) with the percent restarting at zero for
    every phase it enters -- 'Writing firmware' 0..100, then 'Calibrating'
    0..100 -- and skips whatever is not needed, so a warm connect goes straight
    from 'Start' to 'Connected' to 'Done' in twenty milliseconds while a cold
    one walks the full sequence over fifteen seconds.  A progress bar has to
    rise once towards one total, so each phase gets a band of the overall range
    and the value is clamped so it can never go backwards.

    The bands are sized by how long each phase takes on a cold unit, not by how
    much work it represents: calibration is two thirds of the wait, so it gets
    two thirds of the bar.  A phase the driver adds later that is not listed
    here still relays its message, holding the bar where it was -- an unknown
    name is not a reason to go quiet.
    """

    # phase (lower case) -> (start, end) of its slice of the overall bar
    BANDS = {
        "start":            (2, 5),
        "writing firmware": (5, 45),
        "rebooting":        (45, 52),
        "connected":        (52, 55),
        "calibrating":      (55, 99),
    }

    # The driver's closing phase is dropped: connect() ends with a line naming
    # the instrument, its firmware and its temperature, which is the same "it
    # worked" said usefully. Two of them in the same millisecond is noise.
    SILENT = {"done"}

    # The driver's phase names are terse and assume you know the instrument.
    # These are for a developer watching a tool call, who wants to know why it
    # is taking fifteen seconds, not which DLL routine is running.
    LABELS = {
        "start":            "Opening the connection",
        "writing firmware": "Loading application firmware",
        "rebooting":        "Rebooting the P1150",
        "connected":        "Link established",
        "calibrating":      "Self-calibrating",
    }

    def __init__(self, callback=None):
        self._cb = callback
        self._pct = 0.0

    def emit(self, pct: float, message: str) -> None:
        self._pct = max(self._pct, float(pct))
        if self._cb is None:
            return
        try:
            self._cb(int(round(self._pct)), message)
        except Exception:
            # from_device() runs inside the ctypes callback, on the driver's
            # own stack. An exception raised there cannot propagate to the
            # caller and would be printed and swallowed by ctypes; losing a
            # progress line is not worth risking the state of a connect that
            # is halfway through writing firmware.
            pass

    def hold(self, message: str) -> None:
        """Say something without moving the bar."""
        self.emit(self._pct, message)

    def from_device(self, pct: int, phase: str) -> None:
        """Callback handed to ez_connect: (percent, phase-name)."""
        key = (phase or "").strip().lower()
        if key in self.SILENT:
            return
        label = self.LABELS.get(key) or (phase or "").strip() or "Working"
        band = self.BANDS.get(key)
        if band is None:
            self.hold(label)
            return
        pct = min(max(int(pct), 0), 100)
        lo, hi = band
        # The percent is only worth showing while it is actually moving: at 0
        # the phase has just started and at 100 it is over, and both read
        # better as a plain statement of what is happening.
        self.emit(lo + (hi - lo) * pct / 100.0,
                  f"{label} {pct}%" if 0 < pct < 100 else label)


def _join(chunks: list) -> tuple:
    """Concatenate chunk dicts into one (i, isnk, aux) triple.

    aux is {"D0": array, ...} holding whichever auxiliary channels were armed
    for this capture, and is empty when none were.
    """
    if not chunks:
        return np.empty(0, np.float32), np.empty(0, np.float32), {}
    aux_keys = [k for k in chunks[0] if k not in ("i", "isnk")]
    return (np.concatenate([c["i"] for c in chunks]),
            np.concatenate([c["isnk"] for c in chunks]),
            {k: np.concatenate([c[k] for c in chunks]) for k in aux_keys})


class Session:
    """A connected P1150, plus at most one capture in flight."""

    def __init__(self):
        self.dev = None
        self.port = None
        self.details = {}
        self.vout_mv = None
        self.ovc_ma = None
        self.probe_on = False

        self._acq_event = threading.Event()
        self._acq_chunk = None
        # Auxiliary channels to retain, snapshotted when a capture is armed so
        # that editing the config mid-capture cannot produce chunks of
        # different shapes that then fail to concatenate.
        self._aux = []

        self._capture_thread = None
        self._capture_stop = threading.Event()
        self._capture = None          # dict describing the run in flight
        self._capture_err = None

    # ---- connection ------------------------------------------------ #

    def _cb_acq(self, data: dict) -> None:
        """DLL acquisition callback.  Must copy and return immediately.

        Both current channels are retained.  They are independent and both
        positive-only: 'i' is current the P1150 sources into the target, 'isnk'
        is current flowing back into the P1150 -- which is what a target's
        charging circuit produces when the P1150 stands in for the battery.
        Net battery current is i - isnk.

        The auxiliary channels (a0, d0, d1) are retained only when the project
        has declared one, because each costs as much memory as a current
        channel and a long capture is already hundreds of megabytes.  Keeping
        all three unconditionally would triple that for data most sessions
        never look at.
        """
        chunk = {"i": data["i"], "isnk": data["isnk"]}
        for ch in self._aux:
            chunk[ch] = data[AUX_KEYS[ch]]
        self._acq_chunk = chunk
        self._acq_event.set()

    def _arm_aux(self) -> list:
        """Snapshot which aux channels this capture will retain."""
        self._aux = [c for c in config.aux_channels() if c in AUX_KEYS]
        return self._aux

    def _cb_async(self, data: dict) -> None:
        pass  # periodic ammeter/temperature chatter; nothing to do with it here

    def is_connected(self) -> bool:
        return self.dev is not None and self.dev.is_connected()

    def require(self) -> PXXXX:
        if not self.is_connected():
            raise DeviceError(
                "Not connected to a P1150. Call p1150_connect first "
                "(p1150_list_devices shows what is attached).")
        return self.dev

    def _find(self, sn: str, prog: "_ConnectProgress") -> tuple:
        """Resolve a serial number to a port, or pick the only unit attached.

        The scan happens even when the serial number is known, because it is
        what turns fifteen silent seconds into a sequence someone can follow:
        it names the instrument before the slow part starts and, crucially,
        says whether the unit is in its bootloader -- which is the difference
        between a connect that returns instantly and one that writes firmware
        and calibrates first.
        """
        prog.emit(1, "Scanning USB for attached P1150 units")
        found = scan()

        if sn:
            match = next((d for d in found
                          if (d["serial"] or "").upper() == sn.upper()), None)
            if match is None:
                # The scan matches on the short serial hash, which is what is
                # printed on the unit. The driver's own lookup also accepts the
                # long MCU serial, so fall back to it before giving up.
                port = PXXXX.get_port_from_sn(sn)
                if port is None:
                    raise DeviceError(
                        f"No P1150 with serial number {sn}. "
                        f"Attached: {_describe(found)}")
                return port, None
            return match["port"], match

        if not found:
            raise DeviceError(
                "No P1150 is attached to this machine. Check the USB cable, "
                "and that the unit is not already open in the desktop GUI -- "
                "only one program can own a P1150 at a time.")
        if len(found) > 1:
            raise DeviceError(
                f"{len(found)} P1150 units are attached, so which one to use "
                f"has to be said: {_describe(found)}. Pass the serial number "
                f"to p1150_connect, or set P1150_SN.")
        return found[0]["port"], found[0]

    def connect(self, sn: str = None, calibrate: bool = True,
                progress=None) -> dict:
        """Connect to a P1150 and leave it ready to measure.

        sn is the serial number printed on the back of the unit.  Omit it when
        exactly one P1150 is attached and it will be discovered; with more than
        one attached the choice has to be made by the caller, not guessed.

        progress, when supplied, is called as progress(percent, message) as the
        connection proceeds -- percent rising once from 0 to 100 over the whole
        operation.  It exists because a cold connect takes about fifteen
        seconds of firmware download and self-calibration, and a developer
        watching an agent work has no way to tell that from a hang.
        """
        if self.is_connected():
            raise DeviceError(
                f"Already connected to {self.details.get('serial_hash')}. "
                f"Call p1150_disconnect first.")

        prog = _ConnectProgress(progress)
        port, found = self._find(sn, prog)
        if found is not None:
            prog.emit(4, f"Found P1150 {found['serial']} on {port}" + (
                " -- in the bootloader, so the application firmware has to be "
                "loaded and the unit calibrated first (about 15 s)"
                if found["bootloader"] else ""))
        else:
            prog.emit(4, f"Connecting to P1150 {sn} on {port}")

        # From a cold boot the P1150 answers as its bootloader 'a51'; ez_connect
        # pushes the application image and it comes back as 'a43'.  One retry
        # covers the case where the first connect landed mid-transition.
        attempts = CONNECT_ATTEMPTS
        details = None
        while attempts >= 1:
            dev = PXXXX(port=port,
                        cb_uclog_async=self._cb_async,
                        cb_acquisition_get_data=self._cb_acq,
                        acq_format=ACQ_FORMAT_NUMPY)
            ok, details = dev.ez_connect(calibrate=calibrate,
                                         progress_callback=prog.from_device)
            if not ok:
                dev.close()
                raise DeviceError(f"ez_connect failed on {port}: {details}")
            if details.get("app") == "a43":
                self.dev = dev
                break
            dev.close()          # still the bootloader; release the port
            attempts -= 1
            if attempts >= 1:
                prog.hold("Still in the bootloader; trying once more")

        if self.dev is None:
            raise DeviceError(
                f"P1150 on {port} stayed in the bootloader after "
                f"{CONNECT_ATTEMPTS} attempts. Power cycle it and retry.")

        self.port = port
        self.details = details
        prog.emit(100, f"P1150 {details.get('serial_hash')} ready on {port} "
                       f"-- firmware {details.get('version')}, "
                       f"{float(details.get('t_degc', 0.0)):.0f} C")
        return {
            "connected": True,
            "port": port,
            "serial": details.get("serial_hash"),
            "model": details.get("model"),
            "firmware": details.get("version"),
            "calibrated": details.get("cal_done"),
            "temperature_c": round(float(details.get("t_degc", 0.0)), 1),
            "driver_version": PXXXX.version(),
        }

    def disconnect(self) -> dict:
        if self._capture_thread is not None:
            self.capture_stop_raw()
        if self.dev is not None:
            try:
                self.dev.acquisition_stop()
                self.dev.probe(connect=False)
            except Exception:
                pass
            self.dev.close()
        self.dev = None
        self.probe_on = False
        self.vout_mv = None
        return {"connected": False}

    # ---- power ----------------------------------------------------- #

    def power_on(self, voltage_mv: int, ovc_ma: int) -> dict:
        """Set voltage and over-current limit, then connect the probe.

        Order matters: the probe relay must not close onto an unset or stale
        output voltage, so vout and OVC are programmed first every time.
        """
        dev = self.require()

        ok, m = dev.vout_metrics()
        if ok and m:
            lo, hi = m[-1]["min"], m[-1]["max"]
            if not (lo <= voltage_mv <= hi):
                raise DeviceError(
                    f"{voltage_mv} mV is outside this P1150's range "
                    f"{lo}-{hi} mV.")

        ok, r = dev.set_vout(int(voltage_mv))
        if not ok:
            raise DeviceError(f"set_vout({voltage_mv}) failed: {r}")
        self.vout_mv = int(voltage_mv)

        ok, r = dev.set_ovc(int(ovc_ma))
        if not ok:
            raise DeviceError(f"set_ovc({ovc_ma}) failed: {r}")
        self.ovc_ma = int(ovc_ma)

        ok, r = dev.probe(connect=True)
        if not ok:
            raise DeviceError(f"probe connect failed: {r}")
        self.probe_on = True

        return {"powered": True, "voltage_mv": self.vout_mv,
                "ovc_ma": self.ovc_ma, "probe_connected": True}

    def power_off(self) -> dict:
        dev = self.require()
        ok, r = dev.probe(connect=False)
        if not ok:
            raise DeviceError(f"probe disconnect failed: {r}")
        self.probe_on = False
        return {"powered": False, "probe_connected": False}

    # ---- status ---------------------------------------------------- #

    ERRORS = [
        (PxxxxAPI.ERROR_I2C, "I2C"), (PxxxxAPI.ERROR_HAL, "HAL"),
        (PxxxxAPI.ERROR_INIT, "INIT"), (PxxxxAPI.ERROR_INIT_TMP, "INIT_TEMP"),
        (PxxxxAPI.ERROR_INIT_VMAIN, "INIT_VMAIN"),
        (PxxxxAPI.ERROR_INIT_ADC, "INIT_ADC"),
        (PxxxxAPI.ERROR_INIT_USBPD, "INIT_USBPD"),
        (PxxxxAPI.ERROR_TEMPERATURE, "OVER_TEMPERATURE"),
        (PxxxxAPI.ERROR_VOUT_FAILURE, "VOUT_FAILURE"),
        (PxxxxAPI.ERROR_CAL, "CALIBRATION"),
        (PxxxxAPI.ERROR_PROBE_CON, "PROBE_CONNECT"),
        (PxxxxAPI.ERROR_SRC_CURRENT, "OVER_CURRENT_SOURCE"),
        (PxxxxAPI.ERROR_SNK_CURRENT, "OVER_CURRENT_SINK"),
    ]

    def status(self) -> dict:
        dev = self.require()
        ok, r = dev.status()
        if not ok:
            raise DeviceError("status() failed")
        s = r[-1]
        errs = [name for bit, name in self.ERRORS if s["err"] & bit]
        out = {
            "serial": self.details.get("serial_hash"),
            "voltage_mv": s["vout"],
            "probe_connected": s["probe"],
            "ovc_ma": s["ovc_ma"],
            "calibrated": s["cal_done"],
            "acquiring": s["acquiring"],
            "temperature_c": round(float(s["t_degc"]), 1),
            "errors": errs or None,
        }
        if errs:
            out["hint"] = (
                "OVER_CURRENT_SOURCE means the target drew more than the OVC "
                "limit and the supply cut out; the target is unpowered until "
                "this is cleared. Clear with p1150_clear_error, then "
                "p1150_power_on again with a limit above the target's true "
                "peak. If it tripped at power-on, the cause is almost always "
                "inrush -- a surge of amps lasting a millisecond as bulk "
                "capacitance charges. That is worth measuring rather than "
                "designing around: p1150_inrush_test finds it, and a real "
                "battery would have browned the target out instead of "
                "supplying it. See p1150_inrush_guide."
            ) if "OVER_CURRENT_SOURCE" in errs else \
                "Call p1150_clear_error, then re-check status."
        return out

    def clear_error(self) -> dict:
        dev = self.require()
        ok, _ = dev.clear_error()
        if not ok:
            raise DeviceError("clear_error failed")
        return self.status()

    # ---- acquisition ----------------------------------------------- #

    def _stream_chunk(self, dev) -> tuple:
        """Arm one logger-mode acquisition and wait for its (i, isnk) chunk."""
        self._acq_event.clear()
        ok, r = dev.acquisition_start(PxxxxAPI.ACQUIRE_MODE_LOGGER)
        if not ok:
            raise DeviceError(f"acquisition_start failed: {r}")
        start = timer()
        while not self._acq_event.is_set():
            if timer() - start > CHUNK_TIMEOUT_S:
                raise DeviceError(
                    f"No acquisition data within {CHUNK_TIMEOUT_S}s. "
                    f"Is the probe connected and the target powered?")
            time.sleep(0.005)
        return self._acq_chunk

    def _stream(self, dev, stop: threading.Event,
                max_s: float, chunks: list) -> None:
        """Collect logger chunks until stopped or the cap is hit."""
        dev.set_timebase(STREAM_TIMEBASE)
        collected_s = 0.0
        try:
            while not stop.is_set() and collected_s < max_s:
                chunks.append(self._stream_chunk(dev))
                collected_s += STREAM_CHUNK_S
        finally:
            try:
                dev.acquisition_stop()
            except Exception:
                pass

    def measure(self, duration_s: float,
                connect_probe_during: bool = False) -> tuple:
        """Blocking capture of duration_s seconds.

        Returns (i, isnk, aux); currents in mA, aux keyed by channel name.

        connect_probe_during closes the probe relay *after* streaming has begun,
        which is the only way to capture a target's power-on inrush and boot
        sequence -- by the time a normal capture starts, the boot is over.
        """
        dev = self.require()
        if self._capture_thread is not None:
            raise DeviceError("A background capture is running; "
                              "call p1150_capture_stop first.")
        self._arm_aux()
        chunks = []
        dev.set_timebase(STREAM_TIMEBASE)
        n_chunks = max(1, int(round(duration_s / STREAM_CHUNK_S)))
        try:
            for k in range(n_chunks):
                chunks.append(self._stream_chunk(dev))
                if connect_probe_during and k == 0 and not self.probe_on:
                    dev.probe(connect=True)
                    self.probe_on = True
        finally:
            try:
                dev.acquisition_stop()
            except Exception:
                pass
        return _join(chunks)

    def measure_when(self, duration_s: float, match, timeout_s: float = 60.0,
                     on_reject=None) -> tuple:
        """Stream, discarding until match() accepts a chunk, then capture.

        Returns (i, isnk, aux, waited_s).

        The alternative -- wait in one call, then capture in another -- leaves a
        gap between the two in which the target can leave the state again, and
        on a target that enters a state briefly that gap is most of the time.
        Here the acquisition never stops: the chunk that satisfies the match is
        the first chunk kept, so the capture begins inside the state rather than
        some time after it was last seen.

        match is given one chunk (the raw aux dict) and decides whether the
        target is in the wanted state.  Rejected chunks go to on_reject, which
        is how the caller accumulates what it saw for a timeout message -- this
        layer deliberately knows nothing about how a state is encoded.
        """
        dev = self.require()
        if self._capture_thread is not None:
            raise DeviceError("A background capture is running; "
                              "call p1150_capture_stop first.")
        self._arm_aux()
        if not self._aux:
            raise DeviceError(
                "No auxiliary channel is being recorded, so there is no signal "
                "to wait for. Declare the state signal with "
                "p1150_set_state_signal first.")
        dev.set_timebase(STREAM_TIMEBASE)
        n_chunks = max(1, int(round(duration_s / STREAM_CHUNK_S)))
        chunks, waited = [], 0.0
        try:
            while True:
                chunk = self._stream_chunk(dev)
                if match(chunk):
                    chunks.append(chunk)
                    break
                if on_reject is not None:
                    on_reject(chunk)
                waited += STREAM_CHUNK_S
                if waited >= timeout_s:
                    raise DeviceError(
                        f"The target did not enter the state within "
                        f"{timeout_s:g} s.")
            while len(chunks) < n_chunks:
                chunks.append(self._stream_chunk(dev))
        finally:
            try:
                dev.acquisition_stop()
            except Exception:
                pass
        return _join(chunks) + (waited,)

    def _aux_trigger_level(self, channel: str) -> float:
        """Level to trigger an aux channel at, in that channel's own units."""
        cfg = config.aux_channel_cfg(channel)
        thr = cfg.get("threshold_mv")
        if thr is not None:
            return float(thr)
        if channel == "A0":
            raise DeviceError(
                "A0 is analog, so triggering on it needs a level in millivolts. "
                "Declare it with p1150_set_aux(channel='A0', threshold_mv=...), "
                "or pass trigger_level explicitly.")
        return analysis.digital_threshold()[0]

    @staticmethod
    def effective_fs(timebase: str, n_samples: int) -> int:
        """Samples per second this capture actually came back at.

        Derived from the span and the sample count rather than assumed, because
        spans longer than a second exceed the 125,000-sample acquisition buffer
        and arrive decimated.  A duration read off a decimated capture using the
        nominal rate is wrong by exactly that factor, which for the inrush test
        is the difference between a 1 ms surge and a 10 ms one.
        """
        span = TBASE_SECONDS.get(timebase)
        if not span or n_samples <= 1:
            return SAMPLE_RATE
        fs = n_samples / span
        # Snap to the nominal rate when it is within rounding: the acquisition
        # can be a sample or two short of the full window.
        return SAMPLE_RATE if abs(fs - SAMPLE_RATE) < 0.05 * SAMPLE_RATE \
            else int(round(fs))

    def _acquire_single(self, dev, timeout_s: float, timeout_msg: str,
                        on_armed=None) -> tuple:
        """Arm a one-shot acquisition, optionally act, and wait for the data.

        on_armed runs once the instrument is waiting on its trigger.  That
        ordering is the only way to catch an event the caller itself causes --
        closing the probe relay for a power-on inrush -- because the surge is
        over in a millisecond and there is no second chance at it.
        """
        self._arm_aux()
        self._acq_event.clear()
        ok, r = dev.acquisition_start(PxxxxAPI.ACQUIRE_MODE_SINGLE)
        if not ok:
            raise DeviceError(f"acquisition_start failed: {r}")
        try:
            if on_armed is not None:
                time.sleep(ARM_SETTLE_S)
                on_armed()
            start = timer()
            while not self._acq_event.is_set():
                if timer() - start > timeout_s:
                    raise DeviceError(timeout_msg)
                time.sleep(0.005)
        finally:
            try:
                dev.acquisition_stop()
            except Exception:
                pass
        return _join([self._acq_chunk] if self._acq_chunk else [])

    def capture_single(self, timebase: str, trigger_ma: float = None,
                       position: str = PxxxxAPI.TRIG_POS_LEFT,
                       slope: str = PxxxxAPI.TRIG_SLOPE_RISE,
                       timeout_s: float = 30.0,
                       trigger_on: str = None,
                       trigger_level: float = None) -> tuple:
        """One-shot capture over a timebase span, optionally triggered.

        The trigger source is a current level (trigger_ma) or an auxiliary input
        (trigger_on, one of A0/D0/D1).  An aux trigger is the exact one: the
        target says when its own work begins, instead of the capture guessing
        from a current threshold that a quiet feature may never cross.

        Returns (i, isnk, aux, fs); currents in mA.
        """
        dev = self.require()
        if self._capture_thread is not None:
            raise DeviceError("A background capture is running; "
                              "call p1150_capture_stop first.")
        ok, _ = dev.set_timebase(timebase)
        if not ok:
            raise DeviceError(f"Unknown timebase {timebase}")

        if trigger_on:
            channel = trigger_on.upper()
            if channel not in AUX_TRIG_SRC:
                raise DeviceError(
                    f"trigger_on must be one of "
                    f"{', '.join(AUX_TRIG_SRC)}, got '{trigger_on}'.")
            level = float(trigger_level) if trigger_level is not None \
                else self._aux_trigger_level(channel)
            dev.set_trigger(src=AUX_TRIG_SRC[channel], pos=position,
                            slope=slope, level=level)
            msg = (f"Trigger did not fire within {timeout_s}s on {channel} at "
                   f"{level}. Check that the target really drives that pin, "
                   f"that the lead is on the right one, and that the slope "
                   f"matches the edge the firmware produces. p1150_aux_check "
                   f"shows what the input is actually doing.")
        elif trigger_ma is None:
            dev.set_trigger(src=PxxxxAPI.TRIG_SRC_NONE)
            msg = f"No acquisition data within {timeout_s}s."
        else:
            dev.set_trigger(src=PxxxxAPI.TRIG_SRC_CUR, pos=position,
                            slope=slope, level=float(trigger_ma))
            msg = (f"Trigger did not fire within {timeout_s}s at {trigger_ma} "
                   f"mA. Check the level is above the resting current but "
                   f"below the event peak.")

        i, isnk, aux = self._acquire_single(dev, timeout_s, msg)
        return i, isnk, aux, self.effective_fs(timebase, i.size)

    # ---- inrush ----------------------------------------------------- #

    def inrush_capture(self, voltage_mv: int, ovc_ma: int,
                       timebase: str = PxxxxAPI.TBASE_SPAN_100MS,
                       trigger_ma: float = 20.0,
                       position: str = PxxxxAPI.TRIG_POS_LEFT,
                       timeout_s: float = 15.0) -> tuple:
        """Power the target up while already armed, to catch the inrush.

        The surge exists only in the microseconds after the probe relay closes,
        so the order here is the whole trick: set the voltage, arm a one-shot
        acquisition triggered on current with the probe still open (so nothing
        can trigger it), and only then close the relay.  The instrument is
        waiting when the event arrives.

        This is why measure(connect_probe_during=True) is not good enough for
        inrush: logger mode re-arms between chunks, and the relay closes in one
        of those gaps, so a millisecond-long surge lands in dead time as often
        as not.  It captures the boot sequence fine; it cannot be trusted for
        the surge at the front of it.

        The target is powered down for this and comes back up, by definition --
        there is no way to measure a power-on surge without a power-on.

        Returns (i, isnk, aux, fs, info); currents in mA.
        """
        dev = self.require()
        if self._capture_thread is not None:
            raise DeviceError("A background capture is running; "
                              "call p1150_capture_stop first.")
        if timebase not in TBASE_SECONDS:
            raise DeviceError(f"Unknown timebase {timebase}")

        # Open the relay first: the target has to be unpowered for there to be
        # an inrush to measure, and a target that is already running would
        # simply never trigger.
        was_on = self.probe_on
        if was_on:
            dev.probe(connect=False)
            self.probe_on = False
            # Let the target's rails actually discharge. Re-powering a board
            # whose bulk capacitance is still charged shows a fraction of the
            # real surge, which is the most misleading result available here.
            time.sleep(0.5)

        ok, r = dev.set_vout(int(voltage_mv))
        if not ok:
            raise DeviceError(f"set_vout({voltage_mv}) failed: {r}")
        self.vout_mv = int(voltage_mv)
        ok, r = dev.set_ovc(int(ovc_ma))
        if not ok:
            raise DeviceError(f"set_ovc({ovc_ma}) failed: {r}")
        self.ovc_ma = int(ovc_ma)

        ok, _ = dev.set_timebase(timebase)
        if not ok:
            raise DeviceError(f"set_timebase({timebase}) failed")
        dev.set_trigger(src=PxxxxAPI.TRIG_SRC_CUR, pos=position,
                        slope=PxxxxAPI.TRIG_SLOPE_RISE, level=float(trigger_ma))

        def power_up():
            ok, r = dev.probe(connect=True)
            if not ok:
                raise DeviceError(f"probe connect failed: {r}")
            self.probe_on = True

        i, isnk, aux = self._acquire_single(
            dev, timeout_s,
            f"The target drew less than the {trigger_ma} mA trigger level "
            f"within {timeout_s}s of being powered, so nothing was captured. "
            f"Either the target is not drawing power at all -- check the probe "
            f"contact at the battery terminals, and p1150_self_test -- or its "
            f"start-up current is genuinely below {trigger_ma} mA, which would "
            f"mean it has no inrush worth worrying about. Retry with a lower "
            f"trigger_ma to confirm.",
            on_armed=power_up)

        info = {"voltage_mv": self.vout_mv, "ovc_ma": self.ovc_ma,
                "timebase": timebase, "trigger_ma": trigger_ma,
                "probe_was_connected": was_on}
        # An over-current trip latches on the device and is invisible in the
        # samples, which just stop rising. Without this the capture reads as a
        # clean 3.2 A peak rather than as the supply cutting out.
        try:
            st = self.status()
            info["device_errors"] = st.get("errors")
            info["ovc_tripped"] = bool(st.get("errors") and
                                       "OVER_CURRENT_SOURCE" in st["errors"])
            info["probe_connected"] = st.get("probe_connected")
        except Exception:
            pass
        return i, isnk, aux, self.effective_fs(timebase, i.size), info

    def self_test(self) -> dict:
        """Measure the P1150's own calibration resistors as a known load.

        Answers the question that otherwise stalls a debugging session: is the
        instrument fine and the target genuinely drawing nothing, or is the
        probe not making contact?  The internal resistors are switched in
        without involving the probe at all, so a good result here isolates the
        fault to the probe or the target.
        """
        dev = self.require()
        if self._capture_thread is not None:
            raise DeviceError("A background capture is running.")
        if self.vout_mv is None:
            dev.set_vout(4000)
            self.vout_mv = 4000

        ok, _ = dev.set_cal_sweep(sweep=True)
        if not ok:
            raise DeviceError("set_cal_sweep failed")
        try:
            time.sleep(0.05)   # let the first resistor settle
            i, isnk, _ = self.measure(1.0)
        finally:
            dev.set_cal_sweep(sweep=False)
        return {"current_ma": i, "sink_ma": isnk, "voltage_mv": self.vout_mv}

    # ---- background capture ---------------------------------------- #

    def capture_start(self, label: str, max_s: float = None) -> dict:
        dev = self.require()
        if self._capture_thread is not None:
            raise DeviceError(
                f"A capture ({self._capture['label']}) is already running.")

        max_s = float(max_s or DEFAULT_MAX_CAPTURE_S)
        aux = self._arm_aux()
        chunks = []
        self._capture = {"label": label, "chunks": chunks,
                         "started": timer(), "max_s": max_s, "aux": aux}
        self._capture_err = None
        self._capture_stop.clear()

        def run():
            try:
                self._stream(dev, self._capture_stop, max_s, chunks)
            except Exception as e:
                self._capture_err = str(e)

        self._capture_thread = threading.Thread(target=run, daemon=True)
        self._capture_thread.start()
        return {"capturing": True, "label": label, "max_duration_s": max_s,
                "aux_channels": aux or None}

    def capture_status(self) -> dict:
        if self._capture_thread is None:
            return {"capturing": False}
        c = self._capture
        n = sum(len(x["i"]) for x in c["chunks"])
        return {
            "capturing": self._capture_thread.is_alive(),
            "label": c["label"],
            "elapsed_s": round(timer() - c["started"], 2),
            "samples": n,
            "captured_s": round(n / SAMPLE_RATE, 3),
            "max_duration_s": c["max_s"],
            "aux_channels": c.get("aux") or None,
            "error": self._capture_err,
        }

    def capture_stop_raw(self) -> tuple:
        """Stop the background capture; returns (label, i, isnk, aux)."""
        if self._capture_thread is None:
            raise DeviceError("No capture is running.")
        self._capture_stop.set()
        self._capture_thread.join(timeout=CHUNK_TIMEOUT_S + 2)
        self._capture_thread = None
        c, self._capture = self._capture, None
        if self._capture_err:
            raise DeviceError(f"Capture failed: {self._capture_err}")
        i, isnk, aux = _join(c["chunks"])
        return c["label"], i, isnk, aux


SESSION = Session()
