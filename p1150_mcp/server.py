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
import queue
import threading

import anyio
import numpy as np

# The SDK renamed FastMCP to MCPServer in mcp 2.0; the decorator, Context and
# run() surface used here is identical, so accept either.
try:
    from mcp.server.mcpserver import MCPServer as _Server, Context
except ImportError:                                  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _Server, Context

from . import analysis, storage, config
from .device import SESSION, SAMPLE_RATE, scan
from .scenarios import GUIDE, MARKER_GUIDE, INRUSH_GUIDE

mcp = _Server("p1150")

# Serial number default so a developer can configure the bench once, in
# .mcp.json, instead of restating it in every conversation.  Battery capacity
# lives in config.py instead: it is a property of the project being measured,
# is asked for interactively, and persists once set.
DEFAULT_SN = os.environ.get("P1150_SN") or None


# How often the event loop looks for a progress message from the worker
# thread.  Fast enough that a bar looks live, slow enough to cost nothing over
# the fifteen seconds it runs for.
PROGRESS_POLL_S = 0.05


def _fail(e: Exception) -> dict:
    """Errors come back as data, not exceptions: the agent should read the
    message and correct itself rather than see an opaque tool failure."""
    return {"error": str(e)}


async def _report(ctx, pct: int, message: str) -> None:
    """Send one progress notification, tolerating whatever is at the far end.

    Progress is decoration.  It must not be able to fail a connection, so every
    way it can go wrong ends here: a client that never asked for progress (the
    SDK makes it a no-op), one that has gone away mid-call, or an installed
    mcp older than the message argument, which is why a TypeError falls back to
    the bare two-argument form rather than giving up.
    """
    if ctx is None:
        return
    try:
        await ctx.report_progress(pct, 100, message)
    except TypeError:
        try:
            await ctx.report_progress(pct, 100)
        except Exception:
            pass
    except Exception:
        pass


async def _with_progress(ctx, work):
    """Run a blocking device call on a worker thread, relaying its progress.

    The driver reports progress by calling back from inside the ctypes call, on
    whatever thread made it, and that call does not return for fifteen seconds
    on a P1150 that is still in its bootloader.  Left on the event loop it would
    block the very notifications it is producing, so it goes to a thread and
    hands (percent, message) pairs back over a queue.  Only this coroutine
    talks to the MCP session; the worker only ever touches the instrument.
    """
    q = queue.Queue()
    out = {}

    def runner():
        try:
            out["result"] = work(lambda pct, msg: q.put((pct, msg)))
        except Exception as e:
            out["error"] = e
        finally:
            q.put(None)      # sentinel: the work is over, stop draining

    thread = threading.Thread(target=runner, daemon=True, name="p1150-progress")
    thread.start()
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            await anyio.sleep(PROGRESS_POLL_S)
            continue
        if item is None:
            break
        await _report(ctx, *item)
    thread.join()
    if "error" in out:
        raise out["error"]
    return out["result"]


def _powered_up_during(meta: dict) -> bool:
    """Did this capture begin with the target unpowered?

    It decides how a surge at the front of the capture is read: the power-up
    one, which happens once when the battery is fitted, or a rail being
    switched, which happens forever.  Recorded at capture time rather than
    guessed from the samples, because a target asleep at a few microamps looks
    exactly like one that is not powered at all.
    """
    meta = meta or {}
    return bool(meta.get("capture_type") == "inrush" or
                meta.get("connect_probe_during"))


def _fs(meta: dict) -> int:
    """The rate a stored run was actually sampled at.

    Captures over a timebase longer than one second come back decimated, so a
    duration computed from the nominal 125 kSps would be wrong by that factor.
    Runs stored before the rate was recorded were all at the nominal rate.
    """
    return int((meta or {}).get("sample_rate") or SAMPLE_RATE)


def _store(label: str, i_ma: np.ndarray, extra: dict = None,
           isnk_ma: np.ndarray = None, aux: dict = None,
           fs: int = SAMPLE_RATE) -> dict:
    """Persist a capture and return the summary the agent actually sees."""
    if i_ma.size == 0:
        return {"error": "Capture returned no samples."}
    battery = config.capacity_mah()
    summary = analysis.summarize(i_ma, fs, battery)
    meta = dict(summary)
    meta.update(extra or {})
    meta["voltage_mv"] = SESSION.vout_mv
    meta["ovc_ma"] = SESSION.ovc_ma
    meta["sample_rate"] = int(fs)
    if aux:
        # Record the levels in force at capture time. They can be overridden
        # later, but a run must still be readable after the project's aux setup
        # has moved on.
        meta["aux_channels"] = list(aux)
        meta["aux_config"] = {c: config.aux_channel_cfg(c) for c in aux}
        meta["marker_channel"] = config.aux_primary()
    run_id = storage.save(label, i_ma, meta, isnk_ma=isnk_ma, aux=aux)
    out = {"run_id": run_id, "label": label}
    out.update(summary)
    out.update(extra or {})
    if aux:
        out.update(_marker_headline(aux, meta, run_id))
    # Every capture is screened for an inrush surge, whatever it was taken for.
    # A developer measuring battery life has no reason to ask about inrush and
    # no way to see it without an instrument at the battery terminals -- so a
    # capture that contains one has to say so unprompted, or it never comes up
    # until the field returns start.
    try:
        warn = analysis.inrush_screen(i_ma, fs, SESSION.ovc_ma,
                                      power_on_capture=_powered_up_during(meta))
        if warn:
            warn["inrush_hint"] = (
                f"p1150_inrush_check('{run_id}') measures the surge and "
                f"estimates whether a real battery would sag far enough to "
                f"reset the target. p1150_inrush_guide explains why it matters.")
            out.update(warn)
    except Exception:
        pass
    if not battery:
        out["hint"] = ("Battery capacity is not configured, so battery life and "
                       "percentage-of-battery figures are unavailable. Ask the "
                       "developer for the pack's mAh rating and record it with "
                       "p1150_set_battery.")
    return out


