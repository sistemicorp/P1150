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
from .scenarios import (GUIDE, MARKER_GUIDE, INRUSH_GUIDE, BATTERY_LIFE_GUIDE,
                        STATE_SIGNAL_GUIDE)

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


# What the read-only analysis tools say about their bandwidth_hz argument.
# One text, because the argument means exactly the same thing in each of them
# and an agent that has read it once should not have to re-read a variant.
_BANDWIDTH_DOC = """\
    bandwidth_hz: read the run block-averaged down to this rate first. 5000 and
        1000 are the useful values. Use it when the target has a switching
        regulator (SMPS/buck) between the battery and the load: the P1150 sits
        at the battery terminals and so measures the regulator's PULSED input
        current, which arrives as a wide noisy band whose peak is a switching
        pulse rather than anything the load did. Averaging it reports what the
        load actually costs, which is the question being asked. Average current
        and charge are unchanged by this -- only the peaks, percentiles and
        shape are. Captures flagged with switching_ripple are the ones that
        need it. Do not use it on inrush work: a surge one to two milliseconds
        wide averages away to nothing and the peak then looks safe.

        It is also how a LONG capture is made quick to read. Every pass here is
        proportional to sample count, and a 300 s run is 37.5 million samples:
        seconds per call raw, milliseconds at 1 kHz. The band-limited copy is
        built once and cached beside the run, so after the first call every
        other tool reading it at the same bandwidth is effectively free. Reach
        for this rather than for a shorter capture -- a measurement cut short to
        keep the tools responsive is the wrong trade, and duration is what a
        duty-cycled average needs most."""


def _bandwidth_doc(fn):
    """Append the shared bandwidth_hz paragraph to a tool's docstring.

    Applied UNDER @mcp.tool(), so the text is in place before the SDK reads the
    docstring to build the tool schema the agent sees.  The alternative was the
    same paragraph copied into four docstrings, which is how three of them end
    up subtly disagreeing about what the argument does a year from now.
    """
    fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n\n" + _BANDWIDTH_DOC + "\n"
    return fn


def _read(run_id: str, bandwidth_hz: float = None):
    """(samples, sample rate, bandwidth plan, metadata) for a stored run.

    Every read-only analysis tool loads through here, so "at 1 kHz" means the
    same thing in all of them -- and so a band-limited read of a long capture
    goes to the small cached copy rather than to the hundreds of megabytes of
    raw samples behind it.
    """
    if bandwidth_hz:
        return storage.load_view(run_id, bandwidth_hz)
    i, meta = storage.load(run_id)
    return i, float(_fs(meta)), None, meta


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
        # has moved on -- and a state code decoded under a different pin
        # assignment than it was captured with would name the wrong states.
        meta["aux_channels"] = list(aux)
        meta["aux_config"] = {c: config.aux_channel_cfg(c) for c in aux}
        meta["marker_channel"] = config.aux_primary()
        signal = config.get_state_signal()
        if signal.get("channels") and all(c in aux for c in signal["channels"]):
            meta["state_signal"] = signal
    run_id = storage.save(label, i_ma, meta, isnk_ma=isnk_ma, aux=aux)
    out = {"run_id": run_id, "label": label}
    out.update(summary)
    out.update(extra or {})
    if aux:
        if meta.get("state_signal"):
            out.update(_state_headline(aux, meta, run_id))
        if config.aux_marker_channels():
            out.update(_marker_headline(aux, meta, run_id))
        elif not meta.get("state_signal"):
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
    # And screened for switching ripple, for the same reason: a developer whose
    # target has a buck converter after the battery sees a band of noise 20x
    # wider than the current they expected, and nothing in the capture says that
    # it is an artefact of measuring at the battery rather than at the load.
    # Said once here, with the option that fixes it, rather than waiting to be
    # asked -- because the question it usually prompts is "is my board broken".
    try:
        out.update(analysis.ripple_screen(i_ma, fs))
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
    # A channel carrying the state signal is not a marker: it is asserted for
    # minutes by design, so reading it as one yields a single assertion
    # spanning the capture.
    signal = set(_signal_channels(meta))
    spare = [c for c in aux if c.upper() not in signal]
    if not channel and not spare:
        raise ValueError(
            f"The only auxiliary channels in this run ({', '.join(aux)}) carry "
            f"the state signal, not a region marker. For per-state current use "
            f"p1150_state_split; for the cost of one function, wire a marker to "
            f"A0 and see p1150_marker_guide.")
    ch = (channel or meta.get("marker_channel") or config.aux_primary()
          or spare[0]).upper()
    if ch not in aux:
        raise ValueError(
            f"Run has no '{ch}' channel. Recorded: {', '.join(aux) or 'none'}.")
    cfg = config.aux_channel_cfg(ch) or (meta.get("aux_config") or {}).get(ch) or {}
    return ch, cfg, analysis.to_logic(aux[ch], cfg)


def _signal_channels(meta: dict = None) -> list:
    """Channels carrying the state signal for this run.

    The run's own record wins: a capture taken before the encoding was changed
    still has to decode under the encoding it was taken with.
    """
    sig = (meta or {}).get("state_signal") or config.get_state_signal()
    return list(sig.get("channels") or [])


def _state_codes(aux: dict, meta: dict = None):
    """(code per sample, {code: name}) for a run carrying a state signal."""
    sig = (meta or {}).get("state_signal") or config.get_state_signal()
    channels = list(sig.get("channels") or [])
    if not channels:
        raise ValueError(
            "No state signal is declared, so a capture cannot be split by "
            "state. If the firmware can drive two spare GPIOs, "
            "p1150_state_signal_guide has the fifteen lines it takes -- it "
            "makes every state measurement exact instead of resting on someone "
            "confirming the target was in the right mode.")
    codes = analysis.decode_state_codes(aux, channels)
    names = {int(c): n for n, c in (sig.get("codes") or {}).items()}
    return codes, names


def _state_headline(aux: dict, meta: dict = None, run_id: str = None) -> dict:
    """The one line about states that belongs on a capture carrying a signal."""
    try:
        codes, names = _state_codes(aux, meta)
        r = analysis.state_breakdown(np.zeros(codes.size, dtype=np.float32),
                                     codes, names)
    except Exception:
        return {}
    seen = [row["state"] or f"code {row['code']}" for row in r["states"]]
    out = {"states_seen": seen, "state_transitions": r["transitions"]}
    if r.get("unmapped_codes"):
        out["unmapped_codes"] = r["unmapped_codes"]
    if run_id:
        out["state_hint"] = (
            f"p1150_state_split('{run_id}') gives current, charge and time for "
            f"each of these states." if len(seen) > 1 else
            f"The target stayed in one state for this whole capture.")
    return out


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
# Orientation                                                          #
# ------------------------------------------------------------------ #
def _state_baselines() -> dict:
    """The capture standing for each declared state, and whether it is sound.

    Resolution order is: a run pinned to the state, else the newest capture
    taken for it.  Both come back with the baseline verdict recorded at capture
    time, so an estimate can warn about a shaky input without re-reading a
    hundred megabytes of samples to find out.
    """
    out = {}
    for key, s in config.get_usage()["states"].items():
        meta = None
        if s.get("run_id"):
            try:
                meta = storage.load_meta(s["run_id"])
            except Exception:
                meta = None
        if meta is None:
            meta = storage.latest_for_state(s.get("name") or key)
        out[key] = meta
    return out


