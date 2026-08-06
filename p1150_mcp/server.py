# -*- coding: utf-8 -*-
"""
MIT License

MCP server exposing a P1150 to an AI agent for battery-current work.

Run it with:   python -m p1150_mcp
Configured by Claude Code (or any MCP client) through .mcp.json.

Design notes, since this is not a 1:1 wrapper of the driver:

* A capture is 125,000 samples/second.  Ten seconds is 1.25 million numbers,
  which cannot be returned to a language model.  So samples are written to disk
  and every tool returns a summary of at most a few hundred values.  run_id is
  the handle the agent passes around.

* Sequences with a mandatory order (vout before probe, timebase before
  acquisition, stop before close) are collapsed into single tools.  An agent
  should be choosing *what* to measure, not re-deriving the driver's protocol.

* The device connection is held open across tool calls, so a developer can power
  the target once and then edit, flash, and measure repeatedly without power
  cycling in between.
"""
import os

import numpy as np

# The SDK renamed FastMCP to MCPServer in mcp 2.0; the decorator and run()
# surface used here is identical, so accept either.
try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                                  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _Server

from . import analysis, storage, config
from .device import SESSION, SAMPLE_RATE
from .scenarios import GUIDE

mcp = _Server("p1150")

# Serial number default so a developer can configure the bench once, in
# .mcp.json, instead of restating it in every conversation.  Battery capacity
# lives in config.py instead: it is a property of the project being measured,
# is asked for interactively, and persists once set.
DEFAULT_SN = os.environ.get("P1150_SN") or None


def _fail(e: Exception) -> dict:
    """Errors come back as data, not exceptions: the agent should read the
    message and correct itself rather than see an opaque tool failure."""
    return {"error": str(e)}


def _store(label: str, i_ma: np.ndarray, extra: dict = None,
           isnk_ma: np.ndarray = None) -> dict:
    """Persist a capture and return the summary the agent actually sees."""
    if i_ma.size == 0:
        return {"error": "Capture returned no samples."}
    battery = config.capacity_mah()
    summary = analysis.summarize(i_ma, SAMPLE_RATE, battery)
    meta = dict(summary)
    meta.update(extra or {})
    meta["voltage_mv"] = SESSION.vout_mv
    run_id = storage.save(label, i_ma, meta, isnk_ma=isnk_ma)
    out = {"run_id": run_id, "label": label}
    out.update(summary)
    out.update(extra or {})
    if not battery:
        out["hint"] = ("Battery capacity is not configured, so battery life and "
                       "percentage-of-battery figures are unavailable. Ask the "
                       "developer for the pack's mAh rating and record it with "
                       "p1150_set_battery.")
    return out


# ------------------------------------------------------------------ #
# Guide                                                                #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_measurement_guide() -> str:
    """How to measure battery current well with a P1150.

    Read this before the first measurement in a conversation. Covers: choosing a
    supply voltage and over-current limit, the five common current profiles
    (sleep floor, boot inrush, periodic wake-up, single triggered event,
    scripted regression run), which tool and capture length suits each, how to
    read a regression result, and the mistakes that produce measurements that
    look fine but mean nothing.
    """
    return GUIDE