# ------------------------------------------------------------------ #
# Marker helpers                                                       #
# ------------------------------------------------------------------ #
def _resolve_marker(aux: dict, channel: str = None, meta: dict = None):
    """Choose an aux channel and decode it to a per-sample assertion mask.

    The current project configuration wins over whatever was in force when the
    run was captured, because the usual reason to re-analyse a run is that the
    threshold or the polarity was wrong the first time.
    """
    meta = meta or {}
    if not aux:
        raise ValueError(
            "This run has no auxiliary channel recorded. Declare one with "
            "p1150_set_aux and capture again -- aux channels are only retained "
            "when the project has asked for them.")
    ch = (channel or meta.get("marker_channel") or config.aux_primary()
          or next(iter(aux))).upper()
    if ch not in aux:
        raise ValueError(
            f"Run has no '{ch}' channel. Recorded: {', '.join(aux) or 'none'}.")
    cfg = config.aux_channel_cfg(ch) or (meta.get("aux_config") or {}).get(ch) or {}
    return ch, cfg, analysis.to_logic(aux[ch], cfg)


def _marker_headline(aux: dict, meta: dict = None, run_id: str = None) -> dict:
    """The one line about markers that belongs on every capture result."""
    try:
        ch, cfg, asserted = _resolve_marker(aux, None, meta)
    except Exception:
        return {"aux_channels": list(aux)}
    n = analysis.assertion_count(asserted)
    out = {"aux_channels": list(aux), "marker_channel": ch,
           "marker_name": cfg.get("name"), "marker_occurrences": n}
    if n:
        out["marker_hint"] = (
            f"p1150_marker_stats('{run_id}') gives current, charge and duration "
            f"for each of the {n} assertions." if run_id else
            "Call p1150_marker_stats(run_id) for per-assertion statistics.")
    else:
        out["marker_warning"] = (
            f"{ch} never asserted during this capture, so no marked region was "
            f"recorded. The instrumented code may not have run, the lead may be "
            f"on the wrong pin, or the polarity may be inverted. Run "
            f"p1150_aux_check to see what the input is doing.")
    return out


# ------------------------------------------------------------------ #
# Guide                                                                #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_measurement_guide() -> str:
    """How to measure battery current well with a P1150.

    Read this before the first measurement in a conversation. Covers: choosing a
    supply voltage and over-current limit, the eight current profiles worth
    knowing (sleep floor, boot sequence, power-on inrush surge, periodic
    wake-up, single triggered event, GPIO-marked code region, scripted
    regression run, charging), which tool and capture length suits each, how to
    read a regression result, and the mistakes that produce measurements that
    look fine but mean nothing.
    """
    return GUIDE


@mcp.tool()
def p1150_inrush_guide() -> str:
    """Why a current surge resets targets in the field, and what to do about it.

    Read this before running p1150_inrush_test or interpreting an inrush
    warning, and whenever a target resets unpredictably, fails to start on a
    weak or cold battery, boot-loops, or trips the over-current limit.

    Inrush is the one thing measurable here that is a reliability bug rather
    than a battery-life one, and it is invisible without an instrument at the
    battery terminals. The P1150 is a low-impedance supply: it delivers the
    surge and holds its voltage, so the target works perfectly on the bench. A
    real battery sags by (surge current x internal resistance) instead, and
    since a cell's internal resistance is at its highest when aged, cold and
    near flat, the failure appears in the field and refuses to reproduce.

    Crucially it distinguishes the two kinds, which are not equally important.
    A surge at power-up happens once, when the battery is fitted, and is usually
    acceptable. A surge from a rail being switched -- an LDO or SMPS
    power-gated to save current, whose decoupling capacitance is a short circuit
    at the instant of enable -- repeats for the life of the product. That one is
    the real defect, its proper fix is a regulator with soft-start, and because
    that is a schematic decision it is far cheaper to find during firmware
    development than after the boards exist.

    Covers: both kinds and when the power-up one does matter, where the surge
    comes from, why the developer cannot see it, how to measure each without
    missing it, why the over-current limit must be set high, what to ask the
    developer for (chemistry, cell internal resistance, the target's brown-out
    voltage), how to judge the sag, whether the power-gating is even paying for
    itself, the fixes in the order they are worth trying, and the confounders
    that make a measured surge smaller than the real one.
    """
    return INRUSH_GUIDE


@mcp.tool()
def p1150_marker_guide() -> str:
    """How to mark a code region with a GPIO so its current can be measured
    exactly.

    Read this before using p1150_set_aux, p1150_marker_stats or
    p1150_compare_marker, and whenever asked what a particular function,
    driver or feature costs the battery.

    The P1150 has three auxiliary inputs (A0, D0, D1) that record alongside
    current. Wiring a spare target GPIO to one of them, and raising it around
    the work being measured, is the difference between inferring an event's
    boundaries from a current threshold and knowing them. This covers the
    firmware change, with example code; where to place the assertions and where
    not to; the wiring and its constraints; how to choose between A0, D0 and D1
    and what voltage levels to set; and the failure modes to check for first.

    It needs the developer's involvement: they make the physical connection, and
    they have to be willing to carry a few lines of instrumentation in the
    build. Both are worth asking for.
    """
    return MARKER_GUIDE