@mcp.tool()
def p1150_start() -> dict:
    """Where this project stands, and what to do next. Start here.

    Call this at the beginning of a session, and whenever the developer asks an
    open question -- "what can I do with this thing", "where do I start", "what
    should I measure" -- rather than answering from general knowledge. It
    reports the actual state of THIS project: what is connected, what the
    battery is, what the usage model says, which states have a usable baseline
    and which do not, and the single next action that moves the work forward.

    The arc of a battery-current project runs:

      1. Connect, and power the target at a voltage the DEVELOPER confirms. It
         goes straight to the battery terminals and there is no undo.
      2. Record the battery: capacity, and how long the product has to last.
      3. Describe how the product is used -- its states and their share of the
         time, and the things that happen a countable number of times a day.
         This cannot be measured and has to be asked for.
      4. Measure each state, once, with the developer putting the target into it.
      5. Estimate battery life, and see which contributor actually dominates.
      6. Optimise that one, re-measure that one state, estimate again.

    Everything else the server does hangs off that spine: p1150_compare for
    whether a change cost battery life, marker GPIOs (p1150_marker_guide) for
    what one feature costs, and inrush (p1150_inrush_guide) for the reliability
    problem that turns up in these captures unasked.
    """
    try:
        battery = config.get()
        usage = config.get_usage()
        baselines = _state_baselines()
        out = {
            "connected": SESSION.is_connected(),
            "powered": bool(SESSION.probe_on),
            "voltage_mv": SESSION.vout_mv,
            "ovc_ma": SESSION.ovc_ma,
            "battery": battery or None,
            "usage_states": {k: dict(v, measured=bool(baselines.get(k)))
                             for k, v in usage["states"].items()},
            "usage_events": usage["events"],
            "runs_stored": len(storage.list_runs(1000)),
        }
        # What the target is drawing at this moment, if anything is connected.
        # It is free -- the reading arrives once a second whether it is asked
        # for or not -- and orientation is exactly when it is useful.
        if SESSION.is_connected():
            a = SESSION.ammeter.snapshot()
            if a.get("available") and not a.get("stale"):
                out["ammeter_ma"] = a["current_ma"]
        total = config.usage_fraction_total()
        if usage["states"]:
            out["usage_fraction_total_pct"] = round(total, 3)
        unmeasured = [k for k in usage["states"] if not baselines.get(k)]

        # One next action, not a menu.  An agent handed a list of five possible
        # things will pick the one that needs nothing from the developer, and
        # the steps that need the developer are the ones that cannot be skipped.
        if not out["connected"]:
            nxt = ("Connect the instrument: p1150_connect(). The first "
                   "connection after plugging in takes about 15 seconds.")
        elif not battery.get("capacity_mah"):
            nxt = ("Ask the developer two questions and record the answers with "
                   "p1150_set_battery: what capacity battery (mAh) the target "
                   "runs on, and how long it has to last (target_days). "
                   "Neither can be measured.")
        elif not out["powered"]:
            nxt = ("Ask the developer what supply voltage to use and CONFIRM IT "
                   "before calling p1150_power_on -- it goes directly to the "
                   "target's battery terminals and too high destroys the "
                   "target. Never infer it.")
        elif not usage["states"]:
            nxt = ("Ask the developer how the product spends its time -- which "
                   "states it has and roughly how many hours a day in each -- "
                   "and declare them with p1150_set_usage_state. Read "
                   "p1150_battery_life_guide first; it has the questions to ask "
                   "and why a capture cannot answer them. If you are also "
                   "writing the target's firmware, add a state signal at the "
                   "same time (p1150_state_signal_guide): fifteen lines, and "
                   "every state measurement afterwards is exact.")
        elif abs(total - 100.0) > 0.5:
            nxt = (f"The declared states account for {total:.1f}% of the time, "
                   f"not 100%. Ask the developer what the device is doing for "
                   f"the remaining {100.0 - total:.1f}% and declare it -- "
                   f"unaccounted time cannot be assumed to be cheap.")
        elif unmeasured:
            s = unmeasured[0]
            signal = config.get_state_signal()
            nxt = (
                f"Measure the remaining states ({', '.join(unmeasured)}). The "
                f"target signals its own state, so p1150_measure_states(60) "
                f"while it runs normally captures them all at once -- or "
                f"p1150_measure_state(state='{s}') to wait for one and measure "
                f"only that."
                if signal.get("codes") else
                f"Measure the '{s}' state: ask the developer to put the target "
                f"into it, wait for it to settle, and confirm before you "
                f"capture. Then p1150_measure_state(state='{s}', "
                f"duration_s=30).")
        else:
            nxt = ("Every declared state has a baseline: run "
                   "p1150_battery_life() for the estimate and the ranking of "
                   "what is actually spending the battery.")
        out["next_action"] = nxt
        out["state_signal"] = config.get_state_signal() or None
        out["guides"] = {
            "p1150_battery_life_guide": "how long will the battery last, and "
                                        "what should I optimise",
            "p1150_measurement_guide": "how to measure well; the profiles worth "
                                       "knowing",
            "p1150_state_signal_guide": "have the firmware declare its own "
                                        "state, so measurements stop depending "
                                        "on anyone confirming it",
            "p1150_inrush_guide": "surges that reset the target in the field",
            "p1150_marker_guide": "what one function or feature costs",
        }
        return out
    except Exception as e:
        return _fail(e)


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
def p1150_battery_life_guide() -> str:
    """How to predict a week of battery life from a minute of measuring.

    Read this whenever the question is how long the battery lasts, whether the
    target will meet a battery-life requirement, or what to optimise to make it
    last longer -- which is the first question of most projects, and the one no
    single capture answers.

    A capture measures the state the target happened to be in. A product moves
    between states, and the weighting between them is a fact about how the
    product is used, not about the board on the bench, so it has to come from
    the developer. Life is capacity / SUM(fraction x current): the instrument
    contributes the currents, in under a minute per state, and the developer
    contributes the weights. That separation is what makes a week predictable
    without waiting a week, and it is the entire method.

    Covers: why extrapolating one capture is wrong and by how much; the
    difference between states (weighted by time) and events (weighted by rate),
    and why folding a boot into an average loses it; how to ask a developer for
    a usage model, in words; how long to measure each state and the two ways a
    baseline goes silently wrong -- a target still settling into a state, and a
    state whose own duty cycle was undersampled; how to read the contribution
    ranking and the optimisation payoff; how to tell whether the developer's
    duty-cycle guess even affects the answer; and what the estimate leaves out
    -- usable versus rated capacity, self-discharge, temperature, ageing.
    """
    return BATTERY_LIFE_GUIDE


