# -*- coding: utf-8 -*-
"""
MIT License

Statistics over a captured current waveform.

Nothing here touches the P1150, so every function is usable on stored runs long
after the hardware has been unplugged -- which is the point: a baseline recorded
last week has to stay comparable against a run taken today.

Currents are milliamps throughout, charge is milliamp-hours (mAh), matching how
battery capacity is specified on a datasheet.  Coulombs are deliberately absent.
"""
import numpy as np

SAMPLE_RATE = 125_000  # P1150 samples/second, fixed

# The sink channel rests at a small non-zero floor (~110 nA measured) when the
# P1150 is not actually sinking, so a bare "isnk > 0" test would call every
# discharge measurement a charge.  Anything below this counts as not sinking.
ISNK_FLOOR_MA = 0.001


# ------------------------------------------------------------------ #
# Current buckets                                                      #
# ------------------------------------------------------------------ #
# Edges in mA.  Log-spaced because a battery-powered target spans deep-sleep
# microamps to radio-transmit tens of milliamps -- five decades that linear
# buckets cannot describe.  The labels are the vocabulary the agent uses when it
# reports where the energy went, so they name behaviour, not just a range.
BUCKETS = [
    (0.0,    0.010, "deep_sleep",  "<10uA"),
    (0.010,  0.100, "sleep",       "10-100uA"),
    (0.100,  1.0,   "idle",        "0.1-1mA"),
    (1.0,    10.0,  "active",      "1-10mA"),
    (10.0,   100.0, "high",        "10-100mA"),
    (100.0,  np.inf,"peak",        ">100mA"),
]


def _f(x, nd=6):
    """numpy scalar -> plain rounded float, so results survive JSON encoding."""
    return round(float(x), nd)


def charge_mah(i_ma: np.ndarray, fs: int = SAMPLE_RATE) -> float:
    """Accumulated charge in mAh: the integral of current over the capture.

    sum(mA) / samples-per-second -> mA*s, / 3600 -> mAh.
    """
    return float(i_ma.sum(dtype=np.float64) / fs / 3600.0)


def summarize(i_ma: np.ndarray, fs: int = SAMPLE_RATE,
              battery_mah: float = None) -> dict:
    """Headline metrics for one capture."""
    n = int(i_ma.size)
    if n == 0:
        return {"error": "capture contains no samples"}

    i64 = i_ma.astype(np.float64, copy=False)
    duration_s = n / fs
    q_mah = charge_mah(i_ma, fs)
    avg = float(i64.mean())
    p05, p50, p90, p99 = (float(v) for v in np.percentile(i64, [5, 50, 90, 99]))

    out = {
        "samples":        n,
        "duration_s":     _f(duration_s, 4),
        "charge_mah":     _f(q_mah),
        "avg_ma":         _f(avg),
        # p5 is a robust stand-in for the resting floor: immune to the odd
        # startup sample, and unlike min() it is not a single ADC outlier.
        "sleep_floor_ma": _f(p05),
        "median_ma":      _f(p50),
        "p90_ma":         _f(p90),
        "p99_ma":         _f(p99),
        "peak_ma":        _f(float(i64.max())),
        "min_ma":         _f(float(i64.min())),
    }
    if battery_mah:
        out["battery_mah"] = battery_mah
        # Named for its assumption.  capacity/avg is only the product's battery
        # life if the product never leaves whatever state this capture caught,
        # and a capture of an active state projects a life shorter than the real
        # one by the whole duty cycle -- often by a factor of a hundred.  It was
        # previously called projected_hours, which invited exactly that reading,
        # and it is the first number a developer new to the instrument seizes
        # on.  A real estimate needs the time weighting, which no capture
        # contains: see battery_life() and p1150_battery_life.
        out["projected_hours_if_continuous"] = \
            _f(battery_mah / avg, 2) if avg > 0 else None
        out["projected_days_if_continuous"] = \
            _f(battery_mah / avg / 24, 2) if avg > 0 else None
        out["projection_note"] = (
            "projected_*_if_continuous assumes the target stays in this state "
            "for the whole life of the battery. For a device that has more than "
            "one state, use p1150_battery_life instead.")
        # What this capture actually cost, as a share of the pack.  Turns an
        # abstract mAh into "that boot sequence costs 0.002% of the battery".
        out["battery_used_pct"] = _f(100.0 * q_mah / battery_mah, 6)
    return out


def segment(i_ma: np.ndarray, fs: int = SAMPLE_RATE,
            battery_mah: float = None) -> dict:
    """Time and charge spent in each current bucket.

    This is the "where did the energy go" view.  A rise in average current is
    only actionable once you know whether the floor moved or the bursts did.
    """
    i64 = i_ma.astype(np.float64, copy=False)
    n = i64.size
    total_q = charge_mah(i_ma, fs)
    rows = []
    for lo, hi, key, label in BUCKETS:
        m = (i64 >= lo) & (i64 < hi)
        cnt = int(m.sum())
        if cnt == 0:
            continue
        sel = i64[m]
        q = float(sel.sum() / fs / 3600.0)
        row = {
            "bucket":     key,
            "range":      label,
            "time_s":     _f(cnt / fs, 4),
            "time_pct":   _f(100.0 * cnt / n, 2),
            "charge_mah": _f(q),
            "charge_pct": _f(100.0 * q / total_q, 2) if total_q > 0 else 0.0,
            "mean_ma":    _f(float(sel.mean())),
        }
        if battery_mah:
            row["battery_pct"] = _f(100.0 * q / battery_mah, 6)
        rows.append(row)
    dominant = max(rows, key=lambda r: r["charge_mah"])["bucket"] if rows else None
    return {
        "total_charge_mah": _f(total_q),
        "dominant_bucket":  dominant,
        "buckets":          rows,
    }