# ------------------------------------------------------------------ #
# Project setup                                                        #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_set_battery(capacity_mah: float = None, chemistry: str = None,
                      nominal_mv: int = None, esr_mohm: float = None,
                      brownout_mv: int = None) -> dict:
    """Record what battery this target runs on, and what it needs of it.

    ASK THE DEVELOPER FOR THE CAPACITY at the start of a project, before the
    first measurement -- it cannot be inferred from a current waveform or from
    the code, and without it the measurements stay abstract. With it, every
    result gains the numbers people actually act on: how long the device lasts,
    what share of the battery one wake-up or one boot costs, how many times an
    operation can run before the pack is flat, and whether a measured charging
    current is a sensible C rate.

    Settings persist across sessions and are merged, not replaced, so each can
    be added when it comes up. Call p1150_get_battery to see what is set.

    capacity_mah: the pack's rated capacity, e.g. 220 for a small LiPo, 2000 for
        an 18650, 3000 for a phone-sized cell, 225 for a CR2032 coin cell.

    chemistry: e.g. "Li-ion", "LiPo", "LiFePO4", "alkaline", "NiMH", "CR2032".
        Worth recording even approximately: it sets the internal resistance
        assumed when judging whether a current surge would brown the target out,
        and coin cells behave completely differently from everything else.

    nominal_mv: nominal cell voltage, useful as a reminder of what to pass to
        p1150_power_on.

    esr_mohm: the cell's internal resistance in milliohms, if it is known or has
        been measured. Only needed for the inrush brown-out estimate, which
        otherwise assumes a typical figure for the chemistry. A measured value
        is much better than an assumed one -- it is the difference between
        "this might reset in the cold" and "this will".

    brownout_mv: the lowest terminal voltage the target still works at -- the
        regulator's dropout or the MCU's brown-out reset level, whichever is
        higher. WORTH ASKING FOR whenever an inrush surge is found: without it
        the sag a real battery would suffer can be calculated but not judged,
        and judging it is the entire question. Typically 3000-3300 mV for a
        3.3 V system on a Li-ion cell.
    """
    try:
        if capacity_mah is not None and capacity_mah <= 0:
            return {"error": "capacity_mah must be greater than zero."}
        if not any(v is not None for v in
                   (capacity_mah, chemistry, nominal_mv, esr_mohm,
                    brownout_mv)):
            return {"error": "Nothing to record. Pass at least one of "
                             "capacity_mah, chemistry, nominal_mv, esr_mohm, "
                             "brownout_mv."}
        cfg = config.set_battery(capacity_mah, chemistry, nominal_mv,
                                 esr_mohm, brownout_mv)
        notes = []
        if cfg.get("capacity_mah"):
            notes.append("Battery life, percentage-of-battery and C rate "
                         "figures are included in measurement results.")
        else:
            notes.append("No capacity recorded yet, so battery life and "
                         "percentage-of-battery figures are still unavailable.")
        if cfg.get("brownout_mv"):
            notes.append("Inrush results now say whether a real cell would sag "
                         "below the target's operating voltage.")
        cfg["note"] = " ".join(notes)
        return cfg
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_get_battery() -> dict:
    """Show the battery settings configured for this project.

    Reports capacity, chemistry, internal resistance and the target's brown-out
    voltage, and the internal-resistance figures that would be assumed for an
    inrush assessment given what is set.

    If nothing is set, ask the developer for the pack's mAh rating and record it
    with p1150_set_battery.
    """
    try:
        cfg = config.get()
        model = config.battery_model()
        cfg["assumed_esr"] = analysis.esr_profile(model["chemistry"],
                                                  model["esr_mohm"])
        if not cfg.get("capacity_mah"):
            cfg["configured"] = False
            cfg["action"] = ("Ask the developer what capacity battery (in mAh) "
                             "the target runs on, then call p1150_set_battery.")
            return cfg
        cfg["configured"] = True
        if not cfg.get("brownout_mv"):
            cfg["brownout_hint"] = (
                "The target's minimum operating voltage is not recorded. It is "
                "needed only to judge an inrush surge -- ask for it if one "
                "turns up.")
        return cfg
    except Exception as e:
        return _fail(e)