# ------------------------------------------------------------------ #
# Project setup                                                        #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_set_battery(capacity_mah: float, chemistry: str = None,
                      nominal_mv: int = None) -> dict:
    """Record the capacity of the battery this target runs on, in mAh.

    ASK THE DEVELOPER FOR THIS at the start of a project, before the first
    measurement -- it cannot be inferred from a current waveform or from the
    code, and without it the measurements stay abstract. With it, every result
    gains the numbers people actually act on: how long the device lasts, what
    share of the battery one wake-up or one boot costs, how many times an
    operation can run before the pack is flat, and whether a measured charging
    current is a sensible C rate.

    The setting persists across sessions, so it only needs asking once per
    project. Call p1150_get_battery to see what is currently set.

    capacity_mah: the pack's rated capacity, e.g. 220 for a small LiPo, 2000 for
        an 18650, 3000 for a phone-sized cell, 225 for a CR2032 coin cell.
    chemistry: optional, e.g. "Li-ion", "LiPo", "LiFePO4", "alkaline", "NiMH".
    nominal_mv: optional nominal cell voltage, useful as a reminder of what to
        pass to p1150_power_on.
    """
    try:
        if capacity_mah <= 0:
            return {"error": "capacity_mah must be greater than zero."}
        cfg = config.set_battery(capacity_mah, chemistry, nominal_mv)
        cfg["note"] = ("Recorded. Battery life, percentage-of-battery and C rate "
                       "figures are now included in measurement results.")
        return cfg
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_get_battery() -> dict:
    """Show the battery capacity currently configured for this project.

    If nothing is set, ask the developer for the pack's mAh rating and record it
    with p1150_set_battery.
    """
    try:
        cfg = config.get()
        if not cfg.get("capacity_mah"):
            return {"configured": False,
                    "action": "Ask the developer what capacity battery (in mAh) "
                              "the target runs on, then call p1150_set_battery."}
        cfg["configured"] = True
        return cfg
    except Exception as e:
        return _fail(e)


# ------------------------------------------------------------------ #
# Device                                                               #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_list_devices() -> dict:
    """List P1150 units attached to this machine, without disturbing them.

    Safe to call at any time: it does not connect, power anything, or change the
    state of a P1150 that is already in use. Use it to discover a serial number
    for p1150_connect.
    """
    try:
        from pxxxx import PXXXX
        ports = PXXXX.list_ports()
        devices = []
        for port in ports:
            d = PXXXX(port=port)
            ok, r = d.ping()
            if ok:
                p = r[-1]
                devices.append({
                    "serial": p["serial_hash"], "port": port,
                    "model": p["model"], "firmware": p["version"],
                    "application": p["app"],
                    "note": "in bootloader; p1150_connect will load the "
                            "application firmware (~15 s)"
                            if p["app"] == "a51" else None,
                })
            d.close()
        return {"count": len(devices), "devices": devices,
                "default_sn": DEFAULT_SN}
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_connect(sn: str = None) -> dict:
    """Connect to a P1150 by serial number and prepare it for measurement.

    The serial number is printed on the back of the unit; p1150_list_devices
    reports it too. Omit sn to use the P1150_SN environment default if one is
    configured.

    The first connection after the P1150 is plugged in takes about 15 seconds:
    the application firmware is loaded and the unit self-calibrates. This is
    normal, not a hang. Later connections are fast.

    The connection stays open for the rest of the session, so the target can
    stay powered across many measurements while firmware is edited and
    re-flashed. Nothing is powered until p1150_power_on is called.
    """
    try:
        target = sn or DEFAULT_SN
        if not target:
            return {"error": "No serial number given and P1150_SN is not set. "
                             "Call p1150_list_devices to find one."}
        return SESSION.connect(target)
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_disconnect() -> dict:
    """Disconnect the probe, stop any capture, and release the P1150.

    This removes power from the target. Call it when measurement work is
    finished, or to hand the P1150 back to the desktop GUI -- only one program
    can own the unit at a time.
    """
    try:
        return SESSION.disconnect()
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_status() -> dict:
    """Report supply voltage, probe state, over-current limit, temperature and
    any active error on the connected P1150.

    Error bits are decoded to names. OVER_CURRENT_SOURCE means the target drew
    more than the configured limit and the supply shut off -- the target is
    unpowered until it is cleared. Call this whenever a measurement looks wrong.
    """
    try:
        return SESSION.status()
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_clear_error() -> dict:
    """Clear a latched P1150 error (typically an over-current trip).

    After clearing, call p1150_power_on again to restore power to the target.
    If the trip was genuine, raise ovc_ma to above the target's true peak
    current first, or it will simply trip again.
    """
    try:
        return SESSION.clear_error()
    except Exception as e:
        return _fail(e)


