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

# Two channels at 125k samples/s * 4 bytes = 1 MB/s retained.  The cap stops an
# agent that forgets to call capture_stop from consuming all memory: the 900 s
# default is ~900 MB.  Lower P1150_MAX_CAPTURE_S on a memory-constrained bench.
DEFAULT_MAX_CAPTURE_S = float(os.environ.get("P1150_MAX_CAPTURE_S") or 900)


class DeviceError(RuntimeError):
    pass


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

    def connect(self, sn: str, calibrate: bool = True) -> dict:
        if self.is_connected():
            raise DeviceError(
                f"Already connected to {self.details.get('serial_hash')}. "
                f"Call p1150_disconnect first.")

        port = PXXXX.get_port_from_sn(sn)
        if port is None:
            attached = PXXXX.list_ports()
            raise DeviceError(
                f"No P1150 with serial number {sn}. "
                f"Ports with a P1150 attached: {attached or 'none'}")

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
            ok, details = dev.ez_connect(calibrate=calibrate)
            if not ok:
                dev.close()
                raise DeviceError(f"ez_connect failed on {port}: {details}")
            if details.get("app") == "a43":
                self.dev = dev
                break
            dev.close()          # still the bootloader; release the port
            attempts -= 1

        if self.dev is None:
            raise DeviceError(
                f"P1150 on {port} stayed in the bootloader after "
                f"{CONNECT_ATTEMPTS} attempts. Power cycle it and retry.")

        self.port = port
        self.details = details
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
                "limit and the supply cut out; raise ovc_ma if the target "
                "legitimately needs it. Clear with p1150_clear_error, then "
                "p1150_power_on again."
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

        Returns (i, isnk, aux); currents in mA.
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
        elif trigger_ma is None:
            dev.set_trigger(src=PxxxxAPI.TRIG_SRC_NONE)
        else:
            dev.set_trigger(src=PxxxxAPI.TRIG_SRC_CUR, pos=position,
                            slope=slope, level=float(trigger_ma))

        self._arm_aux()
        self._acq_event.clear()
        ok, r = dev.acquisition_start(PxxxxAPI.ACQUIRE_MODE_SINGLE)
        if not ok:
            raise DeviceError(f"acquisition_start failed: {r}")
        start = timer()
        try:
            while not self._acq_event.is_set():
                if timer() - start > timeout_s:
                    raise DeviceError(
                        f"Trigger did not fire within {timeout_s}s on "
                        f"{channel} at {level}. Check that the target really "
                        f"drives that pin, that the lead is on the right one, "
                        f"and that the slope matches the edge the firmware "
                        f"produces. p1150_aux_check shows what the input is "
                        f"actually doing."
                        if trigger_on else
                        f"Trigger did not fire within {timeout_s}s at "
                        f"{trigger_ma} mA. Check the level is above the "
                        f"resting current but below the event peak.")
                time.sleep(0.005)
        finally:
            try:
                dev.acquisition_stop()
            except Exception:
                pass
        return _join([self._acq_chunk] if self._acq_chunk else [])

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