# ------------------------------------------------------------------ #
# Auxiliary marker inputs                                              #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_set_aux(channel: str, name: str = None, active_high: bool = True,
                  threshold_mv: float = None, hysteresis_mv: float = None,
                  primary: bool = True) -> dict:
    """Declare a signal the target drives into a P1150 auxiliary input, so that
    current can be measured over exactly the code region the firmware marks.

    THIS REQUIRES TWO THINGS OF THE DEVELOPER, so read p1150_marker_guide and
    agree both with them before calling: a spare GPIO on the target driven high
    at the start of the work being measured and low at the end, and a wire from
    that pin to the P1150's A0, D0 or D1 input with grounds in common. Almost
    every embedded target has a pin free for this, and it is the single change
    that turns "roughly what does this feature cost" into an exact answer.

    Why it is worth the wiring: without a marker, the boundaries of an event
    have to be inferred from a current threshold, which fails whenever the work
    does not stand out clearly above the idle floor, and shifts between runs so
    two measurements are never quite of the same thing. A GPIO marker makes the
    boundaries a fact the firmware states, so charge per invocation is exact and
    two builds are compared over provably the same code path.

    Once declared, the channel is recorded alongside current in every capture,
    p1150_marker_stats reports per-assertion statistics, p1150_compare_marker
    checks that region for regressions, and p1150_capture_single can trigger
    from it.

    channel: "D0" or "D1" for a logic input. Prefer these: they accept a
        1.2-3.3 V signal and the driver rescales them to fixed levels, so no
        threshold is needed whatever IO voltage the target uses.
        "A0" is analog, accepts 0-17 V and reports millivolts as measured. Use
        it when both digital inputs are taken, when the signal is not a clean
        logic level, or -- importantly -- WHEN THE TARGET DRIVES ABOVE 3.3 V.
        A 5 V or 12 V signal must go to A0; connecting it to D0/D1 exceeds
        their 3.3 V limit and damages the P1150.

    name: what the marked region is, e.g. "ble_tx", "sensor_read",
        "crypto_sign". It appears in the results and is what makes them
        readable weeks later.

    active_high: True when the firmware raises the pin for the duration of the
        work (the usual convention). False when the pin idles high and is pulled
        low instead.

    threshold_mv: REQUIRED for A0, and rejected for D0/D1 which have fixed
        levels already. It is the millivolt level separating low from high on
        the signal itself; half the target's IO voltage is the standard choice:
        1650 for a 3.3 V GPIO, 900 for 1.8 V, 2500 for 5 V. It always describes
        the signal, never the assertion, so an active-low marker on a 3.3 V rail
        is still 1650 with active_high=False. A0 accepts up to 17000 mV.

    hysteresis_mv: half-width of the band around the threshold, defaulting to
        10% of it (minimum 50 mV). An edge seen through a probe lead has finite
        slew and some ringing, and without a band one crossing can be counted as
        several assertions. Widen it if p1150_aux_check reports NOISY.

    primary: make this the channel the analysis tools use by default. Set False
        when adding a second marker alongside an established one.

    AFTER CALLING THIS, run p1150_aux_check before a long measurement. It
    confirms the target is really driving the pin and that the threshold sits
    between the two levels -- a marker that never asserts produces a capture
    that looks fine and yields nothing.
    """
    try:
        cfg = config.set_aux(channel, name, active_high,
                             threshold_mv, hysteresis_mv, primary)
        ch = channel.upper()
        cfg["configured"] = ch
        cfg["note"] = (
            f"{ch} will now be recorded alongside current in every capture. "
            f"Run p1150_aux_check to confirm the target actually drives it "
            f"before taking a measurement that matters.")
        return cfg
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_get_aux() -> dict:
    """Show which auxiliary inputs are set up as markers for this project.

    Returns each channel's name, polarity and thresholds, and which one the
    analysis tools use by default. Nothing configured means captures record
    current only.
    """
    try:
        cfg = config.get_aux()
        if not cfg.get("channels"):
            return {"configured": False,
                    "channels": {},
                    "note": "No auxiliary input is set up, so captures record "
                            "current only. If the target has a spare GPIO, "
                            "read p1150_marker_guide -- marking a code region "
                            "with it gives exact per-invocation charge instead "
                            "of a figure inferred from a current threshold."}
        cfg["configured"] = True
        return cfg
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_clear_aux(channel: str = None) -> dict:
    """Stop recording an auxiliary input, or all of them.

    Use when the marker wire comes off, or to save memory on a long capture --
    each retained aux channel costs as much as a current channel.
    """
    try:
        cfg = config.clear_aux(channel)
        cfg["note"] = (f"{channel.upper()} is no longer recorded."
                       if channel else
                       "Auxiliary inputs are off; captures record current only.")
        return cfg
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_aux_check(duration_s: float = 2.0) -> dict:
    """Check that the target really drives the auxiliary inputs, before relying
    on them.

    ASK THE DEVELOPER TO EXERCISE THE INSTRUMENTED CODE while this runs -- the
    marker only appears if the code being marked actually executes. For a marker
    on something periodic, use a duration covering several repetitions.

    This is the step that catches the failures which otherwise waste a whole
    measurement: a lead on the wrong pin, a GPIO never configured as an output,
    an inverted polarity, or an A0 threshold sitting outside the two levels the
    signal actually reaches. Each of those produces a capture that looks
    perfectly normal and contains no usable marker.

    For every configured channel it reports the levels seen, how many assertions
    were detected, and a verdict:
      TOGGLING    the signal moves as a marker should. Good to measure.
      STUCK_LOW   never left its low level -- the code did not run, the pin is
                  not driven, or the wire is on the wrong pad.
      STUCK_HIGH  never left its high level -- often an inverted polarity
                  (retry with active_high=False), or a pin left asserted.
      NOISY       transitions far too often to be a GPIO marking work. Usually a
                  floating input, or a threshold sitting in the middle of noise
                  -- widen hysteresis_mv, or check the ground connection.

    For A0 it also suggests a threshold and hysteresis from the two levels it
    actually observed, which is more reliable than assuming the target's IO
    voltage.
    """
    try:
        channels = config.aux_channels()
        if not channels:
            return {"error": "No auxiliary input is configured. Call "
                             "p1150_set_aux first (p1150_marker_guide explains "
                             "the firmware and wiring side)."}
        i, _, aux = SESSION.measure(duration_s)
        out = {"duration_s": duration_s, "channels": {}}
        for ch in channels:
            if ch not in aux:
                continue
            cfg = config.aux_channel_cfg(ch)
            r = analysis.marker_survey(aux[ch], cfg, SAMPLE_RATE, ch)
            r["name"] = cfg.get("name")
            r["units"] = ("mV as measured at the input" if ch == "A0" else
                          "mV, rescaled by the driver to fixed logic levels "
                          "(~100 low, ~900 high) whatever the target drives")
            r["active_high"] = cfg.get("active_high", True)
            if ch == "A0" and cfg.get("threshold_mv") is not None:
                lo, hi = r.get("low_level"), r.get("high_level")
                thr = cfg["threshold_mv"]
                if lo is not None and hi is not None and not (lo < thr < hi):
                    r["threshold_warning"] = (
                        f"The configured threshold of {thr} mV is not between "
                        f"the levels actually seen ({lo} to {hi} mV), so the "
                        f"signal never crosses it. Re-run p1150_set_aux with "
                        f"threshold_mv={r.get('suggested_threshold_mv')}.")
            out["channels"][ch] = r
        verdicts = {c: v.get("verdict") for c, v in out["channels"].items()}
        out["ready"] = all(v == "TOGGLING" for v in verdicts.values()) \
            and bool(verdicts)
        if not out["ready"]:
            out["action"] = (
                "Fix the wiring or the firmware before measuring. If the code "
                "being marked simply did not run during these "
                f"{duration_s} s, ask the developer to trigger it and re-check.")
        # A quiet target with a working marker still needs the current side to
        # be sane, so the reading that would otherwise prompt a second call.
        out["mean_current_ma"] = round(float(i.mean()), 6) if i.size else None
        return out
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

    p1150_connect scans for itself, so this is not a required first step -- it
    is for when you need to see what is attached before choosing.
    """
    try:
        devices = scan()
        for d in devices:
            if d.pop("bootloader", False):
                d["note"] = ("in bootloader; p1150_connect will load the "
                             "application firmware (~15 s)")
        return {"count": len(devices), "devices": devices,
                "default_sn": DEFAULT_SN}
    except Exception as e:
        return _fail(e)


@mcp.tool()
async def p1150_connect(sn: str = None, ctx: Context = None) -> dict:
    """Connect to a P1150 by serial number and prepare it for measurement.

    The serial number is printed on the back of the unit; p1150_list_devices
    reports it too. Omit sn to use the P1150_SN environment default, or, if
    that is not set either, to use the only P1150 attached -- when there is
    more than one, the serial number has to be given.

    The first connection after the P1150 is plugged in takes about 15 seconds:
    the application firmware is loaded and the unit self-calibrates. This is
    normal, not a hang. Later connections are fast. Progress is reported to the
    developer as it goes, so do not narrate it yourself or poll for it.

    The connection stays open for the rest of the session, so the target can
    stay powered across many measurements while firmware is edited and
    re-flashed. Nothing is powered until p1150_power_on is called.
    """
    try:
        return await _with_progress(
            ctx, lambda report: SESSION.connect(sn or DEFAULT_SN,
                                                progress=report))
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

        If it trips the moment power is applied, the cause is almost always
        inrush -- a surge of amps for a millisecond as bulk capacitance charges.
        Raising the limit gets past it, but the surge is worth measuring first
        with p1150_inrush_test: a battery cannot supply it and would brown the
        target out instead. Note also that a limit below a surge silently CLIPS
        it, so a capture taken at 500 mA cannot show a 2 A peak.

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

    The capture is also screened for inrush surges, and reports one if found --
    a rail being power-gated surges every time firmware enables it, and turns up
    in a capture like this one without anyone looking for it. Pass such a
    warning on to the developer; see p1150_inrush_check.
    """
    try:
        i, isnk, aux = SESSION.measure(duration_s, connect_probe_during)
        return _store(label, i,
                      {"capture_type": "timed",
                       "connect_probe_during": bool(connect_probe_during)},
                      isnk_ma=isnk, aux=aux)
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
        label, i, isnk, aux = SESSION.capture_stop_raw()
        return _store(label, i, {"capture_type": "background"},
                      isnk_ma=isnk, aux=aux)
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
        i, isnk, aux = SESSION.measure(duration_s)
        battery = config.capacity_mah()
        out = analysis.charge_test(i, isnk, SAMPLE_RATE, battery)
        stored = _store(label, i, {"capture_type": "charge_test"},
                        isnk_ma=isnk, aux=aux)
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
        out = analysis.charge_test(i, isnk, _fs(meta), config.capacity_mah())
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
                         timeout_s: float = 30.0,
                         trigger_on: str = None,
                         trigger_level: float = None) -> dict:
    """Capture a single event, optionally waiting for a trigger.

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

    trigger_on: trigger from an auxiliary input the target itself drives --
        "A0", "D0" or "D1" -- instead of from a current level. This is the
        precise option and the one to prefer when the target has a spare GPIO:
        the firmware raises the pin exactly where the event begins, so the
        capture starts at the right instant even for work whose current draw
        never rises far enough above the idle floor for a current trigger to
        catch it. Declare the channel first with p1150_set_aux; see
        p1150_marker_guide for the firmware and wiring side.

    trigger_level: overrides the aux threshold for this capture only, in the
        channel's own units (millivolts for A0). Rarely needed -- the level from
        p1150_set_aux is used by default.

    position: where the trigger sits in the window. TRIG_POS_LEFT records mostly
        after the event starts; TRIG_POS_CENTER also shows what led up to it,
        which is what you want when diagnosing an unexpected current spike.

    slope: TRIG_SLOPE_RISE fires on the asserting edge of an active-high marker,
        TRIG_SLOPE_FALL on an active-low one.
    """
    try:
        i, isnk, aux, fs = SESSION.capture_single(
            timebase, trigger_ma, position, slope, timeout_s,
            trigger_on, trigger_level)
        return _store(label, i, {"capture_type": "single",
                                 "timebase": timebase,
                                 "trigger_ma": trigger_ma,
                                 "trigger_on": trigger_on},
                      isnk_ma=isnk, aux=aux, fs=fs)
    except Exception as e:
        return _fail(e)