@mcp.tool()
def p1150_state_signal_guide() -> str:
    """How to make the target declare which state it is in, so a measurement
    never rests on someone confirming it.

    READ THIS IF YOU ARE ALSO WRITING THE TARGET'S FIRMWARE. It is the single
    highest-value change available here: about fifteen lines to drive a 2-bit
    code on two spare GPIOs, after which one capture of the device doing its
    real work yields a separate, exact current for every state it passes
    through, and a battery-life estimate stops depending on a human putting the
    board into a mode and saying so.

    Without it, every state baseline rests on a claim -- slow to arrange, not
    repeatable, and when it is wrong the capture looks perfectly normal while
    the error is multiplied by that state's whole share of the product's life.

    Covers: the D0/D1 encoding and why code 0 should be the lowest-power state;
    the firmware, with vendor register equivalents; where in the code to change
    the state and where not to; the failure that catches everyone -- MCUs that
    release GPIO drive in deep sleep unless pad retention is configured, so the
    pins float during exactly the state most worth measuring; how a state signal
    differs from a region marker and how to have both; the wiring and its 3.3 V
    limit; and what the measured time-in-state does and does not tell you about
    how the product is really used.
    """
    return STATE_SIGNAL_GUIDE


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
                      brownout_mv: int = None, target_days: float = None) -> dict:
    """Record what battery this target runs on, and what it needs of it.

    ASK THE DEVELOPER FOR THE CAPACITY at the start of a project, before the
    first measurement -- it cannot be inferred from a current waveform or from
    the code, and without it the measurements stay abstract. With it, every
    result gains the numbers people actually act on: how long the device lasts,
    what share of the battery one wake-up or one boot costs, how many times an
    operation can run before the pack is flat, and whether a measured charging
    current is a sensible C rate.

    ASK FOR target_days IN THE SAME BREATH. "How long does it have to last" is
    a question every developer of a battery product has an answer to, it is the
    thing the whole exercise is ultimately judged against, and it costs nothing
    to record while the capacity is being asked for anyway.

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

    target_days: how long the product is required to run on one charge. Once
        set, p1150_battery_life reports a PASS / MARGINAL / SHORT verdict
        against it, and when it is short, what each contributor would have to
        become to reach it -- "sleep current from 180 uA to 95 uA" rather than
        "cut total current by 38%". Weeks and months are fine as days: 7, 30,
        365.
    """
    try:
        if capacity_mah is not None and capacity_mah <= 0:
            return {"error": "capacity_mah must be greater than zero."}
        if target_days is not None and target_days <= 0:
            return {"error": "target_days must be greater than zero."}
        if not any(v is not None for v in
                   (capacity_mah, chemistry, nominal_mv, esr_mohm,
                    brownout_mv, target_days)):
            return {"error": "Nothing to record. Pass at least one of "
                             "capacity_mah, chemistry, nominal_mv, esr_mohm, "
                             "brownout_mv, target_days."}
        cfg = config.set_battery(capacity_mah, chemistry, nominal_mv,
                                 esr_mohm, brownout_mv, target_days)
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
        if cfg.get("target_days"):
            notes.append(f"Estimates are judged against "
                         f"{cfg['target_days']:g} days.")
        if cfg.get("capacity_mah") and not config.get_usage()["states"]:
            notes.append("Next: ask the developer how the product spends its "
                         "time and declare it with p1150_set_usage_state -- "
                         "capacity alone cannot give a battery life for a "
                         "device that has more than one state. "
                         "p1150_battery_life_guide has the questions.")
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
# Usage model                                                          #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_set_usage_state(name: str, fraction_pct: float = None,
                          hours_per_day: float = None,
                          description: str = None,
                          run_id: str = None) -> dict:
    """Declare a state the product spends part of its life in, and how much of
    its life that is.

    THIS HAS TO BE ASKED FOR. It is the half of a battery-life estimate that no
    instrument can supply: a P1150 measures what the target draws in standby to
    a fraction of a microamp in thirty seconds, and has no way whatever of
    knowing the device is in standby 99% of the time. Do not infer the split
    from the firmware source, from timer periods, or from what a comparable
    product did -- a timer period says how often something runs, not how a
    product nobody has finished designing yet will be used.

    Ask it plainly: "what states does the device have -- asleep, awake but idle,
    actively working -- and out of a typical day, how long is it in each?"
    Two to four states is the useful range. Read p1150_battery_life_guide for
    the full set of questions and why they matter.

    THE FRACTIONS MUST TOTAL 100%. If they do not, some of the product's time is
    unaccounted for and no estimate can be made from it -- unaccounted time
    could be at any current at all, so it cannot be assumed to be cheap. The
    usual omission is the resting state, left out because it felt too obvious
    to mention.

    An uncertain answer is fine. Take the developer's best guess: p1150_battery_
    life reports how much the estimate moves if the split is out by 2x, and very
    often the answer is "barely", because one state dominates. Chase precision
    only when the tool says it matters.

    name: what the developer calls it -- "standby", "connected", "streaming",
        "deep-sleep". It appears in the report, and it is the name to pass to
        p1150_measure_state when capturing this state's current.

    fraction_pct: share of the product's time spent in this state, 0-100.

    hours_per_day: the same thing in the units developers usually answer in.
        Give one or the other, not both.

    description: what the target is doing in this state, and anything needed to
        get it there. Worth writing down -- it is what makes a baseline
        reproducible weeks later.

    run_id: pin this state to a particular capture. Not normally needed: the
        newest p1150_measure_state capture for the state wins automatically, so
        re-measuring after a firmware change supersedes the old baseline. Pin
        one when the newest capture is an experiment and an older run is still
        the reference.

    THINGS THAT HAPPEN A COUNTABLE NUMBER OF TIMES A DAY ARE NOT STATES. A boot,
    a user interaction, an upload, a wake-up: use p1150_set_usage_event. A
    3-second boot is 0.003% of a day and vanishes as a fraction, while as 2 x
    410 uAh it can be a fifth of the daily budget.
    """
    try:
        cfg = config.set_usage_state(name, fraction_pct, hours_per_day,
                                     description, run_id)
        total = config.usage_fraction_total()
        out = {"states": cfg["states"], "events": cfg["events"],
               "fraction_total_pct": round(total, 3)}
        if abs(total - 100.0) > 0.5:
            out["action"] = (
                f"The declared states now total {total:.1f}% of the time. They "
                f"must total 100% before an estimate can be made -- ask the "
                f"developer what the device is doing for the other "
                f"{100.0 - total:.1f}%." if total < 100.0 else
                f"The declared states total {total:.1f}%, which is more than "
                f"the product has. Two states are overlapping, or one of the "
                f"fractions is wrong.")
        else:
            key = (name or "").strip().lower()
            out["note"] = (
                f"The time model is complete. Measure each state with "
                f"p1150_measure_state -- for this one, ask the developer to put "
                f"the target into '{name}', wait for it to settle, and confirm "
                f"before capturing: p1150_measure_state(state='{key}', "
                f"duration_s=30).")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_set_usage_event(name: str, per_day: float = None,
                          per_hour: float = None, charge_uah: float = None,
                          run_id: str = None, description: str = None) -> dict:
    """Declare something that happens a countable number of times a day and
    costs charge each time it does.

    Boots, user interactions, uploads, OTA updates, wake-ups from standby --
    anything discrete. These are weighted by a RATE and cost a CHARGE, where a
    state is weighted by time and costs a current, and keeping them apart is
    what stops an estimate quietly losing them: a 3-second boot expressed as a
    fraction of a day is 0.003% and rounds away, while at 2 x 410 uAh it is
    plainly a fifth of a coin cell's daily budget.

    TRANSITIONS BELONG HERE. Waking from standby into active is rarely free -- a
    radio reconnects, a sensor warms up, a regulator starts -- and on a device
    that wakes hundreds of times a day the transitions can cost more than either
    state it moves between. If the developer says the device wakes 200 times a
    day, measure one wake and declare it here rather than folding it into
    either state's current, where it would be counted at the wrong rate.

    name: "boot", "user-wake", "hourly-upload", "ota".

    per_day / per_hour: how often it happens. Give one. Ask the developer; like
        the state split, it is a fact about how the product is used. For
        something monthly, use a fraction: per_day=0.033.

    charge_uah: what one occurrence costs. Take it from a measurement:
        p1150_marker_stats reports excess_uah per invocation when the target has
        a marker GPIO (the exact figure), and p1150_summary's charge_mah x 1000
        gives it for a capture containing one occurrence.

    run_id: alternative to charge_uah -- a capture containing EXACTLY ONE
        occurrence, whose total charge is then used. A boot capture
        (p1150_measure with connect_probe_during=True) or a
        p1150_capture_single of the event. A capture containing several
        occurrences, or a lot of idle time either side, will overstate the cost;
        prefer charge_uah from a marker where the event is small.

    description: what triggers it, so the figure can be reproduced later.
    """
    try:
        cfg = config.set_usage_event(name, per_day, per_hour, charge_uah,
                                     run_id, description)
        key = (name or "").strip().lower()
        entry = cfg["events"][key]
        out = {"states": cfg["states"], "events": cfg["events"]}
        if entry.get("run_id") and entry.get("charge_uah") is None:
            try:
                meta = storage.load_meta(entry["run_id"])
                out["cost_from_run"] = {
                    "run_id": entry["run_id"], "label": meta.get("label"),
                    "charge_uah": round((meta.get("charge_mah") or 0) * 1000, 4),
                    "duration_s": meta.get("duration_s")}
            except Exception as e:
                out["warning"] = (f"Run '{entry['run_id']}' could not be read "
                                  f"({e}), so this event has no cost yet.")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_set_state_signal(state: str, code: int,
                           channels: list = None) -> dict:
    """Declare that the target itself signals which state it is in, by driving a
    code on the P1150's digital inputs.

    IF YOU ARE ALSO WRITING THE TARGET'S FIRMWARE, DO THIS EARLY. It is about
    fifteen lines -- read p1150_state_signal_guide for them -- and it removes
    the weakest step in every battery measurement. Without it, a state baseline
    rests on someone confirming the target was in the right mode: slow, not
    repeatable, and when it is wrong the capture looks perfectly normal while
    the error gets multiplied by that state's whole share of the product's life.
    With it, the state is a fact the firmware records sample-for-sample beside
    the current, and one capture of the device doing its real work yields a
    separate current for every state it passes through.

    D0 carries bit 0 and D1 bit 1, so two pins give four states:
        0 = 00, 1 = 01, 2 = 10, 3 = 11

    GIVE CODE 0 TO THE LOWEST-POWER STATE. Both pins then rest low while the
    target sleeps; a pin held high in sleep leaks current into the P1150's
    input and lands in the microamp figure being measured. The cost is that an
    unpowered or not-yet-initialised target also reads as code 0 -- which is
    unmistakable in the same capture, since it draws no current at all.

    state: the name declared with p1150_set_usage_state. Using the same names
        is what lets p1150_battery_life pick the measurement up automatically.

    code: the integer the firmware drives for this state.

    channels: defaults to ["D0", "D1"]. Pass ["D0"] alone for a two-state
        device, which frees D1 for a region marker. All calls for one project
        must use the same channels.

    Declaring the signal starts recording those inputs in EVERY capture, so
    ordinary p1150_measure results begin reporting which states the target
    passed through as well.

    AFTERWARDS, RUN p1150_state_check. Several MCUs release GPIO drive in their
    deepest sleep mode unless pad retention is explicitly configured -- so the
    pins float during exactly the state most worth measuring, and the codes
    decode as noise. It is one call and it is the failure that otherwise wastes
    a whole session.
    """
    try:
        sig = config.set_state_signal(state, code, channels)
        codes = sig.get("codes") or {}
        out = {"channels": sig["channels"], "codes": codes,
               "capacity": (1 << len(sig["channels"])),
               "note": (f"{', '.join(sig['channels'])} are now recorded in "
                        f"every capture. {len(codes)} state(s) declared.")}
        undeclared = [n for n in codes if n not in config.get_usage()["states"]]
        if undeclared:
            out["usage_hint"] = (
                f"These have a code but no share of the product's time yet: "
                f"{', '.join(undeclared)}. An estimate needs both -- see "
                f"p1150_set_usage_state.")
        out["action"] = ("Run p1150_state_check with the target running "
                         "normally, before measuring anything.")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_get_state_signal() -> dict:
    """Show how the target encodes its state on the digital inputs, if it does.
    """
    try:
        sig = config.get_state_signal()
        if not sig.get("codes"):
            return {"configured": False,
                    "note": "The target does not signal its state, so a state "
                            "baseline depends on someone confirming the target "
                            "is in the right mode when the capture is taken. "
                            "If the firmware can be edited -- and if you are "
                            "writing it, it can -- p1150_state_signal_guide "
                            "shows the fifteen lines that make it exact "
                            "instead."}
        return {"configured": True, "channels": sig["channels"],
                "codes": sig["codes"]}
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_clear_state_signal() -> dict:
    """Stop decoding a state signal, and stop recording its inputs.

    Use when the instrumentation comes out of the firmware or the wires come
    off. Stored runs keep the encoding they were captured with and still decode.
    """
    try:
        config.clear_state_signal()
        return {"configured": False,
                "note": "State signal cleared; D0/D1 are no longer recorded."}
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_state_check(duration_s: float = 5.0) -> dict:
    """Confirm the target really drives its state code, before relying on it.

    RUN THIS ONCE AFTER p1150_set_state_signal, with the target running
    normally, and ideally over a window in which it visits more than one state.

    It catches the failures that otherwise produce a capture that looks entirely
    normal and means nothing:
      * pins never driven -- the firmware change did not take, or the leads are
        on the wrong pads. Everything reads as code 0.
      * GPIO drive released in deep sleep. Several MCUs do this unless pad
        retention is configured explicitly (nRF5x retention, STM32 standby,
        ESP32 gpio_hold_en), so the pins float during exactly the state most
        worth measuring.
      * a floating or ungrounded input, which decodes as rapid nonsense.
      * a code the firmware drives that no state is declared for.

    Reports the codes seen, how long each was held, how many transitions
    occurred, and the per-channel signal survey. Watch for an implausible
    transition count: a state code should change a handful of times in five
    seconds, not thousands.
    """
    try:
        sig = config.get_state_signal()
        if not sig.get("channels"):
            return {"error": "No state signal is declared. Call "
                             "p1150_set_state_signal first "
                             "(p1150_state_signal_guide has the firmware)."}
        i, _, aux = SESSION.measure(duration_s)
        codes, names = _state_codes(aux, None)
        r = analysis.state_breakdown(i, codes, names, SAMPLE_RATE)
        out = {"duration_s": duration_s, "channels": sig["channels"],
               "states": r["states"], "transitions": r["transitions"],
               "mean_current_ma": round(float(i.mean()), 6) if i.size else None}
        for k in ("unmapped_codes", "unmapped_note"):
            if r.get(k):
                out[k] = r[k]
        # Per-channel view, because "code 0 throughout" cannot distinguish a
        # target genuinely asleep from a lead that is not connected, and the
        # raw levels can.
        out["channels_detail"] = {
            ch: dict(analysis.marker_survey(aux[ch], config.aux_channel_cfg(ch),
                                            SAMPLE_RATE, ch),
                     bit=sig["channels"].index(ch))
            for ch in sig["channels"] if ch in aux}

        rate = r["transitions"] / duration_s if duration_s else 0
        if rate > 1000:
            out["verdict"] = "NOISY"
            out["action"] = (
                f"The code changed {r['transitions']} times in {duration_s:g} s, "
                f"which is not a state machine. An input is floating or the "
                f"grounds are not common. Check the wiring before anything "
                f"else.")
        elif len(r["states"]) == 1 and r["states"][0]["code"] == 0:
            out["verdict"] = "STUCK_AT_ZERO"
            out["action"] = (
                "Every sample read code 0. Either the target genuinely stayed "
                "in the code-0 state for the whole window -- exercise another "
                "state and re-check -- or the pins are not being driven at all: "
                "the firmware change did not take, the GPIOs were never "
                "configured as outputs, or the leads are on the wrong pads. "
                "channels_detail shows the levels actually seen.")
        elif len(r["states"]) == 1:
            out["verdict"] = "SINGLE_STATE"
            out["action"] = (
                f"Only code {r['states'][0]['code']} was seen. The signal is "
                f"working; exercise the other states and re-check if you want "
                f"them all confirmed.")
        else:
            out["verdict"] = "OK"
            out["note"] = (
                f"{len(r['states'])} states seen with {r['transitions']} "
                f"transitions. Ready to measure -- p1150_measure_states "
                f"captures them all at once.")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_get_usage() -> dict:
    """Show the usage model: the product's states, their share of the time, the
    events that happen on a rate, and which states have a measured baseline.

    Reports whether the state fractions total 100% -- they must, before an
    estimate can be made -- and which states still need measuring.
    """
    try:
        usage = config.get_usage()
        baselines = _state_baselines()
        if not usage["states"] and not usage["events"]:
            return {
                "configured": False, "states": {}, "events": {},
                "note": "No usage model, so battery life cannot be estimated "
                        "-- only the current of whatever state a capture "
                        "happens to catch. Ask the developer how the product "
                        "spends its time and declare it with "
                        "p1150_set_usage_state; p1150_battery_life_guide has "
                        "the questions to ask and why they cannot be inferred."}
        states = {}
        for key, s in usage["states"].items():
            meta = baselines.get(key)
            row = dict(s)
            if meta:
                row.update({"run_id": meta.get("run_id"),
                            "avg_ma": meta.get("avg_ma"),
                            "measured": meta.get("created"),
                            "baseline_verdict": meta.get("baseline_verdict"),
                            "voltage_mv": meta.get("voltage_mv")})
            else:
                row["measured"] = None
            states[key] = row
        total = config.usage_fraction_total()
        out = {"configured": True, "states": states, "events": usage["events"],
               "fraction_total_pct": round(total, 3),
               "fraction_total_ok": abs(total - 100.0) <= 0.5}
        missing = [k for k, v in states.items() if not v.get("measured")]
        if missing:
            out["unmeasured_states"] = missing
            out["action"] = (
                f"No baseline yet for: {', '.join(missing)}. For each, ask the "
                f"developer to put the target into the state, let it settle, "
                f"and confirm -- then p1150_measure_state(state='{missing[0]}', "
                f"duration_s=30).")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_clear_usage(name: str = None) -> dict:
    """Remove one state or event from the usage model, or clear the whole thing.

    Use when the product's usage model changes, or when a state turns out to be
    two states. Stored captures are untouched -- only the model is cleared.
    """
    try:
        cfg = config.clear_usage(name)
        total = config.usage_fraction_total()
        return {"states": cfg["states"], "events": cfg["events"],
                "fraction_total_pct": round(total, 3),
                "note": (f"'{name}' removed. The declared states now total "
                         f"{total:.1f}% of the time."
                         if name else "Usage model cleared.")}
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
# Ammeter                                                              #
# ------------------------------------------------------------------ #
@mcp.tool()
def p1150_ammeter(window_s: float = 60.0) -> dict:
    """What the target is drawing right now. Free, instant, no capture.

    The P1150 sends a current reading once a second, unasked, for the whole time
    it is connected; this returns the newest one. It costs nothing, takes no
    time, stores no run, and works while a capture is already running -- so it
    is the right way to answer "is the target awake", "did that change do
    anything", "is it still alive" without interrupting anything.

    IT IS A ONE-SECOND AVERAGE, AND THAT IS ITS LIMIT. It cannot show a peak, a
    burst, a spike or a shape. A target that sleeps at 10 uA and transmits 80 mA
    for 2 ms every second reads about 170 uA here -- a number that is real, and
    that describes neither state. Do not use it to characterise a state, to
    quote a sleep current, or as an input to battery life: those need
    p1150_measure and the method in p1150_measurement_guide. If a reading
    surprises you, capture it rather than repeating it.

    Nor is it a substitute for p1150_measure when the answer matters. The point
    of this tool is that it is already there, not that it is equivalent.

    Reads it correctly:
      * current_ma -- the mean over the second just elapsed.
      * settled -- false when the output was changed less than a second ago, so
        the reading spans the change and is a blend of before and after. Wait a
        second. This is measured behaviour, not a theoretical caution.
      * stale -- true when the once-a-second stream has stopped, which is what a
        wedged or unplugged unit looks like from here.
      * probe_connected -- FALSE MEANS THIS IS NOT THE TARGET'S CURRENT. With
        the probe open the reading is near zero, which is indistinguishable from
        an excellent sleep current. Check this before believing a low number.

    window_s also reports min, max and mean over the last window_s of readings,
    up to ten minutes, held in memory since the moment the P1150 connected. That
    history is free too: asking for the last five minutes does not wait five
    minutes. A max well above the mean means the target is doing something
    bursty, which is the signal to go and capture it properly.
    """
    try:
        return SESSION.ammeter_read(window_s)
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_ammeter_watch(duration_s: float = 10.0) -> dict:
    """Watch the target's current for duration_s and report it second by second.

    Blocks for duration_s, then returns one reading per second as a small series
    plus its min, max and mean. Use it to watch while something happens that you
    want to see the effect of but do not need the shape of: the developer
    pressing a button, a radio joining a network, a target settling after boot,
    a firmware image being flashed and coming back up.

    Prefer p1150_ammeter when you want the current NOW -- readings accumulate in
    the background whether anything is watching or not, so its window already
    covers the last ten minutes and blocking for a minute to learn about the
    last minute wastes a minute.

    Every caveat on p1150_ammeter applies to every point in this series: each is
    a one-second average that hides whatever happened inside its second. A
    series that steps 0.01 -> 0.01 -> 45 -> 0.01 tells you something woke up and
    roughly what it cost on average; it does not tell you the peak, and the peak
    is what trips an over-current limit and browns out a battery. Capture that
    with p1150_measure or p1150_capture_single.

    duration_s is capped at 300. For anything longer, start a real capture with
    p1150_capture_start -- it records every sample rather than one a second.
    """
    try:
        d = max(1.0, min(float(duration_s), 300.0))
        return SESSION.ammeter_watch(d)
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
def p1150_measure_state(state: str, duration_s: float = 30.0,
                        label: str = None, wait_s: float = 60.0) -> dict:
    """Capture the representative current of one state the product lives in,
    and check the capture is actually fit to stand for it.

    This is the building block of a battery-life estimate and where a new
    project starts. Life is capacity / SUM(fraction x current): this tool
    measures the currents, p1150_set_usage_state records the fractions, and
    p1150_battery_life combines them. Read p1150_battery_life_guide before the
    first one.

    IF THE TARGET SIGNALS ITS OWN STATE (p1150_set_state_signal), none of the
    next two paragraphs applies: this waits until the firmware declares it is in
    the state, keeps only the samples where it says so, and discards the rest.
    The state becomes a fact rather than a claim. When the firmware can be
    edited -- and if you are writing it, it can -- adding that signal is worth
    more than anything else here; p1150_state_signal_guide has the fifteen lines.

    OTHERWISE, ASK THE DEVELOPER TO PUT THE TARGET INTO THE STATE, AND WAIT FOR
    THEM TO CONFIRM IT IS THERE. Only they can do it -- press the button, close
    the app, stop the stream -- and a capture of the wrong state is not a small
    error: it gets multiplied by that state's whole share of the product's life.
    Say which state you are about to measure and wait.

    THEN LET IT SETTLE. Targets descend into their lowest state in steps over
    minutes rather than at once -- an inactivity timeout drops a radio link at
    30 s, supervision intervals widen, a sensor cools, a regulator changes mode.
    A standby capture taken straight after boot commonly reads several times the
    real figure, and nothing about it looks wrong. Wait a few minutes for a
    sleep state, then measure.

    Unlike p1150_measure, the capture is judged as a baseline and the verdict is
    stored with it:
      USABLE       steady, and long enough. Good to build an estimate on.
      NOT_SETTLED  the current drifted steadily across the capture, so the
                   target was still on its way into the state. Reports the
                   current it was converging on -- wait longer and re-measure.
      TOO_SHORT    the state has a duty cycle of its own (a "standby" that
                   advertises once a second is not flat) and too few periods
                   were captured for the average to be stable. Reports the
                   duration to use instead.
      UNSTABLE     the average moved a lot without a consistent trend. Either
                   the workload varies -- capture long enough to average over it
                   -- or this is really two states and should be declared as
                   two.

    state: the name declared with p1150_set_usage_state, e.g. "standby". A state
        not yet in the usage model can still be measured; it just needs its
        share of the time before an estimate can use it.

    duration_s: 10-30 s for a genuinely flat sleep; 30-60 s for a state
        duty-cycled around 1 Hz; at least twelve periods for anything slower.
        Longer is not better -- past the point where the average stops moving,
        it only makes a larger file.

    label: defaults to the state name. Set it when keeping several captures of
        the same state, e.g. "standby-after-fix". The newest capture of a state
        is the one an estimate uses, whatever it is labelled.

    wait_s: only used when the target signals its own state
        (p1150_set_state_signal). The capture then WAITS until the target
        declares it is in this state and keeps only the samples where it says
        so, discarding the rest -- so the measurement is of the state by
        construction rather than by anyone's say-so, and nobody has to press
        anything. This is how to measure a state whose entry you cannot time by
        hand. Give up after wait_s seconds.
    """
    try:
        name = (state or "").strip()
        if not name:
            return {"error": "A state name is required, e.g. 'standby'."}

        signal = config.get_state_signal()
        code = (signal.get("codes") or {}).get(name.lower())
        gate = {}
        if code is None:
            i, isnk, aux = SESSION.measure(duration_s)
        else:
            # The target says when it is in the state, so wait for it to say so
            # and then keep only what it vouches for.  Both halves matter: the
            # wait removes the operator, and the gate removes the samples either
            # side of the state that a fixed window would otherwise average in.
            channels = signal["channels"]
            seen = set()

            def _code_of(chunk):
                c = analysis.decode_state_codes(
                    {ch: chunk[ch] for ch in channels}, channels)
                return int(c[-1]) if c.size else None

            def _match(chunk):
                return _code_of(chunk) == int(code)

            def _note(chunk):
                c = _code_of(chunk)
                if c is not None:
                    seen.add(c)

            try:
                i, isnk, aux, waited = SESSION.measure_when(
                    duration_s, _match, wait_s, on_reject=_note)
            except Exception as e:
                names = {int(c): n for n, c in signal["codes"].items()}
                return {"error": str(e),
                        "wanted": {"state": name, "code": int(code)},
                        "codes_seen_while_waiting":
                            sorted(f"{c} ({names.get(c) or 'undeclared'})"
                                   for c in seen),
                        "action": (
                            "The target never entered this state. Either it "
                            "does not reach it under the current conditions, or "
                            "the firmware does not set the code for it. If the "
                            "codes seen are all 0, check the pins are actually "
                            "driven with p1150_state_check.")}
            codes = analysis.decode_state_codes(aux, channels)
            mask = codes == int(code)
            kept = int(mask.sum())
            if not kept:
                return {"error": f"The target left the '{name}' state before "
                                 f"any of it could be recorded."}
            starts, _ = analysis.intervals(mask)
            gate = {"gated_on": channels, "state_code": int(code),
                    "waited_s": round(waited, 2),
                    "kept_s": round(kept / SAMPLE_RATE, 4),
                    "discarded_s": round((mask.size - kept) / SAMPLE_RATE, 4),
                    "visits": int(starts.size)}
            # Everything is masked with the same mask, so the stored run is the
            # state and nothing else: its duration and average are the state's,
            # and a later re-analysis cannot accidentally include the approach
            # to it.
            i, isnk = i[mask], (isnk[mask] if isnk is not None else None)
            aux = {k: v[mask] for k, v in aux.items()}

        check = analysis.baseline_check(i, SAMPLE_RATE)
        out = _store(label or name, i,
                     dict({"capture_type": "state_baseline",
                           "state": name,
                           "baseline_verdict": check.get("verdict")}, **gate),
                     isnk_ma=isnk, aux=aux)
        if "error" in out:
            return out
        out["baseline"] = check
        if gate:
            out["gate"] = gate
            if gate["visits"] > 1:
                out["gate"]["note"] = (
                    f"The target entered and left this state {gate['visits']} "
                    f"times during the capture; the kept samples are joined end "
                    f"to end, so the average is right but the timing across a "
                    f"join is not. If the state is meant to be held "
                    f"continuously, something is interrupting it.")
        declared = config.get_usage()["states"].get(name.lower())
        if not declared:
            out["usage_hint"] = (
                f"'{name}' is not in the usage model yet, so this measurement "
                f"cannot be weighted. Ask the developer what share of the "
                f"product's time is spent in it and call "
                f"p1150_set_usage_state('{name}', hours_per_day=...).")
        elif check.get("verdict") == "USABLE":
            baselines = _state_baselines()
            missing = [k for k in config.get_usage()["states"]
                       if not baselines.get(k)]
            out["usage_hint"] = (
                f"Still to measure: {', '.join(missing)}."  if missing else
                "Every declared state now has a baseline -- run "
                "p1150_battery_life() for the estimate.")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_measure_states(duration_s: float = 60.0,
                         label: str = "states",
                         exclude_entry_transient: bool = False) -> dict:
    """Measure every state at once, from one capture of the target doing its
    real work.

    Requires the target to signal its own state (p1150_set_state_signal). Let it
    run normally for long enough to pass through everything it does, and this
    splits the capture by the code the firmware was driving: a current, a
    charge, a peak and a time for each state, from a single measurement, with no
    one having to put the target into anything.

    This is the fastest route to a battery-life estimate that exists here, and
    it is more accurate than measuring the states one at a time -- every state
    is measured on the same board, in the same session, at the same supply
    voltage, with the same firmware build, so the terms being added together are
    genuinely comparable.

    Each state's current is filed as its baseline, so p1150_battery_life picks
    them all up afterwards. Only the time weighting is still needed; if the
    device is self-driven (a sensor node, a beacon, a tracker) the measured
    times may be the weighting too -- see p1150_usage_from_capture, and read its
    caveat before adopting them.

    duration_s: long enough to contain several full cycles of whatever the
        target does. 60 s is a reasonable start; a device that wakes once a
        minute needs several minutes. A state the target never enters during
        the capture simply will not appear.

    exclude_entry_transient: leave False unless the transitions BETWEEN states
        are declared separately with p1150_set_usage_event. Entering a state
        costs something -- a radio shutting down, a regulator changing mode --
        and by default that cost stays inside the state, which is the safe
        choice because it can only overstate drain. Setting True moves it out,
        and it then belongs to a transition event that has to exist, or the
        cost silently disappears from the estimate.
    """
    try:
        signal = config.get_state_signal()
        if not signal.get("channels"):
            return {"error":
                    "No state signal is declared, so a capture cannot be split "
                    "by state. This needs about fifteen lines in the target's "
                    "firmware to drive a code on two spare GPIOs -- "
                    "p1150_state_signal_guide has them. Without it, measure the "
                    "states one at a time with p1150_measure_state, asking the "
                    "developer to put the target into each."}
        i, isnk, aux = SESSION.measure(duration_s)
        codes, names = _state_codes(aux, None)
        r = analysis.state_breakdown(i, codes, names, SAMPLE_RATE,
                                     config.capacity_mah())

        key = "settled_mean_ma" if exclude_entry_transient else "mean_ma"
        currents, times = {}, {}
        for row in r["states"]:
            if not row["state"]:
                continue
            currents[row["state"]] = row.get(key) or row["mean_ma"]
            times[row["state"]] = row["time_pct"]

        out = _store(label, i,
                     {"capture_type": "state_sweep",
                      "state_currents": currents,
                      "state_times_pct": times,
                      "state_current_basis": key},
                     isnk_ma=isnk, aux=aux)
        if "error" in out:
            return out
        out["states"] = r["states"]
        out["transitions"] = r["transitions"]
        for k in ("unmapped_codes", "unmapped_note", "dominant_state"):
            if r.get(k):
                out[k] = r[k]

        declared = set(config.get_usage()["states"])
        seen = {s.lower() for s in currents}
        never = sorted(declared - seen)
        if never:
            out["states_not_seen"] = never
            out["action"] = (
                f"The target never entered: {', '.join(never)}. Either it does "
                f"not reach those states under the conditions of this capture "
                f"-- exercise them and re-run, or measure each with "
                f"p1150_measure_state -- or the firmware does not set a code "
                f"for them.")
        elif declared:
            out["action"] = ("Every declared state was measured. "
                             "p1150_battery_life() will use these.")
        if not declared:
            out["usage_hint"] = (
                "None of these states has a share of the product's time yet, "
                "so no estimate can be made from them. Either ask the developer "
                "for the split (p1150_set_usage_state), or, if this capture is "
                "representative of how the product actually runs, adopt the "
                f"measured times with p1150_usage_from_capture('{out['run_id']}')"
                " -- read its caveat first.")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_state_split(run_id: str, exclude_entry_transient: bool = False) -> dict:
    """Split any stored capture by the state the target said it was in.

    Works on any run taken while a state signal was declared -- including an
    ordinary p1150_measure or a background capture, which record the code
    alongside the current whether or not anyone was thinking about states at the
    time. A capture taken to look at something else will often answer "and what
    does it draw in each mode" for free.

    Reports per state: time held, share of the capture, mean and settled mean
    current, peak, floor, charge, how many times it was visited, and how much
    the entry transient lifts the average. Plus the number of transitions and
    any code the firmware drove that no state is declared for.
    """
    try:
        i, _, aux, meta = storage.load_all(run_id)
        codes, names = _state_codes(aux, meta)
        out = analysis.state_breakdown(i, codes, names, _fs(meta),
                                       config.capacity_mah())
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        if exclude_entry_transient:
            out["current_basis"] = "settled_mean_ma"
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_usage_from_capture(run_id: str, adopt: bool = False) -> dict:
    """Read the time spent in each state from a capture, and optionally adopt it
    as the product's usage model.

    THINK BEFORE ADOPTING. What this measures is how the target behaved DURING
    THE CAPTURE. That is the product's real duty cycle only if the workload on
    the bench is the workload in the field:

      Self-driven devices -- a sensor node on a timer, a beacon, a tracker, a
      logger -- yes. The firmware's own schedule is the whole story, and what it
      does on the bench is what it does in service. Adopting the measured times
      is better than any estimate a person would give.

      User-driven devices -- a wearable, a handheld, anything with a button or
      an app -- no. A device measured for a minute on a desk spends none of that
      minute being used, and adopting these fractions would state that the
      product is never used, giving a battery life far longer than the truth.

    ASK THE DEVELOPER WHICH KIND IT IS. It is one question, and it decides
    whether the usage model is measured or declared. Do not adopt on your own
    judgement.

    adopt: False (default) reports the measured fractions without changing
        anything. True writes them into the usage model, replacing whatever
        fractions were there.
    """
    try:
        i, _, aux, meta = storage.load_all(run_id)
        codes, names = _state_codes(aux, meta)
        r = analysis.state_breakdown(i, codes, names, _fs(meta))
        rows = [s for s in r["states"] if s["state"]]
        if not rows:
            return {"error": f"Run '{run_id}' contains no named states. "
                             f"Codes seen: {r.get('codes_seen')}."}
        measured = {s["state"]: s["time_pct"] for s in rows}
        out = {"run_id": run_id, "duration_s": r["duration_s"],
               "measured_fractions_pct": measured,
               "unnamed_time_pct": round(
                   100.0 - sum(measured.values()), 3),
               "adopted": False}
        if r.get("unmapped_codes"):
            out["unmapped_codes"] = r["unmapped_codes"]
        if not adopt:
            out["caveat"] = (
                "These are the fractions the target actually spent during this "
                "capture. They are the product's duty cycle only if this "
                "workload is the real one -- true for a self-driven device, "
                "false for anything a user drives. Ask the developer which it "
                "is, then call again with adopt=True.")
            return out
        if abs(out["unnamed_time_pct"]) > 0.5:
            return dict(out, error=(
                f"{out['unnamed_time_pct']:.1f}% of the capture was in a code "
                f"with no state declared, so adopting these fractions would "
                f"leave that time unaccounted for and no estimate could be "
                f"made. Declare the missing code(s) first."))
        for name, pct in measured.items():
            config.set_usage_state(name, fraction_pct=pct)
        out["adopted"] = True
        out["fraction_total_pct"] = round(config.usage_fraction_total(), 3)
        out["note"] = (
            f"Adopted as the usage model, from {r['duration_s']} s of measured "
            f"behaviour. Tell the developer these fractions came from a bench "
            f"capture, not from them -- they are the one person who can say "
            f"whether that is how the product really runs.")
        return out
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
@_bandwidth_doc
def p1150_summary(run_id: str, bandwidth_hz: float = None) -> dict:
    """Headline metrics for a stored run: average current (mA), accumulated
    charge (mAh), resting floor, peak, percentiles, and -- if a battery capacity
    is configured -- projected battery life.
    """
    try:
        i, fs, band, meta = _read(run_id, bandwidth_hz)
        out = analysis.summarize(i, fs, config.capacity_mah())
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        if band:
            out["bandwidth"] = band
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_baseline_check(run_id: str) -> dict:
    """Judge whether a stored capture is fit to represent a state in a
    battery-life estimate.

    p1150_measure_state runs this automatically; use it directly on an older
    capture, or after re-analysing one. It catches the two ways a baseline goes
    wrong without looking wrong: a target that had not finished settling into
    the state (targets step down into low-power modes over minutes), and a
    capture too short to cover enough of the state's own repetitions for its
    average to be stable. Verdicts are USABLE, NOT_SETTLED, TOO_SHORT and
    UNSTABLE, each with what to do about it.
    """
    try:
        i, meta = storage.load(run_id)
        out = analysis.baseline_check(i, _fs(meta))
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        out["state"] = meta.get("state")
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
def p1150_battery_life(target_days: float = None) -> dict:
    """Estimate how long the battery lasts, from the measured states and the
    declared usage model -- and say what to optimise.

    This is the answer to "how long will the battery last", and the reason none
    of the per-capture numbers are. A capture measures the state the target
    happened to be in; a product moves between states, so life is
    capacity / SUM(fraction x current) plus the events amortised over the day.
    The instrument supplies the currents and the developer supplies the
    weights.

    Needs, and reports what is missing: a battery capacity (p1150_set_battery),
    states totalling 100% of the time (p1150_set_usage_state), and one capture
    per state (p1150_measure_state). Each state uses its newest capture, so
    after optimising something, re-measure only the state that changed and run
    this again.

    Beyond the headline figure it returns the four things that decide what
    happens next:

      contributors        what is actually spending the battery, ranked. This
                          routinely contradicts intuition -- a state occupying
                          99% of the time can be a third of the drain, and a
                          40 mA burst lasting 8 ms can be irrelevant. Work on
                          anything but the top row or two is wasted.

      optimisation_payoff for each contributor, the life if its cost were
                          halved and if it were removed entirely. The second is
                          the ceiling on what any amount of work on it can
                          achieve, and it is much cheaper to read before the
                          work than after.

      duty_cycle_sensitivity
                          how much the answer moves if the developer's time
                          split is out by 2x. Read it before quoting a figure
                          to anyone. Often it barely moves, and then the guess
                          never needed to be precise -- which is worth saying.

      target              PASS / MARGINAL / SHORT against the required life,
                          and when short, what each contributor would have to
                          become to get there: "sleep current from 180 uA to
                          95 uA", or "boots from 20 a day to 6". A contributor
                          marked unreachable cannot get there even at zero,
                          because the others already exceed the budget -- a
                          design finding rather than a firmware one.

    target_days: override the required life for this call. Better recorded once
        with p1150_set_battery(target_days=...).

    The estimate uses RATED capacity and excludes self-discharge, temperature
    and ageing, so it is optimistic -- a target that browns out at 3.3 V may
    reach that with 15-25% of the rated charge left in the cell. Where the
    number has to be defensible rather than indicative, quote it again with 20%
    off the capacity and give the range.
    """
    try:
        usage = config.get_usage()
        if not usage["states"] and not usage["events"]:
            return {"error":
                    "No usage model, so battery life cannot be estimated. Ask "
                    "the developer how the product spends its time -- which "
                    "states it has and how many hours a day in each -- and "
                    "declare them with p1150_set_usage_state. It cannot be "
                    "measured or inferred from the firmware; "
                    "p1150_battery_life_guide has the questions to ask."}

        total_pct = config.usage_fraction_total()
        if usage["states"] and abs(total_pct - 100.0) > 0.5:
            return {"error":
                    f"The declared states account for {total_pct:.1f}% of the "
                    f"product's time, not 100%, so an estimate is undefined: "
                    f"the unaccounted time could be at any current and cannot "
                    f"be assumed to be cheap. "
                    + (f"Ask the developer what the device is doing for the "
                       f"other {100.0 - total_pct:.1f}% -- most often it is the "
                       f"resting state, left out because it felt too obvious "
                       f"to mention." if total_pct < 100.0 else
                       "Two states are overlapping, or one fraction is wrong."),
                    "states": usage["states"],
                    "fraction_total_pct": round(total_pct, 3)}

        baselines = _state_baselines()
        states, missing, shaky, voltages, stamps = [], [], [], set(), []
        for key, s in usage["states"].items():
            meta = baselines.get(key)
            if not meta:
                missing.append(s.get("name") or key)
                continue
            if meta.get("baseline_verdict") not in (None, "USABLE"):
                shaky.append(f"{s.get('name') or key} "
                             f"({meta['baseline_verdict']})")
            if meta.get("voltage_mv"):
                voltages.add(meta["voltage_mv"])
            if meta.get("created"):
                stamps.append(meta["created"])
            states.append({"name": s.get("name") or key,
                           "fraction_pct": s.get("fraction_pct"),
                           "avg_ma": meta.get("avg_ma"),
                           "run_id": meta.get("run_id"),
                           "measured": meta.get("created"),
                           "baseline_verdict": meta.get("baseline_verdict")})
        if missing:
            return {"error":
                    f"No measurement yet for: {', '.join(missing)}. For each, "
                    f"ask the developer to put the target into the state, let "
                    f"it settle, and confirm -- then "
                    f"p1150_measure_state(state='{missing[0]}', "
                    f"duration_s=30).",
                    "measured_states": [s["name"] for s in states]}

        events = []
        for key, e in usage["events"].items():
            q = e.get("charge_uah")
            if q is None and e.get("run_id"):
                try:
                    q = (storage.load_meta(e["run_id"]).get("charge_mah")
                         or 0.0) * 1000.0
                except Exception:
                    q = None
            if q is None:
                missing.append(e.get("name") or key)
                continue
            events.append({"name": e.get("name") or key,
                           "per_day": e.get("per_day"),
                           "charge_uah": round(q, 4),
                           "run_id": e.get("run_id")})
        if missing:
            return {"error": f"These events have no cost recorded: "
                             f"{', '.join(missing)}. Give charge_uah, or a "
                             f"run_id of a capture containing one occurrence."}

        capacity = config.capacity_mah()
        out = analysis.battery_life(states, events, capacity,
                                    target_days or config.target_days())
        out["states_declared"] = len(states)
        out["events_declared"] = len(events)

        warnings = []
        if shaky:
            warnings.append(
                f"Built on baselines that did not pass their own check: "
                f"{'; '.join(shaky)}. NOT_SETTLED overstates a state's current, "
                f"often several-fold. Re-measure with p1150_measure_state "
                f"before relying on this figure.")
        if len(voltages) > 1:
            warnings.append(
                f"States were measured at different supply voltages "
                f"({', '.join(str(v) for v in sorted(voltages))} mV). Current "
                f"draw varies with supply voltage, so these are not comparable "
                f"terms to add together. Re-measure them all at the same "
                f"voltage, preferably the battery's nominal.")
        if stamps and (max(stamps)[:10] != min(stamps)[:10]):
            warnings.append(
                f"The state baselines were not captured on the same day "
                f"({min(stamps)[:10]} to {max(stamps)[:10]}), so some may "
                f"predate firmware changes since made. Re-measure any state "
                f"whose code has moved on -- p1150_list_runs shows when each "
                f"was taken.")
        if warnings:
            out["warnings"] = warnings
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
@_bandwidth_doc
def p1150_segment(run_id: str, bandwidth_hz: float = None) -> dict:
    """Break a run down by current level: how much TIME and how much CHARGE was
    spent in each band, from deep sleep (<10 uA) up to peak (>100 mA).

    This is the "where did the energy go" view, and it is usually the first
    thing to look at. A target can spend 99% of its time asleep and still burn
    most of its battery in 1% of the time spent transmitting -- average current
    alone cannot show that, and it changes which code is worth optimising.

    Switching ripple wrecks this view in particular: an SMPS drawing pulses
    between 0 and 200 mA to deliver a steady 20 mA load spreads its samples
    across every bucket from deep_sleep to peak, and the breakdown then
    describes the regulator rather than the firmware. Band-limit it and the
    same run resolves into the states the target was actually in.
    """
    try:
        i, fs, band, meta = _read(run_id, bandwidth_hz)
        out = analysis.segment(i, fs, config.capacity_mah())
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        if band:
            out["bandwidth"] = band
        return out
    except Exception as e:
        return _fail(e)