# ------------------------------------------------------------------ #
# Power                                                                #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_self_test() -> dict:
    """Verify the P1150 measures correctly, using its own internal calibration
    resistors as a known load.

    Use this when a measurement reads zero, reads implausibly low, or otherwise
    looks wrong. The internal resistors are switched in without going through
    the probe, so a PASS here means the instrument is fine and the fault is in
    the probe connection or the target -- which is the distinction that
    otherwise costs an hour of debugging.

    A PASS expects a sweep spanning microamps up to well over 50 mA. The probe
    is left exactly as it was, and an output voltage already set by
    p1150_power_on is kept; 4000 mV is used only if none has been set yet.
    """
    try:
        r = SESSION.self_test()
        i = r["current_ma"]
        s = analysis.summarize(i, SAMPLE_RATE)
        # The sweep steps through decade resistors, so a working instrument
        # spans several decades. A flat or tiny reading means it does not.
        span_ok = s["peak_ma"] > 50.0 and s["sleep_floor_ma"] < 1.0
        return {
            "result": "PASS" if span_ok else "FAIL",
            "voltage_mv": r["voltage_mv"],
            "measured_floor_ma": s["sleep_floor_ma"],
            "measured_peak_ma": s["peak_ma"],
            "expected": "floor below 1 mA and peak above 50 mA",
            "note": "Instrument measures correctly. If a target still reads "
                    "zero, check the probe contact at the battery terminals "
                    "and that p1150_power_on has been called."
                    if span_ok else
                    "The P1150 did not measure its own known load correctly. "
                    "Check p1150_status for errors and re-run p1150_connect.",
        }
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_power_on(voltage_mv: int, ovc_ma: int = 500) -> dict:
    """Power the target: set the supply voltage and current limit, then connect
    the probe.

    THE VOLTAGE GOES DIRECTLY TO THE TARGET'S BATTERY TERMINALS. Confirm the
    value with the developer before calling this the first time in a session --
    too high will destroy the target, and there is no undo. Ask rather than
    infer from context.

    voltage_mv: what the battery would supply, in millivolts. Li-ion single cell
        is 4200 (full) / 3700 (nominal) / 3300 (nearly flat); 2x alkaline is
        ~3000; a coin cell is ~3000. Use the same value for every run that will
        be compared -- current draw varies with supply voltage.

    ovc_ma: over-current limit. Must exceed the target's true PEAK current, not
        its average. A radio transmit or motor start can be 20x the average; an
        OVC set near the average trips instantly and browns the target out,
        which is easy to misread as a firmware fault. 500 mA suits most
        low-power boards; raise it if the target legitimately draws more.

    Power stays on until p1150_power_off or p1150_disconnect, so firmware can be
    re-flashed over JTAG between measurements without cycling power.
    """
    try:
        return SESSION.power_on(voltage_mv, ovc_ma)
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_power_off() -> dict:
    """Disconnect the probe, removing power from the target.

    Use before physically handling the target. To measure the target's power-on
    inrush and boot sequence, call this, then use p1150_measure with
    connect_probe_during=True.
    """
    try:
        return SESSION.power_off()
    except Exception as e:
        return _fail(e)