def find_events(i_ma: np.ndarray, fs: int = SAMPLE_RATE,
                threshold_ma: float = None,
                min_duration_us: float = 100.0,
                battery_mah: float = None) -> dict:
    """Detect bursts of activity above a floor, for duty-cycled profiles.

    A BLE advertiser or a periodic sensor poll is described by three numbers the
    average hides: how often it wakes, how long it stays awake, and how much
    charge each wake costs.  A regression in any one of them looks identical in
    avg_ma but has a completely different cause.

    threshold_ma defaults to 10% of the way from the resting floor (p5) up to
    the burst level (p99), which separates the two populations for most targets.
    """
    i64 = i_ma.astype(np.float64, copy=False)
    if i64.size == 0:
        return {"events": 0}

    p05, p99 = (float(v) for v in np.percentile(i64, [5, 99]))
    if threshold_ma is None:
        threshold_ma = p05 + 0.10 * (p99 - p05)
    # Flat trace: no two populations to separate, so report no events rather
    # than chopping sensor noise into thousands of "bursts".
    if p99 <= p05 * 1.5 or threshold_ma <= 0:
        return {"events": 0, "threshold_ma": _f(threshold_ma),
                "note": "no distinct bursts; current is essentially flat"}

    above = i64 > threshold_ma
    # Edge indices via diff on the boolean mask, padded so a capture that starts
    # or ends mid-burst still yields a complete pair of edges.
    edges = np.diff(np.concatenate(([0], above.view(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)

    min_samples = max(1, int(min_duration_us * 1e-6 * fs))
    keep = (ends - starts) >= min_samples
    starts, ends = starts[keep], ends[keep]
    if starts.size == 0:
        return {"events": 0, "threshold_ma": _f(threshold_ma)}

    durations_s = (ends - starts) / fs
    charges_uah = np.array([i64[a:b].sum() / fs / 3600.0 * 1000.0
                            for a, b in zip(starts, ends)])
    peaks = np.array([i64[a:b].max() for a, b in zip(starts, ends)])

    out = {
        "events":            int(starts.size),
        "threshold_ma":      _f(threshold_ma),
        "floor_ma":          _f(p05),
        "mean_duration_ms":  _f(float(durations_s.mean()) * 1000.0, 4),
        "mean_charge_uah":   _f(float(charges_uah.mean())),
        "mean_peak_ma":      _f(float(peaks.mean())),
        "max_peak_ma":       _f(float(peaks.max())),
        "total_event_charge_mah": _f(float(charges_uah.sum()) / 1000.0),
    }
    if battery_mah:
        per_event_mah = float(charges_uah.mean()) / 1000.0
        out["battery_pct_per_event"] = _f(100.0 * per_event_mah / battery_mah, 8)
        # The most tangible form of the number: how many times this operation
        # can happen before the battery is flat, ignoring everything else.
        out["events_per_battery"] = int(battery_mah / per_event_mah) \
            if per_event_mah > 0 else None
    if starts.size >= 2:
        periods_s = np.diff(starts) / fs
        out["mean_period_s"] = _f(float(periods_s.mean()), 4)
        out["period_jitter_pct"] = _f(
            100.0 * float(periods_s.std()) / float(periods_s.mean()), 2
        ) if periods_s.mean() > 0 else None
        out["rate_hz"] = _f(1.0 / float(periods_s.mean()), 4) if periods_s.mean() > 0 else None
        # Duty cycle drives battery life more directly than peak current does.
        out["duty_cycle_pct"] = _f(
            100.0 * float(durations_s.mean()) / float(periods_s.mean()), 3)
    return out


# ------------------------------------------------------------------ #
# Baseline quality                                                     #
# ------------------------------------------------------------------ #
# A capture used as a state baseline is multiplied by a fraction of the
# product's whole life, so an error in it is not a small error.  The two ways it
# goes wrong are both invisible in the summary and both detectable here.
#
# NOT SETTLED.  The target had not reached the state yet.  Boot tails, a radio
# still connected before an inactivity timeout drops it, a sensor cooling, a
# supervisory interval that widens over minutes -- targets very often descend
# into their lowest state in steps over several minutes, and a 30 s capture
# taken immediately after power-on catches a shallow intermediate state.  The
# signature is a mean that drifts monotonically across the capture, and it
# overstates drain by whatever the step was.
#
# TOO SHORT.  The state is itself duty-cycled -- a "standby" that advertises
# once a second is not flat -- and the capture caught a handful of periods, so
# the average depends on how many bursts happened to fall inside the window.
# Ten periods is the point past which that stops moving much.
BASELINE_CHUNKS = 10
BASELINE_DRIFT_PCT = 20.0        # end-to-end change worth objecting to
BASELINE_MONOTONE_FRAC = 0.7     # ...that is a trend rather than a wobble
BASELINE_UNSTABLE_PCT = 50.0     # a non-monotone swing this large is a caution
BASELINE_MIN_EVENTS = 10
BASELINE_SETTLED_TAIL = 3        # chunks averaged for the settled estimate


def baseline_check(i_ma: np.ndarray, fs: int = SAMPLE_RATE,
                   min_events: int = BASELINE_MIN_EVENTS) -> dict:
    """Judge whether a capture is fit to represent a state for a whole week.

    Returns a verdict and, when it is a bad one, what to do instead -- including
    the current the capture appears to be converging on, so a re-measurement can
    be judged against it rather than guessed at.
    """
    i64 = i_ma.astype(np.float64, copy=False)
    n = i64.size
    if n < BASELINE_CHUNKS:
        return {"verdict": "TOO_SHORT", "issues": ["TOO_SHORT"],
                "reason": "capture contains almost no samples"}

    duration_s = n / fs
    mean = float(i64.mean())
    trim = (n // BASELINE_CHUNKS) * BASELINE_CHUNKS
    chunks = i64[:trim].reshape(BASELINE_CHUNKS, -1).mean(axis=1)
    first, last = float(chunks[0]), float(chunks[-1])
    settled = float(chunks[-BASELINE_SETTLED_TAIL:].mean())

    drift_pct = 100.0 * (last - first) / mean if mean > 0 else 0.0
    diffs = np.diff(chunks)
    # How much of the change went the same way as the overall change: a target
    # still settling moves in one direction, a varying workload does not.
    direction = np.sign(last - first)
    monotone = (float(np.mean(np.sign(diffs) == direction))
                if direction and diffs.size else 0.0)
    spread_pct = (100.0 * (float(chunks.max()) - float(chunks.min())) / mean
                  if mean > 0 else 0.0)

    out = {
        "duration_s": _f(duration_s, 3),
        "mean_ma": _f(mean),
        "settled_ma": _f(settled),
        "start_ma": _f(first),
        "end_ma": _f(last),
        # Normalised by the mean rather than by the starting value, so a state
        # that settles from 1 mA to 1 uA does not report a drift of -99900%.
        # It can still exceed 100%: it is a fraction of the average, not of a
        # starting point, which is why the text below quotes both currents.
        "drift_pct": _f(drift_pct, 2),
        "trend_consistency": _f(monotone, 2),
        "spread_pct": _f(spread_pct, 2),
        "chunk_means_ma": [_f(c) for c in chunks],
    }

    issues = []
    if abs(drift_pct) > BASELINE_DRIFT_PCT and monotone >= BASELINE_MONOTONE_FRAC:
        issues.append("NOT_SETTLED")
        falling = drift_pct < 0
        out["action"] = (
            f"Current {'fell' if falling else 'rose'} steadily across the "
            f"capture, from {first:.4g} mA at the start to {last:.4g} mA at the "
            f"end, so the target was still "
            f"{'settling into' if falling else 'leaving'} this state rather "
            f"than sitting in it. "
            + (f"It was heading towards about {settled:.6g} mA. Let the target "
               f"sit in the state for a few minutes -- inactivity timeouts and "
               f"connection supervision often step the current down well after "
               f"boot -- then measure again, for longer, and expect a figure "
               f"near that." if falling else
               "Something is ramping up: check the target really is in the "
               "state being measured, and that nothing else was started during "
               "the capture."))

    ev = find_events(i_ma, fs)
    count = int(ev.get("events") or 0)
    period = ev.get("mean_period_s")
    if count and period:
        out["repetitions"] = count
        out["period_s"] = period
        if count < min_events:
            issues.append("TOO_SHORT")
            want = period * (min_events + 2)
            out.setdefault("action", "")
            out["action"] = ((out["action"] + " ") if out["action"] else "") + (
                f"This state repeats every {period:.4g} s and only {count} "
                f"repetitions were captured, so the average depends on how many "
                f"bursts happened to fall inside the window. Re-measure for at "
                f"least {want:.3g} s.")
    if "NOT_SETTLED" not in issues and spread_pct > BASELINE_UNSTABLE_PCT:
        issues.append("UNSTABLE")
        out.setdefault("action", (
            f"The average moved by {spread_pct:.0f}% across the capture without "
            f"a consistent trend, so this is not one steady state. Either the "
            f"workload varies -- in which case capture long enough to average "
            f"over that variation -- or the target passed through more than one "
            f"state and they should be declared and measured separately."))

    out["issues"] = issues
    out["verdict"] = issues[0] if issues else "USABLE"
    if not issues:
        out["action"] = None
        out["note"] = ("Steady and long enough to stand for this state in a "
                       "battery-life estimate.")
    return out


# ------------------------------------------------------------------ #
# Battery life                                                         #
# ------------------------------------------------------------------ #
# Life is capacity divided by the time-weighted average of the states, plus the
# events amortised over the day.  The arithmetic is trivial; what earns its
# place here is everything around it -- which contributor actually dominates,
# what optimising each one would buy, and whether the answer even depends on the
# duty-cycle split the developer guessed at.
HOURS_PER_DAY = 24.0
DAYS_PER_YEAR = 365.0
# Above about a year of projected life, cell self-discharge and PMIC leakage are
# comparable to the load itself and capacity/current stops being the answer.
SELF_DISCHARGE_NOTE_DAYS = 365.0
# Passing by less than this is inside the uncertainty of the duty-cycle model
# and should not be reported as a pass without saying so.
TARGET_MARGIN = 1.15
# A duty-cycle split only matters if halving or doubling it moves the answer
# more than this.
SENSITIVITY_MATTERS_PCT = 10.0


def _life_days(battery_mah: float, total_ma: float):
    if not battery_mah or total_ma <= 0:
        return None
    return battery_mah / total_ma / HOURS_PER_DAY


def battery_life(states: list, events: list, battery_mah: float = None,
                 target_days: float = None) -> dict:
    """Combine measured state currents with a declared usage model.

    states: [{"name", "fraction_pct", "avg_ma", ...}]  -- the ... is carried
        through to the report untouched, so run ids and baseline verdicts
        recorded by the caller stay attached to the row they describe.
    events: [{"name", "per_day", "charge_uah", ...}]
    """
    rows = []
    for s in states:
        frac = float(s.get("fraction_pct") or 0.0)
        ma = float(s.get("avg_ma") or 0.0)
        rows.append(dict(s, kind="state", contribution_ma=frac / 100.0 * ma))
    for e in events:
        rate = float(e.get("per_day") or 0.0)
        q_uah = float(e.get("charge_uah") or 0.0)
        # uAh per day -> mA: /1000 for mAh, /24 for an hourly average.
        rows.append(dict(e, kind="event",
                         contribution_ma=q_uah * rate / 1000.0 / HOURS_PER_DAY))

    total = sum(r["contribution_ma"] for r in rows)
    for r in rows:
        r["contribution_ma"] = _f(r["contribution_ma"])
        r["contribution_pct"] = _f(100.0 * r["contribution_ma"] / total, 2) \
            if total > 0 else 0.0
    rows.sort(key=lambda r: r["contribution_ma"], reverse=True)

    life = _life_days(battery_mah, total)
    out = {
        "average_ma": _f(total),
        "contributors": rows,
        "dominant": rows[0]["name"] if rows else None,
    }
    if battery_mah:
        out["battery_mah"] = battery_mah
    if life is not None:
        out.update({"projected_days": _f(life, 2),
                    "projected_hours": _f(life * HOURS_PER_DAY, 1),
                    "projected_years": _f(life / DAYS_PER_YEAR, 2)})

    # --- what optimising each contributor would buy ------------------ #
    # The question that follows the first estimate is always "so what do I work
    # on", and contribution_pct alone answers it only for the top row.  Removing
    # a contributor entirely is the ceiling on what any amount of work on it can
    # achieve, which is the number that stops effort going into a rail that
    # cannot pay for itself however well it is optimised.
    if life is not None and total > 0:
        payoff = []
        for r in rows:
            c = r["contribution_ma"]
            halved = _life_days(battery_mah, total - c / 2.0)
            removed = _life_days(battery_mah, total - c)
            payoff.append({
                "name": r["name"],
                "kind": r["kind"],
                "days_if_halved": _f(halved, 2) if halved else None,
                "days_if_eliminated": _f(removed, 2) if removed else None,
                "gain_days_if_halved": _f(halved - life, 2) if halved else None,
            })
        payoff.sort(key=lambda p: p["gain_days_if_halved"] or 0, reverse=True)
        out["optimisation_payoff"] = payoff

    # --- does the duty-cycle guess even matter ----------------------- #
    # The currents are instrument-accurate; the fractions are a developer's
    # estimate of how the product gets used.  Reporting three significant
    # figures off the back of "about 5% active" is false precision, so say
    # outright how much the answer moves when that guess is wrong by 2x.  It
    # frequently does not move at all, and knowing that is worth as much as
    # knowing it does.
    state_rows = [r for r in rows if r["kind"] == "state"]
    if life is not None and len(state_rows) >= 2:
        # Time taken from or given to a state has to come from somewhere, and
        # the state holding most of the time is where it comes from -- it is the
        # resting state the product falls back to.  That state is not itself
        # varied: "the device might be asleep 50% of the time rather than 99.9%"
        # is not a developer misestimating a duty cycle, it is a different
        # product, and reporting it would swamp the rows that describe a real
        # uncertainty.  The quantity actually being guessed at is always the
        # small fraction.
        other = max(state_rows,
                    key=lambda o: float(o.get("fraction_pct") or 0.0))
        f_o = float(other.get("fraction_pct") or 0.0)
        sens, worst = [], 0.0
        for r in state_rows:
            if r is other:
                continue
            f_r = float(r.get("fraction_pct") or 0.0)
            row = {"state": r["name"], "fraction_pct": _f(f_r, 3),
                   "time_taken_from": other["name"]}
            for tag, mult in (("at_half_the_time", 0.5),
                              ("at_double_the_time", 2.0)):
                delta = f_r * (mult - 1.0)
                clamped = delta > f_o
                if clamped:
                    delta = f_o
                shifted = total \
                    + delta / 100.0 * float(r.get("avg_ma") or 0.0) \
                    - delta / 100.0 * float(other.get("avg_ma") or 0.0)
                d = _life_days(battery_mah, shifted)
                cell = {"days": _f(d, 2) if d else None}
                if clamped:
                    cell["note"] = (f"limited by the time available in "
                                    f"'{other['name']}'")
                row[tag] = cell
                if d:
                    worst = max(worst, abs(d - life) / life * 100.0)
            sens.append(row)
        if sens:
            out["duty_cycle_sensitivity"] = sens
            out["duty_cycle_sensitivity_note"] = (
                f"Halving or doubling how much time the product spends in any "
                f"state other than '{other['name']}' moves the estimate by at "
                f"most {worst:.0f}%, so the split does not need to be precise "
                f"-- the measured currents dominate it."
                if worst < SENSITIVITY_MATTERS_PCT else
                f"The estimate moves by up to {worst:.0f}% when a state's share "
                f"of the time is out by 2x, so it is only as good as that "
                f"split. If the developer was estimating it, ask what bounds "
                f"they are confident in and quote the range rather than the "
                f"single figure.")

    # --- against the requirement ------------------------------------- #
    if target_days and life is not None:
        required_ma = battery_mah / (target_days * HOURS_PER_DAY)
        verdict = ("PASS" if life >= target_days * TARGET_MARGIN else
                   "MARGINAL" if life >= target_days else "SHORT")
        tgt = {
            "target_days": target_days,
            "verdict": verdict,
            "projected_days": _f(life, 2),
            "required_average_ma": _f(required_ma),
            "actual_average_ma": _f(total),
            "margin_pct": _f(100.0 * (life - target_days) / target_days, 1),
        }
        if verdict == "MARGINAL":
            tgt["note"] = (
                f"It clears {target_days:g} days by "
                f"{100.0 * (life - target_days) / target_days:.0f}%, which is "
                f"inside the uncertainty of the usage model itself. Treat it as "
                f"not yet proven rather than as a pass.")
        if verdict == "SHORT":
            # Stated as what each contributor would have to become, holding the
            # others fixed.  "Cut the sleep floor to 40 uA" is actionable where
            # "cut total current by 38%" is not, and an unreachable row says
            # plainly that this one is not the way to the target.
            need = []
            for r in rows:
                c = r["contribution_ma"]
                headroom = required_ma - (total - c)
                item = {"name": r["name"], "kind": r["kind"]}
                if headroom <= 0:
                    item["required"] = "unreachable"
                    item["note"] = (
                        "Everything else already exceeds the budget, so this "
                        "contributor cannot reach the target even at zero.")
                elif r["kind"] == "state":
                    frac = float(r.get("fraction_pct") or 0.0)
                    if frac > 0:
                        item["present_ma"] = r.get("avg_ma")
                        item["required_ma"] = _f(headroom / (frac / 100.0))
                        item["reduction_pct"] = _f(100.0 * (1 - headroom / c), 1)
                else:
                    rate = float(r.get("per_day") or 0.0)
                    if rate > 0:
                        item["present_charge_uah"] = r.get("charge_uah")
                        item["required_charge_uah"] = _f(
                            headroom * 1000.0 * HOURS_PER_DAY / rate)
                        item["reduction_pct"] = _f(100.0 * (1 - headroom / c), 1)
                        item["or_reduce_rate_to_per_day"] = _f(
                            headroom * 1000.0 * HOURS_PER_DAY /
                            float(r.get("charge_uah") or 1.0), 2)
                need.append(item)
            tgt["to_reach_target"] = need
        out["target"] = tgt

    notes = []
    if life is not None and life > SELF_DISCHARGE_NOTE_DAYS:
        notes.append(
            f"At {life / DAYS_PER_YEAR:.1f} years the load is comparable to the "
            f"cell's own self-discharge, which this figure does not include. "
            f"For a design at this level, take the self-discharge rate from the "
            f"cell datasheet (a few percent a year for lithium primaries, much "
            f"more for NiMH) and treat the shelf life as the real ceiling.")
    if not battery_mah:
        notes.append(
            "No battery capacity is configured, so only the weighted average "
            "current could be computed. Ask the developer for the pack's mAh "
            "rating and record it with p1150_set_battery.")
    if notes:
        out["notes"] = notes
    return out


# ------------------------------------------------------------------ #
# Inrush                                                               #
# ------------------------------------------------------------------ #
# An inrush is the surge drawn the instant a load is energised: bulk capacitance
# charging, a DC-DC converter starting up, a motor or an LED driver kicking in.
# It is defined by its shape rather than by its size alone -- a low current
# state, a very brief excursion far above it, and a settled state afterwards
# that is low again.
#
# It is worth reporting unprompted, because the developer almost certainly is
# not looking for it.  A real battery cannot supply a surge like this: it sags
# by I*ESR instead, and that sag is what resets the target.  The P1150 is a low
# impedance supply, so it delivers the surge rather than sagging, and the
# current appears on the trace where a battery would have hidden it behind a
# brown-out.  A board that boots perfectly on the bench, or on a fresh warm
# cell, can reset every time on an aged cell on a cold morning at a low state of
# charge -- the three conditions that together put a cell's ESR at its highest.
# Without an instrument at the battery terminals there is nothing to see but an
# intermittent reset that will not reproduce indoors.
#
# TWO KINDS, AND THEY ARE NOT EQUALLY IMPORTANT.
#
# The one everybody thinks of is at power-up, when the battery is first
# connected.  In a real product that happens once, on the assembly line: the
# battery is soldered or clipped in and stays there.  A surge that only ever
# happens then is usually acceptable, and saying otherwise is crying wolf.
#
# The one that actually bites is a rail being switched.  Power-gating an LDO or
# an SMPS to save current is standard practice in a battery product, and every
# time firmware enables that regulator, the decoupling capacitance downstream of
# it is an effective short circuit at the instant of enable -- the current is
# limited only by resistance in the path, so it goes as high as the supply will
# allow.  That repeats for the life of the product, on every duty cycle, at
# every temperature and state of charge.  The remedy is a regulator with a
# soft-start, which ramps its output instead of stepping it; parts that have one
# cost the same as parts that do not, but the choice is made at schematic time
# and is expensive to revisit once boards exist.  Which is exactly why finding
# this during firmware development is worth so much: nobody discovers it until
# the hardware is built, and by then a soft-start part is a respin.
#
# So a recurring surge outranks a power-on surge in everything below.

INRUSH_PEAK_MA = 1000.0         # a one-off excursion below 1 A is not this problem
INRUSH_MAX_DURATION_MS = 4.0    # longer than this is a load, not an inrush
INRUSH_SETTLED_RATIO = 5.0      # the peak must stand this far above what follows
INRUSH_MERGE_GAP_MS = 0.5       # sub-peaks closer than this are one event
INRUSH_SETTLE_WINDOW_MS = 20.0  # window defining the "before" and "after" levels
INRUSH_EDGE_FRACTION = 0.10     # width is measured at 10% of the peak
INRUSH_SEARCH_MS = 50.0         # cap on how far the width search walks outward

# A surge that repeats is reported from a lower bar than a one-off, because
# repetition is itself most of the evidence.  A switched rail on a small circuit
# need not reach an amp to be the same design fault, and unlike a single spike
# it cannot be dismissed as a startup transient -- it has a cause that fires
# again every duty cycle.  Held well above the settled floor so an ordinary
# duty-cycled wake-up burst is not swept up in it.
INRUSH_RECURRING_PEAK_MA = 250.0
INRUSH_RECURRING_RATIO = 10.0
INRUSH_RECURRING_MIN_EVENTS = 3

# Guard against a pathological trace -- current hovering either side of the
# detection threshold -- turning into a per-region Python loop over tens of
# thousands of "events".
INRUSH_MAX_REGIONS = 5000

# How close to the start of a capture, and how near to zero beforehand, an event
# has to be before it is taken for the power-up surge rather than a switched
# rail.  Only a hint: the caller usually knows outright, and says so.
INRUSH_POWER_ON_WINDOW_MS = 5.0
INRUSH_POWER_ON_FLOOR_MA = 0.05

# The P1150's over-current protection shuts the output off when the target draws
# more than this.  3200 mA is the power-on default.
OVC_DEFAULT_MA = 3200.0
# Within this fraction of the limit, the measured peak is a lower bound rather
# than a measurement: the protection is acting, so the number on the trace is
# the instrument's limit and not the target's demand.
OVC_CLIP_FRACTION = 0.90

# Severity of a spike relative to what the target settles at afterwards, which
# is the only fair reference -- 1.5 A into a device that then runs at 1.2 A is
# ordinary, and 1.5 A into one that then runs at 3 mA is not.
INRUSH_RATIO_NOTABLE = 5.0
INRUSH_RATIO_SEVERE = 20.0

# Internal resistance of a cell, in milliohms, as (fresh and warm, mid-life at
# room temperature, aged and cold and near flat).  The third column is the one
# that matters: it is where field failures happen, and it is 5-15x the first.
#
# These are order-of-magnitude figures for a small single cell, good enough to
# tell "no problem" from "this browns out in the field" -- which is the decision
# being made.  ESR also scales roughly inversely with capacity, so a 2000 mAh
# cell is several times stiffer than a 200 mAh one of the same chemistry.  A
# measured value always wins: record it with p1150_set_battery(esr_mohm=...).
CHEMISTRY_ESR_MOHM = {
    "liion":    (80.0, 250.0, 900.0),
    "lipo":     (150.0, 450.0, 1500.0),
    "lifepo4":  (60.0, 200.0, 700.0),
    "nimh":     (100.0, 300.0, 1000.0),
    "nicd":     (100.0, 300.0, 1000.0),
    "alkaline": (250.0, 700.0, 2500.0),
    # A coin cell is in ohms, not milliohms, and is a different world: a CR2032
    # cannot deliver 1 A at all, at any age or temperature.
    "coin":     (10000.0, 30000.0, 100000.0),
}
# Used when the chemistry was not recorded.  A small LiPo is the common case for
# a target being profiled here, and it sits mid-range, so it neither cries wolf
# nor waves through a design that a coin cell could never run.
ESR_UNKNOWN = CHEMISTRY_ESR_MOHM["lipo"]

ESR_SCENARIOS = ("fresh_warm", "mid_life", "aged_cold_flat")
ESR_SCENARIO_LABEL = {
    "fresh_warm": "new cell, room temperature, well charged",
    "mid_life": "part-aged cell, room temperature, mid state of charge",
    "aged_cold_flat": "aged cell, cold, low state of charge -- the worst case, "
                      "and where field resets actually happen",
}

# Chemistry strings a developer might reasonably type, mapped to the table keys.
_CHEM_ALIASES = {
    "liion": "liion", "lion": "liion", "lithiumion": "liion", "18650": "liion",
    "21700": "liion", "licoo2": "liion", "nmc": "liion",
    "lipo": "lipo", "lipoly": "lipo", "lithiumpolymer": "lipo",
    "lifepo4": "lifepo4", "lfp": "lifepo4",
    "nimh": "nimh", "nickelmetalhydride": "nimh",
    "nicd": "nicd", "nicad": "nicd",
    "alkaline": "alkaline", "aa": "alkaline", "aaa": "alkaline",
    "coin": "coin", "coincell": "coin", "button": "coin", "buttoncell": "coin",
    "cr2032": "coin", "cr2016": "coin", "cr2450": "coin", "lir2032": "coin",
}


def esr_profile(chemistry: str = None, esr_mohm: float = None) -> dict:
    """The three internal-resistance cases to evaluate a surge against.

    An explicit measured value pins the mid case and scales the other two by the
    same ratios the table uses, so a developer who measures their own cell gets
    a cold-and-aged estimate out of it rather than having to guess three.
    """
    key = None
    if chemistry:
        flat = "".join(ch for ch in str(chemistry).lower() if ch.isalnum())
        key = _CHEM_ALIASES.get(flat)
        if key is None:
            key = next((v for k, v in _CHEM_ALIASES.items() if k in flat), None)

    table = CHEMISTRY_ESR_MOHM.get(key, ESR_UNKNOWN)
    if esr_mohm:
        # Keep the shape of the chemistry's ageing curve, anchored on the
        # measured figure: a cell that is stiff when new is stiff in the cold
        # too, relative to itself.
        scale = float(esr_mohm) / table[0]
        values = tuple(v * scale for v in table)
        source = f"measured {float(esr_mohm):g} mOhm, aged"
        if key:
            source += f" using the {key} ageing ratio"
    else:
        values = table
        source = (f"typical for {key}" if key else
                  f"chemistry '{chemistry}' not recognised -- assumed small "
                  f"LiPo" if chemistry else
                  "assumed small LiPo -- chemistry not recorded")
    return {"esr_mohm": {s: _f(v, 1) for s, v in zip(ESR_SCENARIOS, values)},
            "chemistry": key or (chemistry or None),
            "source": source,
            "measured": bool(esr_mohm)}


def sag_mv(current_ma: float, esr_mohm: float) -> float:
    """Terminal voltage a cell of this resistance loses at this current."""
    return current_ma * esr_mohm / 1000.0


def _median_window(i64: np.ndarray, lo: int, hi: int):
    """Median over a clipped slice, or None when the slice lies outside."""
    lo, hi = max(0, int(lo)), min(int(i64.size), int(hi))
    if hi <= lo:
        return None
    return float(np.median(i64[lo:hi]))


def _spike_bounds(i64: np.ndarray, peak_idx: int, level: float,
                  limit: int) -> tuple:
    """Widen from the peak to where the current last falls below `level`.

    Measuring the width at a fraction of the peak rather than at the detection
    threshold is what a scope does, and it is the honest number: the width of a
    2 A spike measured at 1 A depends on how tall the spike is, which makes a
    bigger surge look briefer.
    """
    n = i64.size
    lo, hi = max(0, peak_idx - limit), min(n, peak_idx + limit + 1)
    left = np.flatnonzero(i64[lo:peak_idx] < level)
    start = lo + int(left[-1]) + 1 if left.size else lo
    right = np.flatnonzero(i64[peak_idx + 1:hi] < level)
    end = peak_idx + 1 + int(right[0]) if right.size else hi
    return start, end


def _merge_runs(starts: np.ndarray, ends: np.ndarray, gap: int) -> list:
    """Join runs separated by less than `gap` samples.

    A real inrush rings: the current dips below the threshold between sub-peaks
    of the same event, and counting those as separate surges would report six
    events where the target has one.
    """
    merged = []
    for a, b in zip(starts.tolist(), ends.tolist()):
        if merged and a - merged[-1][1] <= gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return merged


def find_inrush(i_ma: np.ndarray, fs: int = SAMPLE_RATE,
                peak_ma: float = INRUSH_PEAK_MA,
                max_duration_ms: float = INRUSH_MAX_DURATION_MS,
                settled_ratio: float = INRUSH_SETTLED_RATIO,
                ovc_ma: float = None, max_events: int = 20,
                ovc_tripped: bool = None,
                recurring_peak_ma: float = INRUSH_RECURRING_PEAK_MA,
                power_on_capture: bool = None) -> dict:
    """Locate current spikes with the shape of an inrush, and say if they repeat.

    A candidate is an excursion above the detection threshold; each is then
    measured for its full width at 10% of its own peak, and compared against the
    current the target settles at afterwards.  Three things separate an inrush
    from ordinary activity: it is brief, it is far above what follows it, and it
    is large -- either in absolute terms, or by happening over and over.

    Repetition is the important axis.  A surge at the start of a capture that
    began with the target unpowered is the power-up one, which in a real product
    happens once when the battery is fitted.  A surge in the middle of a running
    capture is a rail being switched on, and that one recurs forever.

    The detection threshold drops to just under the over-current limit when that
    limit is below the bar, because the instrument clips there: a target with a
    2 A surge measured behind a 500 mA limit shows a 500 mA plateau, and a plain
    "above 1 A" test would call that clean.
    """
    i64 = np.asarray(i_ma, dtype=np.float64)
    n = i64.size
    if n == 0:
        return {"error": "capture contains no samples"}

    # Detection runs at the lower, recurring bar; the one-off bar is applied
    # when classifying, so a small surge is still found and then judged on
    # whether it repeats rather than being discarded for its size alone.
    recurring_ma = min(float(recurring_peak_ma), float(peak_ma))
    detect_ma = recurring_ma
    reason = (f"excursion above {detect_ma:g} mA; a single spike is only called "
              f"an inrush at {float(peak_ma):g} mA or more, but a repeating one "
              f"counts from {recurring_ma:g} mA")
    if ovc_ma and OVC_CLIP_FRACTION * float(ovc_ma) < detect_ma:
        detect_ma = OVC_CLIP_FRACTION * float(ovc_ma)
        reason = (f"excursion above {detect_ma:g} mA -- lowered because the "
                  f"over-current limit is {float(ovc_ma):g} mA, so nothing "
                  f"larger than that can be measured")

    out = {
        "capture_duration_s": _f(n / fs, 4),
        "capture_peak_ma": _f(float(i64.max())),
        "detect_threshold_ma": _f(detect_ma),
        "detect_threshold_reason": reason,
        "inrush_peak_ma": _f(float(peak_ma)),
        "recurring_peak_ma": _f(recurring_ma),
        "max_inrush_duration_ms": max_duration_ms,
        "sample_resolution_us": _f(1e6 / fs, 2),
        "ovc_ma": _f(float(ovc_ma)) if ovc_ma else None,
        # Every count and handle the caller reads is present from the start, so
        # a capture with nothing in it returns the same shape as one that is
        # full of surges. The alternative is a KeyError on the quietest, most
        # ordinary trace there is.
        "events": [],
        "inrush_events": 0,
        "recurring_events": 0,
        "power_on_events": 0,
        "sustained_events": 0,
        "clipped_events": 0,
        "normal_load_events": 0,
        "candidates": 0,
        "worst": None,
        "worst_recurring": None,
    }

    starts, ends = _intervals(i64 >= detect_ma)
    if starts.size == 0:
        return out

    gap = max(1, int(INRUSH_MERGE_GAP_MS * 1e-3 * fs))
    limit = max(1, int(INRUSH_SEARCH_MS * 1e-3 * fs))
    settle = max(1, int(INRUSH_SETTLE_WINDOW_MS * 1e-3 * fs))
    regions = _merge_runs(starts, ends, gap)
    out["candidates"] = len(regions)
    if len(regions) > INRUSH_MAX_REGIONS:
        # A trace hovering either side of the threshold, not a target switching
        # a rail forty thousand times. Measure the largest and say what was
        # skipped rather than spending a minute proving it.
        regions.sort(key=lambda r: -float(i64[r[0]:r[1]].max()))
        regions = sorted(regions[:INRUSH_MAX_REGIONS])
        out["candidates_truncated"] = (
            f"{out['candidates']} excursions crossed {detect_ma:g} mA, far more "
            f"than a switched rail produces. The {INRUSH_MAX_REGIONS} largest "
            f"were measured; the rest are counted only. Current sitting near "
            f"the threshold, rather than stepping across it, produces this.")

    events = []
    for a, b in regions:
        peak_idx = a + int(np.argmax(i64[a:b]))
        peak = float(i64[peak_idx])
        # Width is taken at 10% of this spike's own peak.  On a target whose
        # steady draw is already above that level the search would run away, so
        # it is capped at INRUSH_SEARCH_MS either side -- well past the 4 ms
        # that still counts as an inrush, so nothing real gets truncated.
        s, e = _spike_bounds(i64, peak_idx, INRUSH_EDGE_FRACTION * peak, limit)

        pre = _median_window(i64, s - settle, s)
        post = _median_window(i64, e, e + settle)
        # A spike at the very end of a capture has nothing after it to settle
        # to. Saying so is better than inventing a ratio from one sample.
        settled = post if post is not None else pre

        width_ms = (e - s) / fs * 1000.0
        above_ms = (b - a) / fs * 1000.0
        ratio = (peak / settled) if settled and settled > 0 else None
        charge_uah = float(i64[s:e].sum()) / fs / 3600.0 * 1000.0

        # A flat top at the limit is the signature of protection acting rather
        # than of the target's own demand.
        clipped = bool(ovc_ma and peak >= OVC_CLIP_FRACTION * float(ovc_ma))
        if ovc_tripped and peak >= 0.5 * float(ovc_ma or OVC_DEFAULT_MA):
            clipped = True
        flat = float((i64[a:b] >= 0.95 * peak).mean()) if b > a else 0.0
        # Power gone after the surge: the supply shut off rather than rode it.
        # Only ever claimed for a spike that also reached the limit -- a target
        # dropping to microamps after boot looks identical in the samples, and
        # calling that a shutdown would be worse than saying nothing.
        tail = i64[e:]
        collapsed = bool(clipped and tail.size > settle and
                         float(np.median(tail)) < 0.5)

        # What the spike costs above what the target was drawing anyway. For a
        # rail that is switched repeatedly this is the number that decides
        # whether power-gating it is paying for itself at all.
        excess_uah = charge_uah - (settled or 0.0) * (e - s) / fs / 3600.0 * 1000.0

        ev = {
            "index": len(events),
            "start_s": _f(s / fs, 5),
            "peak_ma": _f(peak),
            "peak_at_s": _f(peak_idx / fs, 5),
            "duration_ms": _f(width_ms, 4),
            "time_above_threshold_ms": _f(above_ms, 4),
            "before_ma": _f(pre) if pre is not None else None,
            "settled_ma": _f(settled) if settled is not None else None,
            "peak_over_settled": _f(ratio, 1) if ratio else None,
            "charge_uah": _f(charge_uah),
            "excess_uah": _f(excess_uah),
            "rise_time_us": _f((peak_idx - s) / fs * 1e6, 1),
            "samples_above_threshold": int(b - a),
        }

        if clipped:
            ev["classification"] = "CLIPPED_BY_OVC"
            ev["peak_is_lower_bound"] = True
            ev["note"] = (
                f"The current reached the P1150's over-current limit "
                f"({float(ovc_ma or OVC_DEFAULT_MA):g} mA), so "
                f"{ev['peak_ma']} mA is the "
                f"instrument's limit and not the target's demand -- the real "
                f"peak is higher, possibly much higher, and cannot be known "
                f"until the limit is raised above it.")
            if flat > 0.5:
                ev["flat_top_pct"] = _f(100.0 * flat, 1)
        elif ratio is not None and ratio < settled_ratio:
            # Not a surge at all: the target draws about this much anyway, so
            # there is no step for a battery to fail to supply. Reporting a
            # 1.5 A peak on a device that runs at 1.2 A as an inrush would be
            # noise, and noise is what stops the real ones being believed.
            ev["classification"] = "NORMAL_LOAD"
            ev["note"] = (
                f"Only {ev['peak_over_settled']}x what the target draws either "
                f"side of it ({ev['settled_ma']} mA), so this is its normal "
                f"working current rather than a start-up surge. A battery sees "
                f"it as a steady load, not a step.")
        elif width_ms > max_duration_ms:
            ev["classification"] = "SUSTAINED"
            ev["note"] = (
                f"Held above the threshold for {ev['duration_ms']} ms, longer "
                f"than the {max_duration_ms} ms that defines an inrush. This is "
                f"a real load being driven, not capacitance charging -- and a "
                f"battery has to supply it for the whole {ev['duration_ms']} ms, "
                f"not just for a moment.")
        elif peak >= peak_ma:
            ev["classification"] = "INRUSH"
        elif ratio is None or ratio >= INRUSH_RECURRING_RATIO:
            # Under the one-off bar, but sharp and far above the floor. Only
            # meaningful if it turns out to repeat, which is decided once every
            # region has been measured.
            ev["classification"] = "SURGE"
        else:
            ev["classification"] = "MINOR_SURGE"
            ev["note"] = (
                f"{ev['peak_ma']} mA is below the {peak_ma:g} mA at which a "
                f"single spike is called an inrush, and it is only "
                f"{ev['peak_over_settled']}x the settled current. Noted in case "
                f"it turns out to repeat, but on its own it is not a finding.")
        if collapsed:
            ev["supply_collapsed_after"] = True
            ev["collapse_note"] = (
                "Current fell to essentially zero after the surge and stayed "
                "there: the P1150 shut its output off. The target is unpowered "
                "from this point in the capture onward, so nothing after it is "
                "the target's behaviour.")
        events.append(ev)

    # ---- which of these is the power-up one? ------------------------ #
    # It matters more than anything else here. In a shipped product the battery
    # goes in once and stays in, so a surge that only happens at power-up is
    # mostly a curiosity; one that happens while the target is running is a rail
    # being switched, and that repeats for the life of the device.
    surges = [e for e in events
              if e["classification"] in ("INRUSH", "CLIPPED_BY_OVC", "SURGE")]
    if surges:
        first = surges[0]
        if power_on_capture:
            first["at_power_on"] = True
        elif power_on_capture is None and \
                first["start_s"] <= INRUSH_POWER_ON_WINDOW_MS * 1e-3 and \
                (first["before_ma"] or 0.0) < INRUSH_POWER_ON_FLOOR_MA:
            first["at_power_on"] = True
            first["at_power_on_inferred"] = (
                "Taken for the power-up surge because it sits at the very start "
                "of the capture with no current before it. If the target was "
                "already running and this is a rail being switched on, it is "
                "the more serious kind -- re-run the analysis on a capture that "
                "starts while the target is up.")

    recurring = [e for e in surges if not e.get("at_power_on")]
    out["inrush_events"] = sum(1 for e in events
                               if e["classification"] in ("INRUSH",
                                                          "CLIPPED_BY_OVC"))
    out["recurring_events"] = len(recurring)
    out["power_on_events"] = sum(1 for e in surges if e.get("at_power_on"))
    out["sustained_events"] = sum(1 for e in events
                                  if e["classification"] == "SUSTAINED")
    out["clipped_events"] = sum(1 for e in events
                                if e["classification"] == "CLIPPED_BY_OVC")
    out["normal_load_events"] = sum(1 for e in events
                                    if e["classification"] == "NORMAL_LOAD")

    # ---- how often, and what the repetition costs ------------------- #
    if len(recurring) >= 2:
        t = np.array([e["peak_at_s"] for e in recurring], dtype=np.float64)
        periods = np.diff(t)
        mp = float(periods.mean())
        jitter = (100.0 * float(periods.std()) / mp) if mp > 0 else None
        rate = (1.0 / mp) if mp > 0 else None
        rep = {
            "occurrences": len(recurring),
            "mean_period_s": _f(mp, 5),
            "rate_hz": _f(rate, 3) if rate else None,
            "period_jitter_pct": _f(jitter, 1) if jitter is not None else None,
            "mean_peak_ma": _f(float(np.mean([e["peak_ma"] for e in recurring]))),
            "max_peak_ma": _f(float(max(e["peak_ma"] for e in recurring))),
            "mean_duration_ms": _f(
                float(np.mean([e["duration_ms"] for e in recurring])), 4),
        }
        # A regular period points at a timer; an irregular one at something
        # event-driven. Both recur, but they are found in different code.
        if jitter is not None:
            rep["regular"] = bool(jitter < 20.0)
            rep["timing_note"] = (
                "Evenly spaced, so something periodic drives it -- a timer, a "
                "duty cycle, a poll." if jitter < 20.0 else
                "Irregularly spaced, so it is event-driven rather than on a "
                "timer -- triggered by traffic, by a sensor, or by user action.")

        # What the repetition actually costs, in the units the developer cares
        # about. Charging the same capacitance over and over is not free, and
        # this is the number that says whether power-gating that rail is paying
        # for itself at all -- which is not a question anyone thinks to ask.
        if rate:
            mean_excess = float(np.mean([e["excess_uah"] for e in recurring]))
            rep["mean_excess_uah_per_event"] = _f(mean_excess)
            rep["equivalent_average_ma"] = _f(mean_excess * rate * 3600.0 / 1000.0)
        out["repetition"] = rep

    # The worst one is what the design has to survive, so it leads -- and
    # "worst" means most concerning, not merely largest. A 1.5 A spike on a
    # device that draws 1.2 A anyway must not outrank a 1.2 A surge on one that
    # idles at 3 mA, which is the one that will strand a customer. A surge while
    # the target is running outranks the same surge at power-up, because that
    # one happens once in the product's life and this one happens always.
    rank = {"CLIPPED_BY_OVC": 0, "INRUSH": 1, "SURGE": 2, "SUSTAINED": 3,
            "MINOR_SURGE": 4, "NORMAL_LOAD": 5}
    events.sort(key=lambda e: (bool(e.get("at_power_on")),
                               rank.get(e["classification"], 9),
                               -e["peak_ma"]))
    out["worst"] = events[0] if events else None
    out["worst_recurring"] = max(recurring, key=lambda e: e["peak_ma"]) \
        if recurring else None
    if len(events) > max_events:
        out["events_note"] = (
            f"{len(events)} spikes measured; the {max_events} most significant "
            f"are listed. The counts and the repetition statistics above cover "
            f"all of them.")
    out["events"] = events[:max_events]
    return out


def inrush_analysis(i_ma: np.ndarray, fs: int = SAMPLE_RATE,
                    ovc_ma: float = None, supply_mv: float = None,
                    chemistry: str = None, esr_mohm: float = None,
                    brownout_mv: float = None,
                    peak_ma: float = INRUSH_PEAK_MA,
                    max_duration_ms: float = INRUSH_MAX_DURATION_MS,
                    settled_ratio: float = INRUSH_SETTLED_RATIO,
                    ovc_tripped: bool = None,
                    recurring_peak_ma: float = INRUSH_RECURRING_PEAK_MA,
                    power_on_capture: bool = None) -> dict:
    """Find inrush spikes and say what a real battery would do about them.

    The current is only half the answer.  What the developer needs to know is
    whether the cell they ship with can supply that surge without sagging below
    the point where the target resets -- and that depends on the cell's internal
    resistance, which is at its worst exactly when the product is in the field:
    aged, cold, and near flat.

    Recurrence decides how much any of it matters.  A surge at power-up happens
    once, when the battery is fitted; a surge while the target is running is a
    switched rail and happens for the life of the product.
    """
    found = find_inrush(i_ma, fs, peak_ma, max_duration_ms, settled_ratio,
                        ovc_ma, ovc_tripped=ovc_tripped,
                        recurring_peak_ma=recurring_peak_ma,
                        power_on_capture=power_on_capture)
    if "error" in found:
        return found

    out = dict(found)
    events = found.get("events") or []
    clipped = bool(found["clipped_events"])
    n_rec = found["recurring_events"]
    rec_worst = found.get("worst_recurring")
    rep = found.get("repetition") or {}

    # A recurring surge is only reported from the lower bar once there are
    # enough of them to be a pattern rather than a coincidence. Above the
    # one-off bar a single one is enough on its own.
    big_recurring = bool(rec_worst and rec_worst["peak_ma"] >= peak_ma)
    many_recurring = n_rec >= INRUSH_RECURRING_MIN_EVENTS
    repeats = n_rec >= 2 and (big_recurring or many_recurring)

    # ---- verdict ---------------------------------------------------- #
    # Ordered by what it costs the product, not by how large the number is.
    if clipped:
        verdict = "OVC_TRIP"
    elif repeats:
        verdict = "RECURRING_INRUSH" if big_recurring else "RECURRING_SURGE"
    elif n_rec and big_recurring:
        verdict = "INRUSH"
    elif found["power_on_events"] and any(
            e.get("at_power_on") and e["peak_ma"] >= peak_ma for e in events):
        verdict = "POWER_ON_INRUSH"
    elif any(e["classification"] == "SUSTAINED" and e["peak_ma"] >= peak_ma
             for e in events):
        # Only above the one-off bar. A 300 mA burst lasting a few milliseconds
        # is a radio waking up or a sensor converting -- the most ordinary thing
        # a battery-powered target does, and reporting it would bury the
        # findings that matter under one per capture.
        verdict = "SUSTAINED_HIGH_CURRENT"
    else:
        verdict = "PASS"
    out["verdict"] = verdict

    # The event the rest of the report is built around: the recurring one where
    # there is one, because that is the one the product lives with.
    worst = rec_worst if (repeats or (n_rec and big_recurring)) \
        else found.get("worst")

    severity = {
        "OVC_TRIP": "CRITICAL",
        "RECURRING_INRUSH": "HIGH",
        "RECURRING_SURGE": "MEDIUM",
        "INRUSH": "MEDIUM",
        "POWER_ON_INRUSH": "LOW",
        "SUSTAINED_HIGH_CURRENT": "MEDIUM",
        "PASS": "NONE",
    }[verdict]

    if not events or worst is None:
        out["severity"] = "NONE"
        out["assessment"] = (
            f"No current spike reached {found['detect_threshold_ma']} mA. The "
            f"highest current anywhere in this capture was "
            f"{found['capture_peak_ma']} mA, which no battery of a sane size "
            f"has trouble supplying.")
        return out
    if verdict == "PASS":
        out["severity"] = severity
        if worst["classification"] == "NORMAL_LOAD":
            out["assessment"] = (
                f"Current reached {worst['peak_ma']} mA, but the target draws "
                f"{worst['settled_ma']} mA either side of it -- only "
                f"{worst['peak_over_settled']}x. That is its working current, "
                f"not a switching surge, so there is no step for a battery to "
                f"fail to supply. Whether a cell can sustain "
                f"{worst['settled_ma']} mA continuously is a separate question, "
                f"and a capacity one.")
        elif worst["classification"] == "SUSTAINED":
            out["assessment"] = (
                f"Current reached {worst['peak_ma']} mA and held there for "
                f"{worst['duration_ms']} ms. That is a load being driven -- a "
                f"radio waking, a sensor converting, a peripheral working -- "
                f"not capacitance being charged, and it is below the "
                f"{peak_ma:g} mA at which a burst is worth flagging on its own. "
                f"Use p1150_events to characterise it as a duty cycle, which is "
                f"the right lens for a burst like this.")
        else:
            out["assessment"] = (
                f"A {worst['peak_ma']} mA spike {worst['duration_ms']} ms wide "
                f"was measured, below the {peak_ma:g} mA at which a single one "
                f"is called an inrush, and it did not repeat during this "
                f"{found['capture_duration_s']} s capture. Nothing to act on. "
                f"If the target power-gates a rail that was not exercised here, "
                f"capture again while it is, since a switched rail is the one "
                f"that matters.")
        return out
    out["severity"] = severity

    # ---- one-off at power-up, or a rail being switched? ------------- #
    if verdict == "POWER_ON_INRUSH":
        out["recurrence"] = "ONCE_AT_POWER_UP"
        inferred = next((e.get("at_power_on_inferred") for e in events
                         if e.get("at_power_on_inferred")), None)
        if inferred:
            # The whole "this only happens once" argument rests on it being the
            # power-up surge, so where that was guessed rather than recorded,
            # the guess has to travel with the conclusion.
            out["recurrence"] = "ONCE_AT_POWER_UP_INFERRED"
            out["recurrence_caveat"] = inferred
        out["recurrence_note"] = (
            f"This surge is at the instant power was applied, and nothing like "
            f"it happened again during the capture. In a product whose battery "
            f"is fitted once and left in, that means it happens once in the "
            f"device's life -- so it is much less serious than it looks, and "
            f"the usual answer is to note it and move on. It still matters if "
            f"the battery is user-replaceable, if the pack's protection FET can "
            f"re-connect under load, or if a charger can hot-plug the rail: "
            f"each of those repeats the event. Check too that it does not trip "
            f"protection, since a surge that trips the P1150's limit will trip "
            f"a pack protection IC as readily.")
    elif repeats or (n_rec and big_recurring):
        out["recurrence"] = "WHILE_RUNNING"
        every = (f"every {rep['mean_period_s']} s ({rep['rate_hz']} Hz)"
                 if rep.get("rate_hz") else "more than once")
        out["recurrence_note"] = (
            f"This surge happens while the target is already running, "
            f"{f'{n_rec} times in this capture, {every}' if n_rec > 1 else 'once in this capture'}"
            f" -- so it is NOT the power-up inrush. A brief spike into a load "
            f"that was drawing nothing a moment earlier is what enabling an LDO "
            f"or an SMPS looks like: at the instant of enable, the decoupling "
            f"capacitance downstream is effectively a short circuit, and the "
            f"current is limited only by the resistance in the path. Unlike a "
            f"power-up surge, this one repeats for the life of the product, at "
            f"every temperature and state of charge."
            + ("" if n_rec > 1 else
               " It occurred only once here, but if it is a rail being "
               "switched it happens on every duty cycle -- capture for longer, "
               "or while exercising the feature that turns it on, to see the "
               "rate."))

    # ---- what a battery would do ------------------------------------ #
    prof = esr_profile(chemistry, esr_mohm)
    out["battery_model"] = prof
    peak = worst["peak_ma"]
    out["battery_sag_basis"] = {
        "peak_ma": peak,
        "at_s": worst["peak_at_s"],
        "which": ("the recurring surge" if worst is rec_worst else
                  "the power-up surge" if worst.get("at_power_on") else
                  "the largest event"),
    }
    # When the sag is modelled on a recurring surge, a bigger one-off peak
    # elsewhere in the capture would otherwise look like it had been missed.
    biggest = found.get("worst")
    if biggest is not None and biggest is not worst and \
            biggest["peak_ma"] > peak:
        out["battery_sag_basis"]["note"] = (
            f"A larger {biggest['peak_ma']} mA peak was measured at "
            f"{biggest['peak_at_s']} s, but the sag below is modelled on the "
            f"recurring surge instead: the larger one happens at power-up, once "
            f"in the product's life, and the recurring one happens forever.")
    rows = []
    for scenario in ESR_SCENARIOS:
        r = prof["esr_mohm"][scenario]
        drop = sag_mv(peak, r)
        row = {"scenario": scenario,
               "condition": ESR_SCENARIO_LABEL[scenario],
               "esr_mohm": r,
               "sag_mv": _f(drop, 1)}
        if supply_mv:
            row["terminal_mv_at_peak"] = _f(max(0.0, supply_mv - drop), 1)
            row["sag_pct"] = _f(100.0 * drop / supply_mv, 1)
            if drop >= supply_mv:
                # The model has run past what the cell can physically do. The
                # honest statement is not "it sags by 9 V" but "it cannot
                # deliver this current at all" -- into a dead short it manages
                # only V/R, and the target simply collapses instead.
                row["cell_cannot_supply"] = True
                row["max_deliverable_ma"] = _f(1000.0 * supply_mv / r, 1)
        if supply_mv and brownout_mv:
            row["browns_out"] = bool(supply_mv - drop < brownout_mv)
        rows.append(row)
    out["battery_sag"] = rows
    impossible = [r["scenario"] for r in rows if r.get("cell_cannot_supply")]
    if impossible:
        out["cell_cannot_supply"] = impossible
        worst_case = rows[-1]
        out["cell_cannot_supply_note"] = (
            f"For {', '.join(impossible)}, the modelled sag exceeds the supply "
            f"voltage entirely -- meaning the cell cannot deliver {peak:g} mA "
            f"at all. It is not that the target dips; it is that the demand is "
            f"impossible and the rail collapses. Into a dead short, a cell of "
            f"{worst_case['esr_mohm']:g} mOhm at {supply_mv:g} mV manages about "
            f"{worst_case.get('max_deliverable_ma')} mA. The surge has to be "
            f"supplied by a capacitor local to the load, or removed.")
    if clipped:
        # Everything below is computed from a peak the instrument clipped, so
        # every figure is a floor. Presenting it without saying so would let a
        # tripped supply read as a comfortable pass, which is the worst
        # possible failure mode for this tool.
        out["battery_sag_is_lower_bound"] = True
        out["battery_sag_warning"] = (
            f"These figures are computed from {peak:g} mA, which is where the "
            f"over-current limit clipped the measurement -- not from the "
            f"target's actual demand. The real sag is LARGER, by an unknown "
            f"factor. Nothing here can be read as a pass; re-measure with the "
            f"limit raised above the surge before drawing any conclusion.")
    out["battery_sag_note"] = (
        "First-order estimate: sag = peak current x cell internal resistance. "
        "It ignores the cell's transient behaviour, which over a few "
        "milliseconds is somewhat stiffer than its DC resistance suggests, and "
        "it ignores the resistance of the protection FET, connector, and PCB "
        "traces between the cell and the load -- which add to it. Treat it as "
        "the right order of magnitude, and measure the cell's ESR to do better."
        + ("" if supply_mv else
           " Supply voltage was not recorded for this run, so only the size of "
           "the drop is shown, not what it leaves at the terminals."))

    if prof["chemistry"] == "coin" and peak >= 100:
        out["coin_cell_warning"] = (
            f"This is a coin cell, whose internal resistance is measured in "
            f"ohms rather than milliohms. A {peak:g} mA surge is not something "
            f"it can supply at all -- its terminal voltage collapses instead, "
            f"and the target resets or never boots. A coin-cell design needs a "
            f"bulk capacitor sized to supply the surge locally, so the cell "
            f"only ever sees the average.")

    if supply_mv and brownout_mv:
        risky = [r["scenario"] for r in rows if r.get("browns_out")]
        if risky:
            out["brownout_risk"] = risky
            out["brownout_verdict"] = (
                f"At {peak:g} mA the terminal voltage falls below the "
                f"{brownout_mv:g} mV the target needs, for: "
                f"{', '.join(risky)}. That is a reset in the field, and it will "
                f"not reproduce on the bench with a fresh cell."
                + (" And the real surge is larger than the clipped figure this "
                   "is based on." if clipped else ""))
        elif clipped:
            out["brownout_verdict"] = (
                f"The clipped peak of {peak:g} mA alone would not take the "
                f"terminal voltage below {brownout_mv:g} mV -- but the true "
                f"peak is higher and unknown, so this says nothing about "
                f"whether the target browns out. Re-measure with the "
                f"over-current limit raised.")
        else:
            out["brownout_verdict"] = (
                f"Even at the worst case modelled the terminal voltage stays "
                f"above the {brownout_mv:g} mV the target needs.")
    elif not brownout_mv:
        out["brownout_unknown"] = (
            "The target's minimum operating voltage is not recorded, so this "
            "cannot say whether the sag causes a reset. Ask the developer for "
            "the brown-out threshold -- the regulator's dropout, or the MCU's "
            "BOR level, whichever is higher -- and record it with "
            "p1150_set_battery(brownout_mv=...).")

    # ---- what it means, and what to do ------------------------------ #
    notes = []
    if verdict == "OVC_TRIP":
        notes.append(
            f"The surge hit the P1150's over-current limit of "
            f"{float(ovc_ma or OVC_DEFAULT_MA):g} mA and the supply shut off, "
            f"so the target lost power. The true peak is unknown and is at "
            f"least that. Re-run with the limit raised to measure it -- and "
            f"note that the limit tripping is itself the finding: a surge this "
            f"large is well beyond what a small cell can deliver.")
    elif verdict in ("INRUSH", "RECURRING_INRUSH", "RECURRING_SURGE",
                     "POWER_ON_INRUSH"):
        notes.append(
            f"A {worst['peak_ma']} mA surge lasting {worst['duration_ms']} ms, "
            f"against {worst['settled_ma']} mA either side of it -- "
            f"{worst['peak_over_settled']}x. The energy in it is negligible "
            f"({worst['charge_uah']} uAh, so battery life is not the issue); "
            f"the problem is entirely that a cell has to deliver that current "
            f"instantly, and a tired one cannot.")
    elif verdict == "SUSTAINED_HIGH_CURRENT":
        notes.append(
            f"The current stays high for {worst['duration_ms']} ms rather than "
            f"spiking briefly, so this is a load being driven and not "
            f"capacitance charging. Inrush limiting will not help; the fix is "
            f"either to reduce the load or to confirm the cell is specified for "
            f"that continuous draw.")

    if ovc_ma and worst["peak_ma"] > 0.7 * float(ovc_ma) and \
            verdict != "OVC_TRIP":
        notes.append(
            f"The peak reaches "
            f"{_f(100.0 * worst['peak_ma'] / float(ovc_ma), 0)}% of the "
            f"over-current limit currently set ({float(ovc_ma):g} mA). "
            f"Run-to-run variation will trip it, which looks like a firmware "
            f"fault and is not one. A pack protection IC will treat it the same "
            f"way.")

    if rep.get("timing_note"):
        jit = rep.get("period_jitter_pct")
        jitter_txt = f" with {jit}% jitter" if jit is not None else ""
        notes.append(
            f"The surges repeat {rep['rate_hz']} times a second{jitter_txt}. "
            f"{rep['timing_note']} Find the code that enables that rail: the "
            f"surge is at the enable, so whatever runs on that period is what "
            f"switches it.")
    elif len(events) > 1 and verdict != "PASS":
        notes.append(
            f"{len(events)} separate spikes were found, not one. Rails coming "
            f"up in sequence, or a peripheral being enabled repeatedly -- "
            f"worth knowing, because staggering them further is often the whole "
            f"fix.")

    # Whether power-gating the rail is even paying for itself. Recharging the
    # same capacitance every cycle is a real cost, and nobody thinks to weigh
    # it against what the gating saves.
    if rep.get("equivalent_average_ma"):
        notes.append(
            f"Recharging that capacitance {rep['rate_hz']} times a second "
            f"costs {rep['equivalent_average_ma']} mA on average "
            f"({rep['mean_excess_uah_per_event']} uAh per switch-on), which is "
            f"spent whether or not the rail does any work. If the rail is being "
            f"power-gated to save current, compare that against what it draws "
            f"when simply left on -- gating a rail more often than its "
            f"capacitance can pay back is a net loss, and a common one.")
    out["findings"] = notes

    if verdict != "PASS":
        recurring_first = [
            "Identify which rail is being switched. The surge is at the "
            "instant an LDO or SMPS is enabled, when the decoupling "
            "capacitance downstream of it is still discharged and therefore "
            "looks like a short circuit. Match the timing above against the "
            "code that turns a regulator on.",
            "Specify a regulator with SOFT-START. This is the proper fix: the "
            "part ramps its output over a controlled time instead of stepping "
            "it, so the capacitance charges gradually and the surge does not "
            "exist. Many LDO and SMPS families offer a soft-start pin, or an "
            "adjustable ramp set by a capacitor, at the same price as the part "
            "without one -- but the choice is made at schematic time, which is "
            "why finding this before the boards are built is worth so much.",
            "If the regulator is already fitted and has no soft-start, put a "
            "slew-rate-limited load switch in front of the rail (a soft-start "
            "pin, or an RC on the gate of a series FET), which achieves the "
            "same ramp externally.",
            "Reduce the decoupling on the switched rail to what that "
            "sub-circuit actually needs. Bulk capacitance carried over from a "
            "reference design is the usual reason a switched rail surges as "
            "hard as it does.",
            "Reconsider whether the rail needs power-gating at all, or needs "
            "gating that often. Each enable costs the surge plus the charge to "
            "refill the capacitance; a rail cycled rapidly can cost more than "
            "leaving it powered.",
        ]
        power_on_first = [
            "Find what is being energised at that instant: bulk capacitance "
            "charging through a near-zero source impedance is the usual "
            "answer, and the surge is limited only by ESR and trace "
            "resistance.",
            "Stagger the rails and the peripherals in firmware. Enabling "
            "loads one at a time, milliseconds apart, costs nothing and often "
            "removes the problem outright.",
            "Use a load switch with a controlled slew rate (a soft-start pin, "
            "or a gate RC) on the branch that draws the surge, so the "
            "capacitance charges over milliseconds instead of microseconds.",
            "Check the DC-DC converter's soft-start. A missing or wrongly "
            "sized soft-start capacitor turns every start-up into a "
            "full-current event.",
            "Reduce bulk capacitance to what the design actually needs, or "
            "add series resistance -- an NTC inrush limiter, or a resistor "
            "bypassed by a FET once the rail is up.",
            "Where the surge is unavoidable (a motor, a transmit burst), put "
            "a capacitor local to the load big enough to supply it, so the "
            "cell only ever sees the average.",
        ]
        out["remedies"] = recurring_first \
            if out.get("recurrence") == "WHILE_RUNNING" else power_on_first
        out["verify"] = (
            "After changing anything, capture the same workload again and "
            "compare the peak, and for a switched rail the rate as well. The "
            "peak is the number to drive down; duration and charge barely "
            "matter here. p1150_inrush_test re-measures the power-up surge "
            "specifically; a switched rail needs an ordinary capture taken "
            "while the firmware exercises it.")
    return out


def inrush_screen(i_ma: np.ndarray, fs: int = SAMPLE_RATE,
                  ovc_ma: float = None,
                  peak_ma: float = INRUSH_PEAK_MA,
                  recurring_peak_ma: float = INRUSH_RECURRING_PEAK_MA,
                  power_on_capture: bool = None) -> dict:
    """One-line inrush check, cheap enough to run on every capture.

    Returns an empty dict when there is nothing to say.  The point is that a
    developer measuring battery life has no reason to ask about inrush and no
    way to see it, so a capture that contains one should say so unprompted
    rather than waiting to be asked -- and a switched rail turns up in an
    ordinary battery-life capture, not in a test anyone would think to run.
    """
    i64 = np.asarray(i_ma, dtype=np.float64)
    if i64.size == 0:
        return {}
    detect = min(float(peak_ma), float(recurring_peak_ma))
    if ovc_ma and OVC_CLIP_FRACTION * float(ovc_ma) < detect:
        detect = OVC_CLIP_FRACTION * float(ovc_ma)
    # Fast reject: one pass over the array, and most captures stop here.
    if float(i64.max()) < detect:
        return {}

    r = find_inrush(i64, fs, peak_ma, ovc_ma=ovc_ma, max_events=1,
                    recurring_peak_ma=recurring_peak_ma,
                    power_on_capture=power_on_capture)
    worst = r.get("worst")
    rec = r.get("worst_recurring")
    rep = r.get("repetition") or {}
    n_rec = r["recurring_events"]
    repeats = n_rec >= 2 and (
        (rec and rec["peak_ma"] >= peak_ma) or
        n_rec >= INRUSH_RECURRING_MIN_EVENTS)
    if not worst or not (r["inrush_events"] or r["sustained_events"] or repeats):
        return {}

    if r["clipped_events"]:
        msg = (f"A current spike reached the over-current limit "
               f"({float(ovc_ma or OVC_DEFAULT_MA):g} mA) "
               f"{worst['duration_ms']} ms wide, so the supply cut out and the "
               f"true peak is higher than measured.")
        peak_reported, n = worst["peak_ma"], r["inrush_events"]
    elif repeats:
        # The serious case, and the one that shows up in a capture taken for
        # something else entirely.
        rate = f"{rep['rate_hz']} times a second" if rep.get("rate_hz") \
            else f"{n_rec} times"
        msg = (f"A {rec['peak_ma']} mA surge lasting {rec['duration_ms']} ms "
               f"repeats {rate} while the target is running, against "
               f"{rec['settled_ma']} mA either side of it. That is what "
               f"enabling an LDO or SMPS rail looks like -- its decoupling "
               f"capacitance is a short circuit at the instant of enable. "
               f"Unlike a power-up surge this one happens for the life of the "
               f"product, and a real battery may sag far enough at that current "
               f"to reset the target when the cell is aged, cold or near flat. "
               f"The fix is a regulator with soft-start, and it is a schematic "
               f"decision -- much cheaper to find now than after a respin.")
        peak_reported, n = rec["peak_ma"], n_rec
    elif r["inrush_events"]:
        at_power_on = bool(worst.get("at_power_on"))
        msg = (f"An inrush spike of {worst['peak_ma']} mA lasting "
               f"{worst['duration_ms']} ms was recorded, against "
               f"{worst['settled_ma']} mA settled. A real battery may sag "
               f"badly enough at that current to reset the target -- worst "
               f"when the cell is aged, cold or near flat."
               + (" It is at the instant power was applied, which in a product "
                  "whose battery stays fitted happens once, so it is the less "
                  "serious kind."
                  if at_power_on else
                  " It happens while the target is already running, so it is a "
                  "rail being switched rather than the power-up surge -- "
                  "capture for longer to see whether it repeats."))
        peak_reported, n = worst["peak_ma"], r["inrush_events"]
    else:
        msg = (f"Current reached {worst['peak_ma']} mA and stayed above "
               f"{r['detect_threshold_ma']} mA for {worst['duration_ms']} ms.")
        peak_reported, n = worst["peak_ma"], r["sustained_events"]

    out = {"inrush_warning": msg, "inrush_peak_ma": peak_reported,
           "inrush_events": n}
    if repeats and rep.get("rate_hz"):
        out["inrush_rate_hz"] = rep["rate_hz"]
        out["inrush_recurring"] = True
    return out


# ------------------------------------------------------------------ #
# Auxiliary marker channels                                            #
# ------------------------------------------------------------------ #
# A0/D0/D1 carry a signal the target itself drives -- typically a GPIO raised
# for the duration of some piece of work.  That turns "where did the energy go"
# from an inference into a fact: the boundaries of the region come from the
# firmware rather than from a threshold guessed off the current trace, so a
# feature whose current draw overlaps the idle floor is still measurable, and
# two runs are compared over provably the same code path.
#
# What the instrument accepts, and what the driver reports back:
#
#   A0      Analog. Accepts 0-17 V and reports millivolts as measured, so the
#           threshold that separates low from high is a property of the target's
#           IO voltage and has to come from the developer.
#
#   D0/D1   Logic, accepting a 1.2-3.3 V signal. NOT reported as 0/1: the
#           driver rescales them to fixed millivolt levels -- under ~100 mV for
#           a low, over ~900 mV for a high -- so the digital traces can be
#           plotted without overlapping the analog one. The reported level
#           therefore says nothing about the target's IO voltage, which is
#           exactly why a digital marker needs no threshold from the developer.

# Volts the input itself tolerates.  Exceeding these damages the instrument, so
# they are the limit quoted to the developer, not a measurement range.
AUX_INPUT_MAX_MV = {"A0": 17000.0, "D0": 3300.0, "D1": 3300.0}

# Lowest logic swing D0/D1 will register.
DIGITAL_INPUT_MIN_MV = 1200.0

# The bounds the driver's rescaled digital levels are guaranteed to fall
# outside of.  The two channels do not sit on identical rails -- measured with a
# function generator, D0 reports 20/1020 mV and D1 reports 40/1140 mV, offset
# from each other so the traces do not overlap when plotted -- but both lows are
# under 100 and both highs over 900, so one threshold half way between decides
# either channel.  Bounds rather than measured rails on purpose: a threshold
# pinned to one unit's exact levels would be brittle across hardware.
DIGITAL_LOW_MV = 100.0
DIGITAL_HIGH_MV = 900.0


def digital_threshold() -> tuple:
    """(threshold, hysteresis) in reported millivolts, for D0 or D1.

    Taken from the levels the driver actually produces rather than inferred
    from the samples: a channel that never toggles has no span to infer from,
    and that is precisely the case -- a marker that never fired -- where the
    answer has to still be right.
    """
    return (0.5 * (DIGITAL_LOW_MV + DIGITAL_HIGH_MV),
            0.5 * (DIGITAL_HIGH_MV - DIGITAL_LOW_MV))


def to_logic(x: np.ndarray, cfg: dict = None) -> np.ndarray:
    """Raw aux samples -> boolean "marker asserted" per sample.

    A Schmitt trigger rather than a bare comparison: a real edge arriving over a
    probe lead has finite slew and some ringing, and a bare threshold turns one
    crossing into a burst of assertions.  Implemented without a Python loop --
    at 125 kSps a one-minute capture is 7.5 million samples.
    """
    cfg = cfg or {}
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    if n == 0:
        return np.zeros(0, dtype=bool)

    thr = cfg.get("threshold_mv")
    hys = cfg.get("hysteresis_mv")
    if thr is None:
        # A digital channel: its levels are fixed and known, so they are used
        # rather than inferred. A0 always carries a threshold of its own, since
        # set_aux requires one, so it never reaches this branch.
        d_thr, d_hys = digital_threshold()
        thr = d_thr
        if hys is None:
            hys = d_hys
    hys = abs(float(hys or 0.0))

    above = x >= (thr + hys)
    below = x <= (thr - hys)
    decisive = above | below
    # Hold the last decisive state across the band: forward-fill the index of
    # the most recent sample that was unambiguously high or low.  Before the
    # first such sample the state reads low, which is the safe default -- an
    # assertion is only ever reported once the signal has actually gone high.
    idx = np.where(decisive, np.arange(n), 0)
    np.maximum.accumulate(idx, out=idx)
    high = above[idx]

    return high if cfg.get("active_high", True) else ~high


# ------------------------------------------------------------------ #
# State signal                                                         #
# ------------------------------------------------------------------ #
# The target driving a code on the digital inputs to say which state it is in,
# decoded here into per-state current.  D0 is bit 0 and D1 is bit 1, so two
# pins carry four states.
#
# The samples immediately after a state is entered are the transition into it,
# not the state: a radio shutting down, a regulator changing mode, a sensor
# powering off.  Those belong to the transition, which the usage model counts
# separately as an event, so counting them in the state's average would charge
# for them twice -- and on a target that switches often, the entry transient can
# be most of what a "sleep" average contains.  Both figures are reported: the
# mean over everything, and the mean with a settling window after each entry
# discarded.
STATE_SETTLE_MS = 20.0
STATE_ENTRY_SIGNIFICANT_PCT = 20.0
# A visit too short to survive the settling window contributes nothing to the
# settled figure, which is correct but silent, so it is counted and reported.
STATE_MIN_VISIT_MS = 1.0


def decode_state_codes(aux: dict, channels: list) -> np.ndarray:
    """Per-sample integer state code from the bit channels, LSB first."""
    code = None
    for bit, ch in enumerate(channels):
        arr = aux.get(ch)
        if arr is None:
            raise ValueError(
                f"This run has no '{ch}' channel, so the state signal cannot "
                f"be decoded. The channel has to be declared before the "
                f"capture, not after -- p1150_set_state_signal does that, and "
                f"the run must be captured after it.")
        # active_high is not configurable here: the code is a number, and
        # inverting a bit would silently renumber every state.
        bits = to_logic(arr, {"active_high": True}).astype(np.uint8)
        code = bits << bit if code is None else code | (bits << bit)
    if code is None:
        raise ValueError("No state-signal channels are declared.")
    return code


def state_breakdown(i_ma: np.ndarray, codes: np.ndarray, names: dict,
                    fs: int = SAMPLE_RATE, battery_mah: float = None,
                    settle_ms: float = STATE_SETTLE_MS) -> dict:
    """Split a capture into the states the target said it was in.

    names maps code -> state name.  Codes present in the capture but not named
    are reported rather than dropped: an undeclared code means the firmware has
    a state the model does not, which is worth knowing before an estimate is
    built on the states it does have.
    """
    i64 = i_ma.astype(np.float64, copy=False)
    n = min(i64.size, codes.size)
    i64, codes = i64[:n], codes[:n]
    if n == 0:
        return {"error": "capture contains no samples"}

    settle = max(0, int(settle_ms * 1e-3 * fs))
    min_visit = max(1, int(STATE_MIN_VISIT_MS * 1e-3 * fs))
    rows, unmapped = [], []
    for code in sorted(int(c) for c in np.unique(codes)):
        mask = codes == code
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        name = names.get(code)
        sel = i64[mask]
        starts, ends = _intervals(mask)
        # Trim a settling window off the front of every visit.  Visits too
        # short to survive it are excluded from the settled figure entirely,
        # which is why they are counted separately.
        settled = None
        skipped = 0
        if settle and starts.size:
            keep = np.zeros(n, dtype=bool)
            for a, b in zip(starts, ends):
                if b - a > settle:
                    keep[a + settle:b] = True
                else:
                    skipped += 1
            if keep.any():
                settled = float(i64[keep].mean())
        mean = float(sel.mean())
        row = {
            "code": code,
            "state": name,
            "time_s": _f(cnt / fs, 4),
            "time_pct": _f(100.0 * cnt / n, 3),
            "mean_ma": _f(mean),
            "settled_mean_ma": _f(settled) if settled is not None else None,
            "peak_ma": _f(float(sel.max())),
            "floor_ma": _f(float(np.percentile(sel, 5))),
            "charge_mah": _f(float(sel.sum() / fs / 3600.0)),
            "visits": int(starts.size),
        }
        if starts.size:
            visits_ms = (ends - starts) / fs * 1000.0
            row["mean_visit_ms"] = _f(float(visits_ms.mean()), 3)
            row["longest_visit_ms"] = _f(float(visits_ms.max()), 3)
        if skipped:
            row["visits_too_short_to_settle"] = skipped
        if settled is not None and settled > 0:
            entry = 100.0 * (mean - settled) / settled
            row["entry_transient_pct"] = _f(entry, 1)
            if entry > STATE_ENTRY_SIGNIFICANT_PCT:
                row["entry_note"] = (
                    f"Entering this state costs enough that it lifts the "
                    f"average {entry:.0f}% above the settled current. Use "
                    f"settled_mean_ma as the state's current and declare the "
                    f"transition into it with p1150_set_usage_event, or the "
                    f"cost is attributed to time the target spends resting "
                    f"rather than to the {row['visits']} transitions that "
                    f"actually caused it.")
        if battery_mah:
            row["battery_pct"] = _f(100.0 * row["charge_mah"] / battery_mah, 6)
        if name is None:
            unmapped.append(code)
        rows.append(row)

    out = {
        "duration_s": _f(n / fs, 4),
        "states": rows,
        # Every code change, so the count of transitions the firmware made.
        "transitions": int(np.count_nonzero(np.diff(codes))),
        "codes_seen": sorted(int(c) for c in np.unique(codes)),
    }
    if unmapped:
        out["unmapped_codes"] = unmapped
        out["unmapped_note"] = (
            f"The target drove code(s) {unmapped} that no state is declared "
            f"for. Either the firmware has a state the usage model does not, "
            f"or the pins were not driven -- an uninitialised or unpowered "
            f"target reads as code 0. Declare them with p1150_set_state_signal, "
            f"or find out what the firmware is doing there before building an "
            f"estimate that ignores it.")
    named = [r for r in rows if r["state"]]
    if named:
        out["dominant_state"] = max(named, key=lambda r: r["time_s"])["state"]
    return out


def _intervals(mask: np.ndarray) -> tuple:
    """Start (inclusive) and end (exclusive) indices of each True run."""
    edges = np.diff(np.concatenate(([0], mask.view(np.int8), [0])))
    return np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)


def intervals(asserted: np.ndarray) -> tuple:
    """Start (inclusive) and end (exclusive) sample index of each assertion."""
    return _intervals(np.asarray(asserted, dtype=bool))


def assertion_count(asserted: np.ndarray) -> int:
    """How many times the marker went from de-asserted to asserted."""
    return int(intervals(asserted)[0].size)


def marker_survey(x: np.ndarray, cfg: dict = None,
                  fs: int = SAMPLE_RATE, channel: str = None) -> dict:
    """What a raw aux channel actually looks like, for checking the wiring.

    Answers the question that blocks everything else: is the target really
    driving this pin, or is the lead on the wrong pad?  Reports the levels seen
    and, for an analog channel, where a threshold would sensibly sit.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return {"error": "no samples"}

    # Split at the midpoint of the extremes and take the median of each side,
    # rather than a fixed pair of percentiles. A marker asserted for 2% of a
    # capture -- the normal case, not an edge case -- puts both the 2nd and the
    # 98th percentile down in the low rail, which would report the two levels as
    # nearly equal and suggest a threshold the signal never crosses. The median
    # of each side is independent of the duty cycle and still ignores outliers.
    x_min, x_max = float(x.min()), float(x.max())
    mid = 0.5 * (x_min + x_max)
    highs, lows = x[x > mid], x[x <= mid]
    lo_rail = float(np.median(lows)) if lows.size else x_min
    hi_rail = float(np.median(highs)) if highs.size else x_max
    span = hi_rail - lo_rail
    out = {
        "samples":  int(x.size),
        "min":      _f(float(x.min()), 3),
        "max":      _f(float(x.max()), 3),
        "mean":     _f(float(x.mean()), 3),
        "low_level":  _f(lo_rail, 3),
        "high_level": _f(hi_rail, 3),
    }

    asserted = to_logic(x, cfg)
    starts, ends = _intervals(asserted)
    out["transitions"] = int(starts.size + ends.size)
    out["asserted_pct"] = _f(100.0 * float(asserted.mean()), 2)
    out["assertions"] = int(starts.size)

    # A flat channel is the common wiring fault, and it reads very differently
    # depending on which rail it is stuck at, so the two are named separately.
    if starts.size == 0:
        out["verdict"] = "STUCK_LOW" if not asserted.any() else "STUCK_HIGH"
    elif asserted.all():
        out["verdict"] = "STUCK_HIGH"
    elif starts.size > 0.02 * x.size:
        # More than one assertion per 50 samples is not a GPIO marking work.
        out["verdict"] = "NOISY"
    else:
        out["verdict"] = "TOGGLING"
        # Drop assertions clipped by the start or end of the capture. Their true
        # length is unknown, and including them makes a perfectly regular marker
        # look ragged -- a 2 s capture of a 50 ms marker reported a 14 ms
        # shortest assertion purely because recording began part way through
        # one, which reads as a glitching GPIO and sends the developer looking
        # for a fault that is not there.
        full = np.ones(starts.size, dtype=bool)
        if starts[0] == 0:
            full[0] = False
        if ends[-1] == x.size:
            full[-1] = False
        clipped = int((~full).sum())
        d_ms = (ends - starts)[full if full.any() else slice(None)] / fs * 1000.0
        out["mean_assertion_ms"] = _f(float(d_ms.mean()), 4)
        out["shortest_assertion_ms"] = _f(float(d_ms.min()), 4)
        if clipped:
            out["clipped_at_capture_edge"] = clipped
            out["clipped_note"] = (
                f"{clipped} assertion(s) ran past the start or end of this "
                f"check and are counted but excluded from the timings above.")

    # Only A0 takes a threshold, so only A0 gets a suggestion -- offering one
    # for a digital channel would invite setting a threshold that overrides the
    # known-good fixed levels with a guess. The midpoint of the two observed
    # rails is the same choice a scope makes.
    if span > 0 and (channel or "").upper() == "A0":
        out["suggested_threshold_mv"] = _f(lo_rail + span / 2.0, 1)
        out["suggested_hysteresis_mv"] = _f(max(50.0, 0.15 * span), 1)
    return out


def marker_analysis(i_ma: np.ndarray, asserted: np.ndarray,
                    fs: int = SAMPLE_RATE, battery_mah: float = None,
                    min_duration_us: float = 0.0,
                    max_listed: int = 20) -> dict:
    """Current statistics for the regions where the marker was asserted.

    Reports each assertion's duration, charge and peak, the aggregate over all
    of them, and -- separately -- the current outside the markers.  The
    difference between those two is what the marked code actually costs: the
    charge measured inside the window includes whatever the rest of the target
    was drawing anyway, and only the excess over that floor is attributable.
    """
    i64 = np.asarray(i_ma, dtype=np.float64)
    asserted = np.asarray(asserted, dtype=bool)
    n = int(min(i64.size, asserted.size))
    if n == 0:
        return {"error": "capture contains no samples"}
    i64, asserted = i64[:n], asserted[:n]

    starts, ends = _intervals(asserted)
    dropped = 0
    if min_duration_us > 0 and starts.size:
        min_samples = max(1, int(min_duration_us * 1e-6 * fs))
        keep = (ends - starts) >= min_samples
        dropped = int((~keep).sum())
        starts, ends = starts[keep], ends[keep]

    duration_s = n / fs
    out = {
        "capture_duration_s": _f(duration_s, 4),
        "occurrences": int(starts.size),
        "sample_resolution_us": _f(1e6 / fs, 2),
    }
    if dropped:
        out["dropped_short_assertions"] = dropped

    if starts.size == 0:
        out["diagnosis"] = (
            "The marker never asserted during this capture. Either the firmware "
            "did not reach the instrumented code, the GPIO is not being driven, "
            "the lead is on the wrong pin, or the polarity is inverted (try "
            "active_high=False). Run p1150_aux_check to see what the input is "
            "actually doing.")
        return out

    # An assertion clipped by the start or end of the capture has an unknown
    # true duration, so including it would drag every average toward a number
    # that describes the capture window rather than the target.
    truncated = int(starts[0] == 0) + int(ends[-1] == n)
    if truncated:
        out["truncated_edges"] = truncated
        out["truncated_note"] = (
            "The capture begins or ends part way through an assertion "
            f"({truncated} of its two edges is outside the window). Those "
            "assertions are still counted, but excluded from the duration and "
            "charge statistics, which they would otherwise bias low. Capture "
            "for longer, or start the capture while the marker is idle.")
    full = np.ones(starts.size, dtype=bool)
    if starts[0] == 0:
        full[0] = False
    if ends[-1] == n:
        full[-1] = False

    # Exact per-interval sums from one cumulative pass, rather than a Python
    # loop over what can be tens of thousands of assertions.
    csum = np.concatenate(([0.0], np.cumsum(i64)))
    sums = csum[ends] - csum[starts]
    lengths = (ends - starts).astype(np.float64)
    durations_s = lengths / fs
    charges_uah = sums / fs / 3600.0 * 1000.0
    means = sums / lengths
    peaks = np.array([i64[a:b].max() for a, b in zip(starts, ends)])

    inside = asserted
    outside = ~asserted
    n_out = int(outside.sum())
    baseline_ma = float(i64[outside].mean()) if n_out else 0.0

    # What the marked work costs above the current the target draws anyway.
    # Attributing the whole window to the feature would charge it for the idle
    # floor as well, which is what makes two markers of different length look
    # like different features when they are not.
    excess_uah = charges_uah - baseline_ma * durations_s / 3600.0 * 1000.0

    f = full if full.any() else np.ones_like(full)   # all truncated: use all
    out.update({
        "asserted": {
            "time_s":     _f(float(inside.sum()) / fs, 4),
            "time_pct":   _f(100.0 * float(inside.mean()), 3),
            "mean_ma":    _f(float(i64[inside].mean())),
            "peak_ma":    _f(float(i64[inside].max())),
            "charge_mah": _f(float(i64[inside].sum()) / fs / 3600.0),
        },
        "deasserted": {
            "time_s":     _f(float(n_out) / fs, 4),
            "time_pct":   _f(100.0 * float(outside.mean()), 3),
            "mean_ma":    _f(baseline_ma),
            "peak_ma":    _f(float(i64[outside].max())) if n_out else None,
            "charge_mah": _f(float(i64[outside].sum()) / fs / 3600.0)
                          if n_out else 0.0,
        },
        "per_occurrence": {
            "mean_duration_ms":   _f(float(durations_s[f].mean()) * 1000.0, 4),
            "min_duration_ms":    _f(float(durations_s[f].min()) * 1000.0, 4),
            "max_duration_ms":    _f(float(durations_s[f].max()) * 1000.0, 4),
            "duration_jitter_pct": _f(
                100.0 * float(durations_s[f].std()) /
                float(durations_s[f].mean()), 2)
                if durations_s[f].mean() > 0 else None,
            "mean_charge_uah":    _f(float(charges_uah[f].mean())),
            "max_charge_uah":     _f(float(charges_uah[f].max())),
            "mean_excess_uah":    _f(float(excess_uah[f].mean())),
            "mean_current_ma":    _f(float(means[f].mean())),
            "mean_peak_ma":       _f(float(peaks[f].mean())),
            "max_peak_ma":        _f(float(peaks[f].max())),
        },
        "baseline_ma": _f(baseline_ma),
        "total_charge_in_markers_mah": _f(float(charges_uah.sum()) / 1000.0),
        "total_excess_charge_mah": _f(float(excess_uah.sum()) / 1000.0),
        "share_of_capture_charge_pct": _f(
            100.0 * float(sums.sum()) / float(i64.sum()), 2)
            if i64.sum() > 0 else None,
    })

    if starts.size >= 2:
        periods_s = np.diff(starts) / fs
        mp = float(periods_s.mean())
        out["repetition"] = {
            "mean_period_s": _f(mp, 4),
            "rate_hz": _f(1.0 / mp, 4) if mp > 0 else None,
            "period_jitter_pct": _f(100.0 * float(periods_s.std()) / mp, 2)
                                 if mp > 0 else None,
            "duty_cycle_pct": _f(
                100.0 * float(durations_s[f].mean()) / mp, 3) if mp > 0 else None,
        }

    # The single worst occurrence, by name. A mean hides the one iteration that
    # hit a retry path or a cache miss, and that outlier is usually the bug.
    worst_q = int(np.argmax(charges_uah))
    worst_t = int(np.argmax(durations_s))
    out["worst_occurrence"] = {
        "by_charge": {"index": worst_q,
                      "start_s": _f(float(starts[worst_q]) / fs, 4),
                      "charge_uah": _f(float(charges_uah[worst_q])),
                      "duration_ms": _f(float(durations_s[worst_q]) * 1000.0, 4)},
        "by_duration": {"index": worst_t,
                        "start_s": _f(float(starts[worst_t]) / fs, 4),
                        "charge_uah": _f(float(charges_uah[worst_t])),
                        "duration_ms": _f(float(durations_s[worst_t]) * 1000.0, 4)},
    }

    if battery_mah:
        per_mah = float(charges_uah[f].mean()) / 1000.0
        out["battery_mah"] = battery_mah
        out["battery_pct_per_occurrence"] = _f(
            100.0 * per_mah / battery_mah, 8)
        out["occurrences_per_battery"] = int(battery_mah / per_mah) \
            if per_mah > 0 else None

    # Listed individually only up to a cap: a long capture can hold thousands of
    # assertions, and the aggregate above already describes them.
    if max_listed and starts.size:
        step = max(1, int(np.ceil(starts.size / max_listed)))
        rows = []
        for k in range(0, starts.size, step):
            rows.append({
                "index":       int(k),
                "start_s":     _f(float(starts[k]) / fs, 4),
                "duration_ms": _f(float(durations_s[k]) * 1000.0, 4),
                "charge_uah":  _f(float(charges_uah[k])),
                "mean_ma":     _f(float(means[k])),
                "peak_ma":     _f(float(peaks[k])),
            })
        out["occurrence_sample"] = rows
        if step > 1:
            out["occurrence_sample_note"] = (
                f"Every {step}th assertion of {starts.size}, as an evenly "
                f"spread sample. The statistics above cover all of them.")
    return out


def compare_markers(base_i: np.ndarray, base_asserted: np.ndarray,
                    cand_i: np.ndarray, cand_asserted: np.ndarray,
                    fs: int = SAMPLE_RATE, threshold_pct: float = 5.0,
                    battery_mah: float = None,
                    min_duration_us: float = 0.0,
                    fs_cand: int = None) -> dict:
    """Regression check over just the marked region of two runs.

    Charge per occurrence is the verdict metric, not average current over the
    capture: it is independent of how many times the marked code happened to run
    and of everything the target did outside it, which is exactly what makes a
    marked comparison stricter than a whole-capture one.
    """
    b = marker_analysis(base_i, base_asserted, fs, battery_mah, min_duration_us)
    c = marker_analysis(cand_i, cand_asserted, fs_cand or fs, battery_mah,
                        min_duration_us)

    if not b.get("occurrences") or not c.get("occurrences"):
        missing = "baseline" if not b.get("occurrences") else "candidate"
        return {"verdict": "UNKNOWN",
                "error": f"The marker never asserted in the {missing} run, so "
                         f"there is nothing to compare. Check that the "
                         f"instrumented code ran, and that both runs used the "
                         f"same aux channel and polarity.",
                "baseline": b, "candidate": c}

    pb, pc = b["per_occurrence"], c["per_occurrence"]
    metrics = {
        "charge_per_occurrence_uah": _delta(pb["mean_charge_uah"],
                                            pc["mean_charge_uah"]),
        "excess_per_occurrence_uah": _delta(pb["mean_excess_uah"],
                                            pc["mean_excess_uah"]),
        "duration_ms":               _delta(pb["mean_duration_ms"],
                                            pc["mean_duration_ms"]),
        "mean_current_ma":           _delta(pb["mean_current_ma"],
                                            pc["mean_current_ma"]),
        "peak_ma":                   _delta(pb["mean_peak_ma"],
                                            pc["mean_peak_ma"]),
        "baseline_outside_marker_ma": _delta(b["baseline_ma"], c["baseline_ma"]),
        "occurrences":               _delta(float(b["occurrences"]),
                                            float(c["occurrences"])),
    }

    pct = metrics["charge_per_occurrence_uah"]["pct"]
    if pct is None:
        verdict = "UNKNOWN"
    elif pct > threshold_pct:
        verdict = "REGRESSION"
    elif pct < -threshold_pct:
        verdict = "IMPROVEMENT"
    else:
        verdict = "PASS"

    # Duration and current move independently, and the fix differs: slower code
    # versus code that now runs a more expensive peripheral. Naming which one
    # moved is the whole point of having the marker.
    notes = []
    d_dur = metrics["duration_ms"]["pct"]
    d_cur = metrics["mean_current_ma"]["pct"]
    if d_dur is not None and abs(d_dur) > threshold_pct:
        notes.append(
            f"The marked region takes {abs(d_dur):.1f}% "
            f"{'longer' if d_dur > 0 else 'less time'} "
            f"({pb['mean_duration_ms']} -> {pc['mean_duration_ms']} ms). The "
            f"code itself got {'slower' if d_dur > 0 else 'faster'} -- look for "
            f"added work, retries, a busy-wait, or a changed clock.")
    if d_cur is not None and abs(d_cur) > threshold_pct:
        notes.append(
            f"Current during the marked region moved {d_cur:+.1f}% "
            f"({pb['mean_current_ma']} -> {pc['mean_current_ma']} mA) while it "
            f"runs. Something the code enables now draws differently -- a "
            f"peripheral, radio TX power, a regulator mode, or a clock.")
    if not notes and verdict != "PASS":
        notes.append(
            "Charge per occurrence moved without a clear change in either "
            "duration or current draw. Check the occurrence spread: an "
            "occasional expensive iteration shifts the mean without shifting "
            "the typical case.")
    db = metrics["baseline_outside_marker_ma"]["pct"]
    if db is not None and abs(db) > threshold_pct:
        notes.append(
            f"Current OUTSIDE the marker also moved {db:+.1f}%. That is not "
            f"attributable to the marked code -- treat it as a separate change "
            f"and check the sleep floor with p1150_compare.")

    return {
        "verdict": verdict,
        "threshold_pct": threshold_pct,
        "charge_per_occurrence_change_pct": pct,
        "metrics": metrics,
        "likely_cause": notes,
        "baseline": b,
        "candidate": c,
    }


# ------------------------------------------------------------------ #
# Charging                                                             #
# ------------------------------------------------------------------ #
def charge_test(i_ma: np.ndarray, isnk_ma: np.ndarray,
                fs: int = SAMPLE_RATE, battery_mah: float = None) -> dict:
    """Assess a target's ability to charge the battery it is standing in for.

    The P1150 sits at the battery terminals, so what it measures IS what the
    battery would see: the charger's output minus whatever the rest of the
    target is drawing at the same moment.  No arithmetic is needed to separate
    them, and none is possible -- a single pair of terminals only carries the
    net.

    Sign convention: net = i - isnk.  Positive net means the target is still
    consuming (the battery would be discharging); negative net means current is
    flowing into the battery, i.e. charging.
    """
    if i_ma.size == 0:
        return {"error": "capture contains no samples"}

    i64 = i_ma.astype(np.float64, copy=False)
    s64 = isnk_ma.astype(np.float64, copy=False)
    n = min(i64.size, s64.size)
    i64, s64 = i64[:n], s64[:n]

    net = i64 - s64                      # + = discharging, - = charging
    charge_ma = -net                     # + = charging, the friendlier sign
    mean_charge = float(charge_ma.mean())
    duration_s = n / fs
    sinking = s64 > ISNK_FLOOR_MA
    sink_pct = 100.0 * float(sinking.mean())

    out = {
        "duration_s":        _f(duration_s, 4),
        "mean_source_ma":    _f(float(i64.mean())),
        "mean_sink_ma":      _f(float(s64.mean())),
        "net_charge_ma":     _f(mean_charge),
        "peak_sink_ma":      _f(float(s64.max())),
        "time_sinking_pct":  _f(sink_pct, 2),
        # Net charge delivered over the capture. Negative means the battery lost
        # charge despite a charger being attached.
        "net_charge_mah":    _f(float(charge_ma.sum() / fs / 3600.0)),
    }

    if sink_pct < 1.0:
        out["result"] = "NOT_CHARGING"
        out["diagnosis"] = (
            "No sink current was measured: nothing is pushing current back into "
            "the battery terminals. Either the charging source is not connected "
            "or not enabled, the target's charger IC is not running, or the "
            "charge path to the battery terminals is open. The P1150 measured "
            f"{out['mean_source_ma']} mA flowing OUT, so the target is running "
            f"on the P1150 alone.")
        return out

    if mean_charge <= 0:
        out["result"] = "NET_DISCHARGING"
        out["diagnosis"] = (
            f"A charging source is present (sink current seen for "
            f"{out['time_sinking_pct']}% of the capture) but the target consumes "
            f"more than it delivers: net {_f(-mean_charge)} mA still flowing "
            f"OUT of the battery. The battery will drain more slowly but will "
            f"never reach full. Typical cause: measuring while the radio or a "
            f"high-power peripheral is active, or a charger current limit set "
            f"below the target's own consumption.")
        return out

    out["result"] = "CHARGING"

    # Stability: a charger cycling in and out reads the same on average as a
    # steady one, but means something quite different is happening.
    if sink_pct < 95.0:
        out["stability"] = "INTERMITTENT"
        out["stability_note"] = (
            f"Current only flowed into the battery for {out['time_sinking_pct']}% "
            f"of the capture. The charger is cycling rather than delivering "
            f"steadily -- check for thermal foldback, an input supply sagging "
            f"under load, or the charger re-qualifying its input.")
    else:
        out["stability"] = "STEADY"

    if battery_mah:
        # C rate: 1C charges a nominal capacity in one hour, which is the rate
        # most targets are designed around.
        c_rate = mean_charge / battery_mah
        out["battery_mah"] = battery_mah
        out["c_rate"] = _f(c_rate, 3)
        out["time_to_full_hours_from_empty"] = _f(battery_mah / mean_charge, 2)
        out["note_time_to_full"] = (
            "Constant-current estimate. A real charger tapers in its "
            "constant-voltage phase near full, so expect longer in practice.")
        if c_rate < 0.02:
            out["c_rate_assessment"] = (
                f"{c_rate:.3f}C is very low -- barely a trickle. A "
                f"{battery_mah:g} mAh battery would take "
                f"{battery_mah / mean_charge:.0f} hours from empty. Check the "
                f"charger's programmed current, and whether the target is "
                f"drawing most of it.")
        elif c_rate < 0.5:
            out["c_rate_assessment"] = (
                f"{c_rate:.2f}C is a conservative charge rate -- safe, and "
                f"gentler on cell life, but slower than the ~1C most designs "
                f"target.")
        elif c_rate <= 1.5:
            out["c_rate_assessment"] = (
                f"{c_rate:.2f}C is the usual design point (1C charges a "
                f"{battery_mah:g} mAh cell in about an hour).")
        else:
            out["c_rate_assessment"] = (
                f"{c_rate:.2f}C is high. Confirm the cell is rated for it -- "
                f"most Li-ion cells specify 1C or below for charging, and "
                f"exceeding it shortens life or is unsafe.")
    else:
        out["battery_capacity_missing"] = (
            "Set the battery capacity with p1150_set_battery to get the C rate "
            "and a time-to-full estimate. Ask the developer for the pack's mAh "
            "rating -- it cannot be inferred from the measurement.")
    return out


# ------------------------------------------------------------------ #
# Regression comparison                                                #
# ------------------------------------------------------------------ #
def _delta(base, cand):
    """Absolute and percent change, guarding a zero baseline."""
    if base is None or cand is None:
        return None
    d = cand - base
    return {
        "baseline": _f(base),
        "candidate": _f(cand),
        "delta": _f(d),
        "pct": _f(100.0 * d / base, 2) if base else None,
    }


def _signature(sm_b, sm_c, ev_b, ev_c, tol_pct):
    """Name the most likely cause of a change, as a hypothesis for the developer.

    Deliberately heuristic.  The value is not certainty, it is pointing at the
    right subsystem: "your sleep floor moved" and "your radio duty cycle moved"
    lead to completely different places in the firmware.
    """
    notes = []

    def moved(b, c, tol=tol_pct):
        return b and c and abs(100.0 * (c - b) / b) > tol

    floor_up = sm_c["sleep_floor_ma"] > sm_b["sleep_floor_ma"] and \
        moved(sm_b["sleep_floor_ma"], sm_c["sleep_floor_ma"])
    if floor_up:
        notes.append(
            f"Sleep floor rose {sm_b['sleep_floor_ma']:.4f} -> "
            f"{sm_c['sleep_floor_ma']:.4f} mA. Typical cause: a peripheral, "
            f"clock or regulator left enabled, or a pin left driven, so the "
            f"target no longer reaches its lowest sleep state.")

    nb, nc = ev_b.get("events", 0), ev_c.get("events", 0)
    if nb and nc:
        if moved(ev_b.get("mean_duration_ms"), ev_c.get("mean_duration_ms")):
            d = "longer" if ev_c["mean_duration_ms"] > ev_b["mean_duration_ms"] else "shorter"
            notes.append(
                f"Wake bursts are {d}: {ev_b['mean_duration_ms']:.3f} -> "
                f"{ev_c['mean_duration_ms']:.3f} ms. Typical cause: more (or "
                f"less) work done per wake-up.")
        if moved(ev_b.get("mean_period_s"), ev_c.get("mean_period_s")):
            d = "more often" if ev_c["mean_period_s"] < ev_b["mean_period_s"] else "less often"
            notes.append(
                f"Target wakes {d}: period {ev_b['mean_period_s']:.4f} -> "
                f"{ev_c['mean_period_s']:.4f} s. Typical cause: a changed timer "
                f"interval, advertising interval or poll rate.")
        if moved(ev_b.get("mean_peak_ma"), ev_c.get("mean_peak_ma")):
            notes.append(
                f"Burst peak moved {ev_b['mean_peak_ma']:.3f} -> "
                f"{ev_c['mean_peak_ma']:.3f} mA. Typical cause: radio TX power, "
                f"clock speed, or a regulator mode change.")
    elif nc and not nb:
        notes.append("Bursts appeared that the baseline did not have: new "
                     "periodic activity (timer, advertising, polling).")
    elif nb and not nc:
        notes.append("Bursts present in the baseline are gone: the periodic "
                     "activity stopped -- confirm the workload actually ran.")

    if not notes and moved(sm_b["avg_ma"], sm_c["avg_ma"]):
        notes.append("Average current moved but neither the floor nor the burst "
                     "shape did clearly. Look for a uniform offset: leakage, a "
                     "pull-up, or a load added in parallel.")
    return notes


def compare(base: np.ndarray, cand: np.ndarray, fs: int = SAMPLE_RATE,
            threshold_pct: float = 5.0, battery_mah: float = None,
            fs_cand: int = None) -> dict:
    """Baseline vs candidate, with a verdict and a likely cause."""
    fs_c = fs_cand or fs
    sm_b = summarize(base, fs, battery_mah)
    sm_c = summarize(cand, fs_c, battery_mah)
    ev_b = find_events(base, fs)
    ev_c = find_events(cand, fs_c)

    dur_b, dur_c = sm_b["duration_s"], sm_c["duration_s"]
    dur_mismatch_pct = abs(100.0 * (dur_c - dur_b) / dur_b) if dur_b else 0.0

    metrics = {
        "avg_ma":         _delta(sm_b["avg_ma"], sm_c["avg_ma"]),
        "sleep_floor_ma": _delta(sm_b["sleep_floor_ma"], sm_c["sleep_floor_ma"]),
        "peak_ma":        _delta(sm_b["peak_ma"], sm_c["peak_ma"]),
        "median_ma":      _delta(sm_b["median_ma"], sm_c["median_ma"]),
    }
    # Total mAh only compares like with like.  Two captures of different length
    # trivially differ in accumulated charge, which would read as a huge
    # regression for no reason -- so it is withheld rather than shown wrongly.
    if dur_mismatch_pct <= 5.0:
        metrics["charge_mah"] = _delta(sm_b["charge_mah"], sm_c["charge_mah"])

    pct = metrics["avg_ma"]["pct"] if metrics["avg_ma"] else None
    if pct is None:
        verdict = "UNKNOWN"
    elif pct > threshold_pct:
        verdict = "REGRESSION"
    elif pct < -threshold_pct:
        verdict = "IMPROVEMENT"
    else:
        verdict = "PASS"

    out = {
        "verdict": verdict,
        "threshold_pct": threshold_pct,
        "avg_current_change_pct": pct,
        "metrics": metrics,
        "baseline": sm_b,
        "candidate": sm_c,
        "baseline_events": ev_b,
        "candidate_events": ev_c,
        "likely_cause": _signature(sm_b, sm_c, ev_b, ev_c, threshold_pct),
    }
    if dur_mismatch_pct > 5.0:
        out["warning"] = (
            f"Capture lengths differ by {dur_mismatch_pct:.1f}% "
            f"({dur_b}s vs {dur_c}s). Total charge_mah is not comparable and "
            f"has been omitted; avg_ma remains valid. For a defensible "
            f"regression number, re-run both over the same duration and the "
            f"same workload.")
    key = "projected_hours_if_continuous"
    if battery_mah and sm_b.get(key) and sm_c.get(key):
        # A ratio of two same-assumption projections, so the assumption cancels:
        # this is a valid statement about how much the two runs differ, but not
        # about the product's battery life unless the workload is the whole
        # duty cycle.  p1150_battery_life is the tool for the latter.
        out["projected_life_if_continuous"] = _delta(sm_b[key], sm_c[key])
    return out