@mcp.tool()
@_bandwidth_doc
def p1150_events(run_id: str, threshold_ma: float = None,
                 min_duration_us: float = 100.0,
                 bandwidth_hz: float = None) -> dict:
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

    On a target fed through a switching regulator, run this band-limited. The
    detector separates two populations with a threshold, and switching ripple
    crosses any threshold thousands of times a second -- so the raw trace
    reports a torrent of microsecond "events" that are pulses of the regulator,
    and the real wake-ups are lost among them.
    """
    try:
        i, fs, band, meta = _read(run_id, bandwidth_hz)
        out = analysis.find_events(i, fs, threshold_ma,
                                   min_duration_us, config.capacity_mah())
        out["run_id"] = run_id
        out["label"] = meta.get("label")
        if band:
            out["bandwidth"] = band
            # Averaging sets a floor on what a duration can be: one output
            # sample.  Below it min_duration_us is not being applied as asked,
            # and a burst narrower than the block averages down into the floor
            # and stops being detected at all -- which would otherwise read as
            # "the wake-ups went away" after a change of bandwidth.
            if band.get("applied"):
                period_us = 1e6 / band["effective_hz"]
                if period_us > min_duration_us:
                    out["resolution_note"] = (
                        f"At {band['effective_hz']:g} Hz one sample is "
                        f"{period_us:.0f} us, so min_duration_us={min_duration_us:g} "
                        f"cannot be honoured and the shortest detectable burst is "
                        f"{period_us:.0f} us. Bursts shorter than that are averaged "
                        f"into the floor and will not appear. Raise the bandwidth "
                        f"or drop it entirely if the bursts are that fast.")
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


PLOT_POINTS = 4000       # points the rendered trace is reduced to


def _plot_envelope(a: np.ndarray, fs: float, target: int = PLOT_POINTS):
    """(x, lo, hi) for a raw trace, min/max reduced to about `target` blocks.

    Min/max rather than striding: a 1 ms burst inside a 30 s capture would fall
    between strided samples and vanish from the plot, which is exactly the
    feature the developer is looking for.  Every block contributes both its
    extremes, so the band drawn between them is the true excursion of the
    samples behind it and no spike can hide between two plotted points.

    hi is None when the capture is short enough to draw sample for sample.

    Returned as two edges rather than as one zigzag line because of what they
    are then drawn with.  A polyline alternating min, max, min, max spans the
    full height of the axis 4000 times, and rasterising that across the five or
    six decades of a log axis costs the best part of a second -- on precisely
    the long, noisy captures this exists to make viewable.  Filling between two
    edges is the same picture for a third of the time.
    """
    n = int(a.size)
    step = max(1, n // target)
    if step <= 1:
        return np.arange(n) / fs, a, None
    trim = (n // step) * step
    blocks = a[:trim].reshape(-1, step)
    return (np.linspace(0, trim / fs, blocks.shape[0]),
            blocks.min(axis=1), blocks.max(axis=1))


@mcp.tool()
@_bandwidth_doc
def p1150_plot(run_id: str, path: str = None, log_scale: bool = True,
               show_marker: bool = True, bandwidth_hz: float = None) -> dict:
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

    With bandwidth_hz the plot gets both traces: the raw envelope in grey behind
    the averaged line. That is the useful picture on a switching target -- the
    grey band is what the instrument measured, the line is what the load drew,
    and having them on one axis is what makes the band legible as ripple rather
    than as the target misbehaving.

    Waveforms are decimated to a few thousand points for the plot; the stored
    samples are untouched.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        i, _, aux, meta = storage.load_all(run_id)
        fs = _fs(meta)

        band, filt = None, None
        if bandwidth_hz:
            # Through the cache, passing the samples that are already in hand:
            # the view gets built for the tools that follow this one without
            # reading the run a second time to do it.
            y_f, fs_f, band, _ = storage.load_view(run_id, bandwidth_hz, i)
            if band.get("applied"):
                # A long capture band-limited to 1 kHz is still far more points
                # than the figure has pixels, so it gets averaged again -- by
                # averaging and not by min/max, because the whole point of this
                # trace is that it is a line. The rate it ends up at is reported
                # rather than left implied: it is the bandwidth actually on
                # screen, and below the one that was asked for.
                if y_f.size > PLOT_POINTS:
                    y_f, fs_f, more = analysis.downsample(
                        y_f, fs_f, fs_f * PLOT_POINTS / y_f.size)
                    if more.get("applied"):
                        band["display_hz"] = more["effective_hz"]
                filt = (np.arange(y_f.size) / fs_f, y_f)

        x, lo, hi = _plot_envelope(i, fs)

        if log_scale:
            lo = np.maximum(lo, 1e-4)  # keep zeros off a log axis
            if hi is not None:
                hi = np.maximum(hi, 1e-4)
            if filt:
                filt = (filt[0], np.maximum(filt[1], 1e-4))

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

        # Grey behind the averaged line when there is one, so the eye reads the
        # band as context and the line as the measurement; the ordinary colour
        # when the band is all there is.
        raw_colour = "#b4b4c4" if filt else "#1f77b4"
        raw_label = f"raw ({fs / 1000.0:g} kSa/s envelope)" if filt else None
        if hi is None:
            plt.plot(x, lo, linewidth=0.6, color=raw_colour, label=raw_label)
        else:
            # step: a block covers an interval of time and its bounds hold
            # across the whole of it, so the band is a run of rectangles.
            # Interpolating between block centres would instead draw an
            # isolated 1 ms burst as a triangle two blocks wide.
            #
            # An edge as well as a fill: where the target held a steady current
            # the two bounds coincide, and a fill with no height would draw
            # nothing at all.
            plt.fill_between(x, lo, hi, step="mid", color=raw_colour,
                             edgecolor=raw_colour, linewidth=0.5,
                             alpha=0.6 if filt else 1.0, label=raw_label)
        if filt:
            plt.plot(filt[0], filt[1], linewidth=1.0, color="#1f5fa8",
                     label=f"averaged to {band['effective_hz']:g} Hz")
            plt.legend(loc="best", fontsize=8, framealpha=0.85)
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

        # A band-limited plot gets its own filename, so it does not overwrite
        # the raw one: the two side by side are how a developer sees that the
        # band really was ripple.
        name = run_id + (f"_{band['effective_hz']:.0f}hz" if filt else "")
        out = path or os.path.join(storage.runs_dir(), name + ".png")
        plt.savefig(out, dpi=110)
        plt.close()
        res = {"run_id": run_id, "path": out}
        if marked:
            res["marker"] = marked
        if band:
            res["bandwidth"] = band
        return res
    except Exception as e:
        return _fail(e)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
