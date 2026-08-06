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
        # Only meaningful when the capture is representative of steady-state
        # duty cycle -- a 2 s capture of a boot sequence projects nonsense.
        out["battery_mah"] = battery_mah
        out["projected_hours"] = _f(battery_mah / avg, 2) if avg > 0 else None
        out["projected_days"] = _f(battery_mah / avg / 24, 2) if avg > 0 else None
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
            threshold_pct: float = 5.0, battery_mah: float = None) -> dict:
    """Baseline vs candidate, with a verdict and a likely cause."""
    sm_b = summarize(base, fs, battery_mah)
    sm_c = summarize(cand, fs, battery_mah)
    ev_b = find_events(base, fs)
    ev_c = find_events(cand, fs)

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
    if battery_mah and sm_b.get("projected_hours") and sm_c.get("projected_hours"):
        out["projected_battery_life"] = _delta(sm_b["projected_hours"],
                                               sm_c["projected_hours"])
    return out