# ------------------------------------------------------------------ #
# Inrush                                                               #
# ------------------------------------------------------------------ #
def _inrush_args(overrides: dict = None) -> dict:
    """Battery and supply context for an inrush assessment."""
    m = config.battery_model()
    args = {"chemistry": m["chemistry"], "esr_mohm": m["esr_mohm"],
            "brownout_mv": m["brownout_mv"]}
    args.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return args


@mcp.tool()
def p1150_inrush_test(label: str = "inrush", voltage_mv: int = None,
                      ovc_ma: int = 3200, timebase: str = "TBASE_SPAN_100MS",
                      trigger_ma: float = 20.0,
                      brownout_mv: int = None) -> dict:
    """Measure the target's power-on inrush surge and say whether a real battery
    could supply it.

    THIS COVERS ONLY THE POWER-UP SURGE, WHICH IS THE LESS IMPORTANT KIND. In a
    shipped product the battery is fitted once and left in, so a surge that only
    happens when power is first applied happens once in the device's life, and
    is usually acceptable. The kind that hurts is a rail switched while the
    target runs -- an LDO or SMPS power-gated to save current, surging every
    time firmware enables it, for the life of the product. To find that one,
    capture the target doing its normal work with p1150_measure or
    capture_start/capture_stop (every capture is screened automatically) and use
    p1150_inrush_check. A clean result HERE says nothing about switched rails.

    Run this one when the power-up surge itself is in question: the target
    boot-loops or fails to start on a weak or cold cell, it trips the
    over-current limit at power-on, the battery is user-replaceable, a charger
    or dock can hot-plug the rail, or the cell is a coin cell.

    THIS POWER-CYCLES THE TARGET. The probe is opened, the target's rails are
    given a moment to discharge, and it is powered back up -- there is no way to
    measure a power-on surge without a power-on. Say so before calling it if the
    target is mid-workload, holding state, or connected to a debugger.

    WHY INRUSH IS WORTH CHECKING EVEN WHEN NOTHING SEEMS WRONG. Most developers
    have no instrument at the battery terminals, so an inrush problem is
    invisible to them: charging bulk capacitance can pull amps for a millisecond
    or two, and the only symptom is a device that occasionally fails to start.
    The P1150 is a low-impedance supply, so it delivers that surge and shows it.
    A battery cannot -- it sags by (surge current x internal resistance)
    instead, and that sag is what resets the target. Because a cell's internal
    resistance is at its highest when aged, cold, and at a low state of charge,
    a board that boots perfectly on the bench can reset every time on a cold
    morning at the end of the battery's life. Inrush is a common root cause of
    exactly that class of field failure, and it is one of the few things
    measurable here that is a reliability bug rather than a battery-life one.

    HOW IT IS MEASURED: the acquisition is armed and triggered on current with
    the probe still open, and only then is the relay closed. The instrument is
    already waiting when the surge arrives. p1150_measure(connect_probe_during=
    True) captures the boot sequence but re-arms between chunks, so a
    millisecond-long surge at the very front of it can fall in a gap.

    Returns the peak, how long it lasted, what the target settles at afterwards,
    and the estimated terminal-voltage sag for a fresh cell, a part-aged one,
    and an aged cold one near flat -- the last being where field failures
    happen. If brownout_mv is known it says outright whether the target would
    reset.

    voltage_mv: supply voltage for the test. Defaults to whatever p1150_power_on
        last set. THIS GOES STRAIGHT TO THE TARGET'S BATTERY TERMINALS --
        confirm it with the developer if it has not already been agreed. Test at
        the LOW end of the battery's range as well as at nominal: inrush is
        worse where the battery is weakest, and that is the case that fails.

    ovc_ma: over-current limit for the test, default 3200 mA, which is the
        P1150's own default and about its ceiling. Leave it high: the point is
        to measure the surge, and a low limit clips it -- the trace then shows
        the instrument's limit rather than the target's demand, and the supply
        cuts out mid-measurement. If it trips even at 3200 mA, that is itself
        the finding.

    timebase: capture window. 100 ms is a good default: long enough to show the
        surge and what the target settles to afterwards. Use TBASE_SPAN_10MS or
        20MS to resolve the shape of a very fast surge, or 500MS/1S to see the
        whole boot after it. Spans above 1 s are decimated by the instrument and
        blur a millisecond-scale event -- avoid them here.

    trigger_ma: current level that starts the capture, default 20 mA. It only
        needs to sit above the noise floor and below the surge, since the target
        is drawing nothing at all when the capture is armed.

    brownout_mv: the lowest terminal voltage the target still runs at, for this
        test only. Better recorded once with p1150_set_battery.
    """
    try:
        v = voltage_mv or SESSION.vout_mv
        if not v:
            return {"error": "No supply voltage set or given. Pass voltage_mv "
                             "(confirm it with the developer first -- it goes "
                             "directly to the target's battery terminals), or "
                             "call p1150_power_on first."}
        i, isnk, aux, fs, info = SESSION.inrush_capture(
            v, ovc_ma, timebase, trigger_ma)
        stored = _store(label, i, dict(info, capture_type="inrush"),
                        isnk_ma=isnk, aux=aux, fs=fs)
        if "error" in stored:
            return stored

        out = analysis.inrush_analysis(
            i, fs, ovc_ma=info.get("ovc_ma"), supply_mv=v,
            # The latched error is authoritative where the samples are only
            # suggestive: a surge that ends just under the limit still tripped
            # it if the device says so.
            ovc_tripped=info.get("ovc_tripped"),
            power_on_capture=True,
            **_inrush_args({"brownout_mv": brownout_mv}))
        out["run_id"] = stored.get("run_id")
        out["label"] = label
        out["voltage_mv"] = v
        out["timebase"] = timebase
        out["settled_ma"] = stored.get("median_ma")

        if info.get("ovc_tripped"):
            out["device_errors"] = info.get("device_errors")
            out["action"] = (
                f"The over-current limit of {ovc_ma} mA tripped, so the target "
                f"is unpowered now and the peak shown is the instrument's "
                f"limit, not the target's. Call p1150_clear_error and then "
                f"p1150_power_on to restore power. The trip is the result: the "
                f"target demands more than {ovc_ma} mA at power-up, which no "
                f"small cell can deliver.")
        elif not info.get("probe_was_connected"):
            out["note_power"] = ("The target was unpowered before this test and "
                                 "is powered now.")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_inrush_check(run_id: str, peak_ma: float = None,
                       max_duration_ms: float = None,
                       brownout_mv: int = None,
                       supply_mv: int = None) -> dict:
    """Look for inrush surges in a stored capture, and assess what a real
    battery would do about them.

    USE THIS ON ORDINARY CAPTURES, not just on one taken with p1150_inrush_test.
    The most damaging kind of inrush is not at power-up at all: it is a rail
    being switched while the target runs. Power-gating an LDO or SMPS to save
    current is standard practice, and at the instant of enable the decoupling
    capacitance downstream is discharged -- which is to say a short circuit --
    so the current is limited only by resistance in the path. That repeats every
    duty cycle for the life of the product, and it turns up in a capture taken
    to measure battery life, where nobody is looking for it.

    Also the tool to reach for when a capture came back with an inrush warning
    attached, or when the target resets unpredictably, fails to start on a weak
    battery, or trips the over-current limit.

    It separates the two cases and says which it found, as `recurrence`:
      WHILE_RUNNING     a switched rail. Reports how often it repeats, whether
                        the timing is regular (a timer) or not (event-driven),
                        the charge each switch-on costs, and what that adds up
                        to as an average current -- which answers whether the
                        power-gating is saving anything at all. The proper fix
                        is a regulator with soft-start.
      ONCE_AT_POWER_UP  the surge when power was applied. In a product whose
                        battery is fitted once and left in, that happens once in
                        the device's life, so it is ranked lower. It still
                        matters for a user-replaceable battery, a pack
                        protection FET that can re-connect under load, a
                        hot-pluggable rail, or a coin cell.

    A spike counts as an inrush when it is brief (under 4 ms at 10% of its own
    peak) and stands well above the current the target settles at either side of
    it -- the only fair reference, since 1.5 A into a device that then runs at
    1.2 A is ordinary and 1.5 A into one that then runs at 3 mA is not. A single
    spike must also reach 1 A; a REPEATING one counts from 250 mA, because
    repetition is itself most of the evidence and a switched rail on a small
    circuit need not reach an amp to be the same fault. Longer excursions are
    reported as sustained load -- a radio waking, a sensor converting -- which
    has a different cause and a different fix.

    If the run was captured with the over-current limit set below the surge, the
    detection threshold drops to just under that limit: the instrument clips
    there, so a 2 A surge measured behind a 500 mA limit appears as a 500 mA
    plateau and would otherwise read as clean. Any such event is reported with
    its peak marked as a lower bound.

    peak_ma: minimum peak for a SINGLE spike to count as an inrush, default
        1000 mA. Lower it to investigate a target whose supply is smaller, or
        whose brown-out threshold is close.

    max_duration_ms: longest a spike may last and still be inrush, default 4 ms.

    supply_mv / brownout_mv: override the run's recorded supply voltage and the
        target's minimum operating voltage. Both are better recorded once --
        the supply comes from p1150_power_on, the brown-out level from
        p1150_set_battery(brownout_mv=...).
    """
    try:
        i, meta = storage.load(run_id)
        kw = {}
        if peak_ma is not None:
            kw["peak_ma"] = peak_ma
        if max_duration_ms is not None:
            kw["max_duration_ms"] = max_duration_ms
        out = analysis.inrush_analysis(
            i, _fs(meta), ovc_ma=meta.get("ovc_ma"),
            supply_mv=supply_mv or meta.get("voltage_mv"),
            power_on_capture=_powered_up_during(meta),
            **_inrush_args({"brownout_mv": brownout_mv}), **kw)
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        out["settled_ma"] = meta.get("median_ma")
        if not meta.get("ovc_ma"):
            out["ovc_unknown"] = (
                "The over-current limit in force during this run was not "
                "recorded, so a surge clipped by it cannot be identified as "
                "clipped. If this run predates that being stored, or the peak "
                "sits suspiciously flat, re-measure with p1150_inrush_test.")
        return out
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
        out = analysis.summarize(i, _fs(meta), config.capacity_mah())
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
        out = analysis.segment(i, _fs(meta), config.capacity_mah())
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
        out = analysis.find_events(i, _fs(meta), threshold_ma,
                                   min_duration_us, config.capacity_mah())
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_marker_stats(run_id: str, channel: str = None,
                       min_duration_us: float = 0.0) -> dict:
    """Current statistics for the regions where the target's marker GPIO was
    asserted -- the exact answer to "what does this piece of code cost".

    Requires a run captured while an auxiliary input was configured
    (p1150_set_aux). The firmware states where the work starts and stops, so
    unlike p1150_events -- which infers bursts from a current threshold -- the
    boundaries are exact, and work that never rises far above the idle floor is
    measured just as well as work that does.

    Reports, for each assertion and in aggregate:
      duration        how long the marked code ran. A change here is the code
                      getting slower, not drawing more.
      charge_uah      what one invocation costs the battery.
      excess_uah      that cost minus what the target was drawing anyway
                      outside the marker. This is the figure to attribute to
                      the feature; the raw charge also contains the idle floor,
                      which makes a long marker look expensive when it is not.
      mean/peak mA    current while it runs. A change here is the code enabling
                      something different, not taking longer.
      deasserted      the same figures for everything outside the markers, so
                      the marked region can be put in proportion.
      worst_occurrence  the single most expensive and the single longest
                      invocation. A mean hides the one iteration that hit a
                      retry path, and that outlier is usually the interesting
                      one.
      repetition      rate, period jitter and duty cycle when the marker fires
                      more than once.

    channel: which aux input to read. Defaults to the project's primary one.

    min_duration_us: discard assertions shorter than this. Use it if the GPIO
        glitches; note the P1150 samples every 8 us, so an assertion shorter
        than about 50 us is not reliably resolved and one shorter than 8 us can
        be missed entirely.
    """
    try:
        i, _, aux, meta = storage.load_all(run_id)
        ch, cfg, asserted = _resolve_marker(aux, channel, meta)
        out = analysis.marker_analysis(i, asserted, _fs(meta),
                                       config.capacity_mah(), min_duration_us)
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        out["marker_channel"] = ch
        out["marker_name"] = cfg.get("name")
        out["marker_polarity"] = "active_high" if cfg.get("active_high", True) \
            else "active_low"
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_compare_marker(baseline_run_id: str, candidate_run_id: str,
                         channel: str = None, threshold_pct: float = 5.0,
                         min_duration_us: float = 0.0) -> dict:
    """Compare two runs over just the marked code region: did this feature get
    more expensive?

    The strictest regression check available here, and the one to use whenever
    the target has a marker GPIO wired up. p1150_compare weighs a whole capture,
    so it answers only "did the device get worse" and dilutes a real change to
    one feature with everything else the target was doing. This compares the
    marked region alone, and judges on CHARGE PER INVOCATION -- which does not
    move just because the workload ran a different number of times, and does not
    require the two captures to be the same length.

    Both runs must have been captured with the same aux channel configured and
    the same firmware marker in the same place; otherwise the comparison is
    between two different code regions and means nothing.

    Beyond the verdict it separates the two causes that a single number cannot:
    the marked code taking LONGER (added work, retries, a busy-wait, a slower
    clock) versus DRAWING MORE while it runs (a peripheral left on, radio TX
    power, a regulator mode). Those lead to completely different fixes. It also
    reports current outside the marker separately, so a change there is not
    misattributed to the marked feature.
    """
    try:
        bi, _, baux, bmeta = storage.load_all(baseline_run_id)
        ci, _, caux, cmeta = storage.load_all(candidate_run_id)
        bch, bcfg, b_asserted = _resolve_marker(baux, channel, bmeta)
        cch, ccfg, c_asserted = _resolve_marker(caux, channel, cmeta)
        if bch != cch:
            return {"error": f"The runs used different aux channels "
                             f"({bch} vs {cch}). Pass channel= to pick one, or "
                             f"re-capture so both mark the same input."}
        out = analysis.compare_markers(bi, b_asserted, ci, c_asserted,
                                       _fs(bmeta), threshold_pct,
                                       config.capacity_mah(), min_duration_us,
                                       fs_cand=_fs(cmeta))
        out["marker_channel"] = bch
        out["marker_name"] = bcfg.get("name") or ccfg.get("name")
        out["baseline_run"] = {"run_id": baseline_run_id,
                               "label": bmeta.get("label"),
                               "voltage_mv": bmeta.get("voltage_mv")}
        out["candidate_run"] = {"run_id": candidate_run_id,
                                "label": cmeta.get("label"),
                                "voltage_mv": cmeta.get("voltage_mv")}
        if bmeta.get("voltage_mv") and cmeta.get("voltage_mv") and \
                bmeta["voltage_mv"] != cmeta["voltage_mv"]:
            out["warning"] = (
                f"Runs used different supply voltages "
                f"({bmeta['voltage_mv']} mV vs {cmeta['voltage_mv']} mV). "
                f"Current draw depends on supply voltage, so this comparison "
                f"is not valid. Re-run both at the same voltage.")
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
        out = analysis.compare(b, c, _fs(mb), threshold_pct,
                               config.capacity_mah(), fs_cand=_fs(mc))
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
def p1150_plot(run_id: str, path: str = None, log_scale: bool = True,
               show_marker: bool = True) -> dict:
    """Render a run as a PNG current-vs-time plot and return the file path.

    Useful when the numbers are ambiguous and the shape of the waveform settles
    it -- read the image back to see the profile directly. Log scale is the
    default because a battery profile spans microamps to milliamps, and a linear
    axis flattens the sleep floor into the baseline.

    show_marker shades the regions where the target's marker GPIO was asserted,
    when the run has an auxiliary channel recorded. Seeing the current alongside
    the code region that produced it is usually what settles an ambiguous
    result: it shows immediately whether the cost sits inside the marked work or
    just outside it, which the numbers alone cannot.

    Waveforms are decimated to a few thousand points for the plot; the stored
    samples are untouched.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        i, _, aux, meta = storage.load_all(run_id)
        fs = _fs(meta)
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
            x = np.linspace(0, trim / fs, y.size)
        else:
            y, x = i, np.arange(n) / fs

        if log_scale:
            y = np.maximum(y, 1e-4)  # keep zeros off a log axis

        plt.figure(figsize=(11, 4.5))

        marked = None
        if show_marker and aux:
            try:
                ch, cfg, asserted = _resolve_marker(aux, None, meta)
                starts, ends = analysis.intervals(asserted)
                # Shading is one patch per assertion, so a capture holding
                # thousands of them would take longer to draw than to measure --
                # and would ink the whole axis solid anyway.
                if 0 < starts.size <= 400:
                    for a, b in zip(starts, ends):
                        plt.axvspan(a / fs, b / fs,
                                    color="#f0a30a", alpha=0.20, linewidth=0)
                    marked = {"channel": ch, "name": cfg.get("name"),
                              "shaded": int(starts.size)}
                elif starts.size:
                    marked = {"channel": ch, "name": cfg.get("name"),
                              "shaded": 0,
                              "note": f"{starts.size} assertions is too many to "
                                      f"shade legibly; left unmarked."}
            except Exception:
                pass

        plt.plot(x, y, linewidth=0.6)
        if log_scale:
            plt.yscale("log")
        plt.xlabel("Time (s)")
        plt.ylabel("Current (mA)")
        title = (f"{meta.get('label', run_id)}  --  "
                 f"avg {meta.get('avg_ma')} mA, {meta.get('charge_mah')} mAh")
        if marked and marked.get("shaded"):
            title += f"   (shaded: {marked.get('name') or marked['channel']})"
        plt.title(title)
        plt.grid(True, "both", color="#ddddee")
        plt.tight_layout()

        out = path or os.path.join(storage.runs_dir(), run_id + ".png")
        plt.savefig(out, dpi=110)
        plt.close()
        res = {"run_id": run_id, "path": out}
        if marked:
            res["marker"] = marked
        return res
    except Exception as e:
        return _fail(e)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