# ------------------------------------------------------------------ #
# Measurement                                                          #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_measure(duration_s: float, label: str,
                  connect_probe_during: bool = False) -> dict:
    """Capture battery current for a fixed number of seconds, then summarise it.

    Blocks for duration_s, so keep it under about 60 seconds; for an open-ended
    run ("start recording, I'll flash and run the firmware, then stop") use
    p1150_capture_start and p1150_capture_stop instead.

    label: a short name for this run, e.g. "baseline" or "after-sleep-fix". It
        becomes part of the run_id used by p1150_compare, so make it meaningful.

    connect_probe_during: start recording BEFORE powering the target, to capture
        power-on inrush and the boot sequence. Requires the probe to be off
        (call p1150_power_off first). Without this, recording starts after power
        is already applied and the whole boot is missed.

    Choosing a duration: sleep-floor measurements want 10-60 s. A duty-cycled
    profile needs at least 10 full wake-up periods -- 15 s minimum for something
    advertising once per second. Anything under about 2 s only characterises
    whatever happened to be running at that instant.

    Returns a run_id plus headline metrics: average current (mA), accumulated
    charge (mAh), resting floor, peak, and percentiles. The samples themselves
    are kept on disk; use p1150_segment, p1150_events and p1150_compare to
    examine them further.
    """
    try:
        i, isnk = SESSION.measure(duration_s, connect_probe_during)
        return _store(label, i, {"capture_type": "timed"}, isnk_ma=isnk)
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_capture_start(label: str, max_duration_s: float = None) -> dict:
    """Begin recording battery current in the background and return immediately.

    This is the tool for the edit-flash-run loop: start the capture, let the
    developer flash and exercise the firmware for as long as the workload takes,
    then call p1150_capture_stop to end it and get the summary. Recording keeps
    running across other messages in the conversation.

    While a background capture is running the P1150 is busy: do not call
    p1150_status, p1150_power_on or p1150_measure until it is stopped. Use
    p1150_capture_status to check on it.

    max_duration_s caps the recording so a forgotten capture cannot consume all
    memory (default 900 s, about 450 MB). Recording stops on its own at the cap;
    p1150_capture_stop still has to be called to save it.
    """
    try:
        return SESSION.capture_start(label, max_duration_s)
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_capture_status() -> dict:
    """Check on a background capture: elapsed time, samples collected, and
    whether it is still recording.

    Safe to call while a capture is in flight -- it reads local state and does
    not touch the P1150.
    """
    try:
        return SESSION.capture_status()
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_capture_stop() -> dict:
    """Stop the background capture, save it, and return its summary.

    Returns a run_id plus average current (mA), accumulated charge (mAh),
    resting floor and peak. Pass the run_id to p1150_compare to check it against
    a baseline, or to p1150_segment / p1150_events to see where the energy went.
    """
    try:
        label, i, isnk = SESSION.capture_stop_raw()
        return _store(label, i, {"capture_type": "background"},
                      isnk_ma=isnk)
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_verify_charging(duration_s: float = 10.0,
                          label: str = "charge-test") -> dict:
    """Verify that the target actually charges its battery, and at what rate.

    SETUP: the P1150 must be connected at the battery terminals as usual (via
    p1150_power_on, standing in for the battery), and the developer then applies
    the target's own charging source -- USB, a wall adapter, wireless, solar.
    Ask them to confirm the charger is attached and enabled before calling this.

    Because the P1150 sits exactly where the battery would, what it measures IS
    what the battery would see: the charger's output minus whatever the rest of
    the target is drawing at that moment. That net is the only thing a single
    pair of battery terminals can carry, so no separation of the two is needed
    or possible.

    Three outcomes:
      CHARGING       current flows into the battery. Reports the net charge
                     current, the C rate, and an estimated time to full.
      NET_DISCHARGING a charger is present but the target consumes more than it
                     delivers -- the battery drains slower but never fills.
                     Common when measuring with the radio active, or with a
                     charger current limit set too low.
      NOT_CHARGING   no current flows in at all: charger not connected or not
                     enabled, charger IC not running, or an open charge path.

    Charge rate is reported as a C rate against the configured battery capacity:
    1C fills the pack in about an hour and is what most designs aim for. Set the
    capacity with p1150_set_battery first, or the C rate and time-to-full cannot
    be computed.

    Use at least 10 s; a charger that cycles on and off needs 30-60 s before the
    intermittency is visible.
    """
    try:
        i, isnk = SESSION.measure(duration_s)
        battery = config.capacity_mah()
        out = analysis.charge_test(i, isnk, SAMPLE_RATE, battery)
        stored = _store(label, i, {"capture_type": "charge_test"}, isnk_ma=isnk)
        out["run_id"] = stored.get("run_id")
        out["label"] = label
        # A sink over-current trip is latched on the device, not visible in the
        # samples, and would silently truncate the charge current being measured.
        try:
            st = SESSION.status()
            if st.get("errors"):
                out["device_errors"] = st["errors"]
                if "OVER_CURRENT_SINK" in st["errors"]:
                    out["warning"] = (
                        "The P1150's sink current limit tripped during this "
                        "measurement, so the charge current shown is capped by "
                        "the instrument, not by the target. Raise ovc_ma via "
                        "p1150_power_on and re-run.")
        except Exception:
            pass
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_charge_summary(run_id: str) -> dict:
    """Re-run the charging analysis on a stored capture.

    Only works for runs recorded with the sink channel; a run captured before
    that was retained reports an error rather than a misleading result.
    """
    try:
        i, isnk, meta = storage.load_full(run_id)
        if isnk is None:
            return {"error": f"Run '{run_id}' has no sink-current channel, so "
                             f"charging cannot be assessed. Re-capture with "
                             f"p1150_verify_charging."}
        out = analysis.charge_test(i, isnk, SAMPLE_RATE, config.capacity_mah())
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_capture_single(label: str, timebase: str = "TBASE_SPAN_100MS",
                         trigger_ma: float = None,
                         position: str = "TRIG_POS_LEFT",
                         slope: str = "TRIG_SLOPE_RISE",
                         timeout_s: float = 30.0) -> dict:
    """Capture a single event, optionally waiting for a current threshold.

    Use this to look closely at one thing -- a radio transmit, a flash write, a
    sensor read, a wake-up burst -- rather than to characterise a workload.

    timebase: the window length. One of TBASE_SPAN_10MS, 20MS, 50MS, 100MS,
        200MS, 500MS, 1S, 2S, 5S, 10S. Pick the shortest span that still
        contains the whole event: 10-20 ms for a BLE transmit, 100 ms - 1 s for
        a flash erase or sensor warm-up. Too short clips the event; too long
        buries it in idle time.

    trigger_ma: capture starts when current crosses this level, in mA. Set it
        between the resting current and the event's peak -- a good first guess
        is a few times the resting floor. Omit for an untriggered capture that
        starts immediately. If the trigger never fires within timeout_s, the
        level is above the actual peak.

    position: where the trigger sits in the window. TRIG_POS_LEFT records mostly
        after the event starts; TRIG_POS_CENTER also shows what led up to it,
        which is what you want when diagnosing an unexpected current spike.
    """
    try:
        i, isnk = SESSION.capture_single(timebase, trigger_ma, position,
                                        slope, timeout_s)
        return _store(label, i, {"capture_type": "single",
                                 "timebase": timebase,
                                 "trigger_ma": trigger_ma}, isnk_ma=isnk)
    except Exception as e:
        return _fail(e)


# ------------------------------------------------------------------ #
# Analysis                                                             #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_list_runs(limit: int = 25) -> dict:
    """List stored captures, newest first, with their headline numbers.

    Runs persist between sessions, so a baseline recorded days ago is still here
    to compare against.
    """
    try:
        return {"runs": storage.list_runs(limit),
                "runs_dir": storage.runs_dir()}
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_summary(run_id: str) -> dict:
    """Headline metrics for a stored run: average current (mA), accumulated
    charge (mAh), resting floor, peak, percentiles, and -- if a battery capacity
    is configured -- projected battery life.
    """
    try:
        i, meta = storage.load(run_id)
        out = analysis.summarize(i, SAMPLE_RATE, config.capacity_mah())
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_segment(run_id: str) -> dict:
    """Break a run down by current level: how much TIME and how much CHARGE was
    spent in each band, from deep sleep (<10 uA) up to peak (>100 mA).

    This is the "where did the energy go" view, and it is usually the first
    thing to look at. A target can spend 99% of its time asleep and still burn
    most of its battery in 1% of the time spent transmitting -- average current
    alone cannot show that, and it changes which code is worth optimising.
    """
    try:
        i, meta = storage.load(run_id)
        out = analysis.segment(i, SAMPLE_RATE, config.capacity_mah())
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_events(run_id: str, threshold_ma: float = None,
                 min_duration_us: float = 100.0) -> dict:
    """Find the wake-up bursts in a duty-cycled run and characterise them.

    Reports how often the target wakes, how long it stays awake, the charge each
    wake costs (uAh), the burst peak, and the duty cycle. These are the three
    independent things that drive battery life; average current is just their
    product, so it cannot tell you which one changed.

    threshold_ma: the level separating "asleep" from "awake". Defaults to 10% of
        the way from the resting floor to the burst level, which works for most
        targets. Set it explicitly if the result reports zero events but the
        target is known to be duty-cycled, or if it reports implausibly many.

    min_duration_us: bursts shorter than this are ignored as noise.
    """
    try:
        i, meta = storage.load(run_id)
        out = analysis.find_events(i, SAMPLE_RATE, threshold_ma,
                                   min_duration_us, config.capacity_mah())
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_compare(baseline_run_id: str, candidate_run_id: str,
                  threshold_pct: float = 5.0) -> dict:
    """Compare two runs and report whether battery current has regressed.

    The main tool for "did my code change cost battery life". Returns a verdict
    (PASS / REGRESSION / IMPROVEMENT) against threshold_pct on average current,
    per-metric deltas, burst statistics for both runs, and a plain-language
    hypothesis about the cause -- a sleep floor that rose points somewhere
    completely different from bursts that got longer.

    For the comparison to mean anything both runs must use the same supply
    voltage, the same workload, and the same duration. If the durations differ
    by more than 5% the accumulated-charge comparison is withheld (a longer run
    trivially accumulates more mAh) and a warning is returned; average current
    stays valid.

    If two back-to-back baselines differ by more than threshold_pct, the
    workload is not repeatable and no single comparison should be trusted.
    """
    try:
        b, mb = storage.load(baseline_run_id)
        c, mc = storage.load(candidate_run_id)
        out = analysis.compare(b, c, SAMPLE_RATE, threshold_pct,
                               config.capacity_mah())
        out["baseline_run"] = {"run_id": baseline_run_id,
                               "label": mb.get("label"),
                               "voltage_mv": mb.get("voltage_mv")}
        out["candidate_run"] = {"run_id": candidate_run_id,
                                "label": mc.get("label"),
                                "voltage_mv": mc.get("voltage_mv")}
        if mb.get("voltage_mv") and mc.get("voltage_mv") and \
                mb["voltage_mv"] != mc["voltage_mv"]:
            out["warning"] = (
                f"Runs used different supply voltages "
                f"({mb['voltage_mv']} mV vs {mc['voltage_mv']} mV). Current "
                f"draw depends on supply voltage, so this comparison is not "
                f"valid. Re-run both at the same voltage.")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_plot(run_id: str, path: str = None, log_scale: bool = True) -> dict:
    """Render a run as a PNG current-vs-time plot and return the file path.

    Useful when the numbers are ambiguous and the shape of the waveform settles
    it -- read the image back to see the profile directly. Log scale is the
    default because a battery profile spans microamps to milliamps, and a linear
    axis flattens the sleep floor into the baseline.

    Waveforms are decimated to a few thousand points for the plot; the stored
    samples are untouched.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        i, meta = storage.load(run_id)
        n = i.size
        # Min/max decimation rather than striding: a 1 ms burst inside a 30 s
        # capture would fall between strided samples and vanish from the plot,
        # which is exactly the feature the developer is looking for.
        target = 4000
        step = max(1, n // target)
        if step > 1:
            trim = (n // step) * step
            blocks = i[:trim].reshape(-1, step)
            lo, hi = blocks.min(axis=1), blocks.max(axis=1)
            y = np.empty(lo.size * 2, dtype=np.float32)
            y[0::2], y[1::2] = lo, hi
            x = np.linspace(0, trim / SAMPLE_RATE, y.size)
        else:
            y, x = i, np.arange(n) / SAMPLE_RATE

        if log_scale:
            y = np.maximum(y, 1e-4)  # keep zeros off a log axis

        plt.figure(figsize=(11, 4.5))
        plt.plot(x, y, linewidth=0.6)
        if log_scale:
            plt.yscale("log")
        plt.xlabel("Time (s)")
        plt.ylabel("Current (mA)")
        plt.title(f"{meta.get('label', run_id)}  --  "
                  f"avg {meta.get('avg_ma')} mA, {meta.get('charge_mah')} mAh")
        plt.grid(True, "both", color="#ddddee")
        plt.tight_layout()

        out = path or os.path.join(storage.runs_dir(), run_id + ".png")
        plt.savefig(out, dpi=110)
        plt.close()
        return {"run_id": run_id, "path": out}
    except Exception as e:
        return _fail(e)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
