# -*- coding: utf-8 -*-
"""
MIT License

Per-project settings, persisted next to the stored runs.

Three things belong here, and for the same reason: none can be derived from a
current waveform or guessed from the code, all are properties of the project
rather than of any one measurement, and all would otherwise have to be re-asked
every session.

* Battery capacity turns raw current into the numbers a developer actually
  reasons about: how long the device lasts, what share of the battery one
  wake-up costs, whether a measured charge current is a sensible C rate.  The
  cell's chemistry and internal resistance, alongside the voltage the target
  stops working at, turn a measured current surge into the question that
  matters -- would the real battery sag far enough to reset it.

* The usage model -- how the product's time divides between its states, and how
  often its one-off events happen -- is the other half of a battery-life
  estimate, and the half no instrument can supply.  A P1150 can measure a
  standby current to a fraction of a microamp in thirty seconds; only the
  developer knows the device is in standby 99% of the time.  Life is
  capacity / SUM(fraction x current), so the measurement contributes the
  currents and the model contributes the weights.  Keeping the weights here is
  what lets a week of battery life be predicted from a minute of measuring.

* Auxiliary-input setup describes what the target drives into A0/D0/D1 and what
  counts as "asserted" on it.  A0 is an analog input reported in millivolts, so
  the logic threshold is a decision about the target's IO voltage, not something
  the instrument knows.
"""
import os
import json

from . import storage
from .analysis import (AUX_INPUT_MAX_MV, D0S_BAUD, MARK_ENTER,
                       MARK_EXIT_OF_ENTER)

_FILE = "battery.json"
_AUX_FILE = "aux.json"
_USAGE_FILE = "usage.json"

# The P1150's auxiliary inputs.  A0 is analog, accepting 0-17 V and reported in
# millivolts; D0 and D1 are logic inputs accepting 1.2-3.3 V.  Names are upper
# case throughout the MCP surface and lower case in the driver's acquisition
# dict.  The electrical detail lives in analysis.py, next to the code that
# decodes the levels.
AUX_CHANNELS = ("A0", "D0", "D1")

# Default hysteresis for an A0 marker when the caller does not give one: a band
# of +/-10% of the threshold, floored at 50 mV.  A GPIO edge seen through a
# probe lead has finite slew and some ringing; without a band, one crossing can
# be counted as several assertions.
AUX_HYST_FRACTION = 0.10
AUX_HYST_MIN_MV = 50.0


def _path() -> str:
    return os.path.join(storage.runs_dir(), _FILE)


def get() -> dict:
    """Current battery settings.  Empty dict if never configured."""
    try:
        with open(_path(), "r") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    # Environment seeds the capacity so an existing .mcp.json keeps working,
    # but an explicit p1150_set_battery call wins.
    if not cfg.get("capacity_mah") and os.environ.get("P1150_BATTERY_MAH"):
        try:
            cfg["capacity_mah"] = float(os.environ["P1150_BATTERY_MAH"])
            cfg["source"] = "P1150_BATTERY_MAH environment variable"
        except ValueError:
            pass
    return cfg


def set_battery(capacity_mah: float = None, chemistry: str = None,
                nominal_mv: int = None, esr_mohm: float = None,
                brownout_mv: int = None, target_days: float = None) -> dict:
    """Record what the target's battery is, and what the target needs of it.

    Merges rather than replaces: the ESR and the brown-out threshold usually
    come up later, when an inrush measurement raises the question, and having to
    restate the capacity to add one of them invites getting it wrong.
    """
    cfg = get()
    if capacity_mah:
        cfg["capacity_mah"] = float(capacity_mah)
    if chemistry:
        cfg["chemistry"] = chemistry
    if nominal_mv:
        cfg["nominal_mv"] = int(nominal_mv)
    if esr_mohm:
        cfg["esr_mohm"] = float(esr_mohm)
    if brownout_mv:
        cfg["brownout_mv"] = int(brownout_mv)
    if target_days:
        cfg["target_days"] = float(target_days)
    cfg["source"] = "p1150_set_battery"
    with open(_path(), "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def capacity_mah():
    """Configured capacity in mAh, or None."""
    return get().get("capacity_mah") or None


def target_days():
    """Battery life the product is required to achieve, in days, or None."""
    return get().get("target_days") or None


def battery_model() -> dict:
    """What the inrush analysis needs to say how a real cell would behave.

    Separate from capacity_mah() because none of it is required: an inrush is
    still worth reporting without knowing the chemistry, it just cannot be
    turned into a voltage sag as confidently.
    """
    cfg = get()
    return {"chemistry": cfg.get("chemistry"),
            "esr_mohm": cfg.get("esr_mohm"),
            "brownout_mv": cfg.get("brownout_mv")}


# ------------------------------------------------------------------ #
# Usage model                                                          #
# ------------------------------------------------------------------ #
# How the product spends its life, in two forms, because real duty cycles are
# described in two different ways and folding them together loses information.
#
# A STATE is continuous and weighted by a fraction of time: "standby 99.2%,
# connected 0.8%".  Its cost is a current, measured for as long as that state
# takes to be representative and no longer.
#
# An EVENT is discrete and weighted by a rate: "boots twice a day", "the user
# opens the app twenty times a day", "an OTA once a month".  Its cost is a
# charge per occurrence, not a current.  Trying to express one of these as a
# time fraction is the usual way an estimate goes wrong -- a boot is 0.004% of
# a day and rounds to nothing as a fraction, while as 2 x 410 uAh it is plainly
# a third of the budget.
#
# The two reconcile in one line, which is what makes a single estimator
# possible:   equivalent_ma = charge_uah x occurrences_per_hour / 1000
#
# Fractions are stored as percentages and rates per day, because those are the
# units a developer states them in.  Names are matched case-insensitively but
# stored as typed, since they appear in the report.
def _usage_path() -> str:
    return os.path.join(storage.runs_dir(), _USAGE_FILE)


def get_usage() -> dict:
    """The full usage model: {"states": {...}, "events": {...}, "signal": {...}}."""
    try:
        with open(_usage_path(), "r") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("states", {})
    cfg.setdefault("events", {})
    cfg.setdefault("signal", {})
    cfg.setdefault("marks", {})
    return cfg


def _write_usage(cfg: dict) -> dict:
    with open(_usage_path(), "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def _key(name: str) -> str:
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("A name is required, e.g. 'standby' or 'active'.")
    return key


def set_usage_state(name: str, fraction_pct: float = None,
                    hours_per_day: float = None, description: str = None,
                    run_id: str = None) -> dict:
    """Declare a state the product spends part of its life in.

    hours_per_day is accepted alongside fraction_pct because developers state
    the split both ways and converting by hand is an easy place to drop a factor
    of 24.  Only one may be given; they are the same number twice.
    """
    key = _key(name)
    if fraction_pct is not None and hours_per_day is not None:
        raise ValueError(
            "Give fraction_pct or hours_per_day, not both -- they express the "
            "same quantity.")
    if hours_per_day is not None:
        if not 0 <= hours_per_day <= 24:
            raise ValueError(f"hours_per_day must be between 0 and 24, got "
                             f"{hours_per_day:g}.")
        fraction_pct = 100.0 * float(hours_per_day) / 24.0
    if fraction_pct is not None and not 0 <= fraction_pct <= 100:
        raise ValueError(f"fraction_pct must be between 0 and 100, got "
                         f"{fraction_pct:g}.")

    cfg = get_usage()
    entry = dict(cfg["states"].get(key) or {})
    if fraction_pct is None and not entry:
        raise ValueError(
            f"State '{name}' is new, so its share of the product's time is "
            f"required. Ask the developer what fraction of the time the device "
            f"is in this state and pass fraction_pct (or hours_per_day). It "
            f"cannot be measured -- it is a property of how the product is "
            f"used.")
    entry["name"] = (name or "").strip()
    if fraction_pct is not None:
        entry["fraction_pct"] = float(fraction_pct)
    if description:
        entry["description"] = description
    if run_id:
        # Pinning a run overrides "newest capture for this state".  Worth having
        # for the case where the latest measurement is the experiment and an
        # older one is still the reference.
        entry["run_id"] = run_id
    cfg["states"][key] = entry
    return _write_usage(cfg)


def set_usage_event(name: str, per_day: float = None, per_hour: float = None,
                    charge_uah: float = None, run_id: str = None,
                    description: str = None) -> dict:
    """Declare something that happens a number of times per day and costs charge
    each time it does."""
    key = _key(name)
    if per_day is not None and per_hour is not None:
        raise ValueError(
            "Give per_day or per_hour, not both -- they express the same rate.")
    if per_hour is not None:
        per_day = 24.0 * float(per_hour)
    if per_day is not None and per_day < 0:
        raise ValueError("A rate cannot be negative.")
    if charge_uah is not None and charge_uah < 0:
        raise ValueError("charge_uah cannot be negative.")

    cfg = get_usage()
    entry = dict(cfg["events"].get(key) or {})
    if per_day is None and not entry:
        raise ValueError(
            f"Event '{name}' is new, so how often it happens is required. Ask "
            f"the developer for a rate and pass per_day (or per_hour).")
    entry["name"] = (name or "").strip()
    if per_day is not None:
        entry["per_day"] = float(per_day)
    if charge_uah is not None:
        entry["charge_uah"] = float(charge_uah)
    if run_id:
        entry["run_id"] = run_id
    if description:
        entry["description"] = description
    if not entry.get("run_id") and entry.get("charge_uah") is None:
        raise ValueError(
            f"Event '{name}' needs a cost: either charge_uah, or run_id of a "
            f"capture containing exactly one occurrence (a boot capture, or a "
            f"p1150_capture_single of the event).")
    cfg["events"][key] = entry
    return _write_usage(cfg)


def clear_usage(name: str = None) -> dict:
    """Remove one state or event by name, or the whole model."""
    if name is None:
        return _write_usage({"states": {}, "events": {}, "signal": {},
                             "marks": {}})
    cfg = get_usage()
    key = _key(name)
    if not (cfg["states"].pop(key, None) or cfg["events"].pop(key, None)):
        raise ValueError(f"Nothing named '{name}' in the usage model.")
    cfg["signal"].get("codes", {}).pop(key, None)
    cfg["marks"].get("symbols", {}).pop(key, None)
    return _write_usage(cfg)


def usage_fraction_total() -> float:
    """Sum of the declared state fractions.  Should be 100."""
    return sum(float(s.get("fraction_pct") or 0.0)
               for s in get_usage()["states"].values())


# ------------------------------------------------------------------ #
# State signal                                                         #
# ------------------------------------------------------------------ #
# The target telling the instrument which state it is in, by driving a code on
# the digital aux inputs.  Two pins carry four states, which is enough for most
# battery products -- sleep, idle, active, transmitting.
#
# This exists because the alternative is a human asserting it.  "Put it into
# standby and tell me when" is the weakest step in a battery-life measurement:
# it is slow, it cannot be repeated identically, and when it is wrong the
# capture looks perfectly normal.  A firmware that declares its own state turns
# that claim into a fact recorded sample-for-sample alongside the current, and a
# measurement can then be restricted to exactly the samples where the target
# said it was in the state.
#
# Codes are stored keyed by state name rather than by code, so that clearing a
# state from the usage model takes its code with it.
SIGNAL_CHANNELS = ("D0", "D1")


def get_state_signal() -> dict:
    """{"channels": ["D0","D1"], "codes": {"sleep": 0, ...}} or {}."""
    return dict(get_usage().get("signal") or {})


def set_state_signal(state: str, code: int, channels: list = None) -> dict:
    """Map one state name to the code the firmware drives for it."""
    key = _key(state)
    sig = get_state_signal()
    channels = [c.upper() for c in (channels or sig.get("channels")
                                    or SIGNAL_CHANNELS)]
    for ch in channels:
        if ch not in SIGNAL_CHANNELS:
            raise ValueError(
                f"A state signal uses the digital inputs {', '.join(SIGNAL_CHANNELS)}, "
                f"got '{ch}'. They rescale whatever the target drives to fixed "
                f"levels, so no threshold is needed and a 1.8 V target works the "
                f"same as a 3.3 V one. A0 is left for a region marker.")
    if "D0" in channels and mark_symbols():
        raise ValueError(
            f"D0 already carries the serial mark stream "
            f"({', '.join(sorted(mark_symbols()))}), and one pin cannot be "
            f"both a logic level and a serial input. Put the signal on D1 "
            f"alone -- channels=['D1'], two codes -- or clear the marks with "
            f"p1150_clear_state_marks. Bear in mind the marks already give "
            f"thirty states, and nested ones, where the pin code gives four.")
    if sig.get("channels") and list(sig["channels"]) != channels:
        raise ValueError(
            f"The state signal already uses {', '.join(sig['channels'])}. "
            f"Clear it with p1150_clear_state_signal before changing which "
            f"pins carry it -- a run captured under one pin assignment cannot "
            f"be decoded under another.")

    code = int(code)
    limit = (1 << len(channels)) - 1
    if not 0 <= code <= limit:
        raise ValueError(
            f"code must be between 0 and {limit} for {len(channels)} "
            f"channel(s), got {code}.")
    codes = dict(sig.get("codes") or {})
    for other, c in list(codes.items()):
        if c == code and other != key:
            raise ValueError(
                f"Code {code} is already assigned to '{other}'. Two states "
                f"cannot share a code -- the capture could not tell them "
                f"apart. Give this one a different code, or remove '{other}' "
                f"first.")
    codes[key] = code

    cfg = get_usage()
    cfg["signal"] = {"channels": channels, "codes": codes}
    _write_usage(cfg)
    # The channels have to be retained during a capture or there is nothing to
    # decode, and the role keeps them out of the way of the marker tools, which
    # would otherwise report a state bit held high for a minute as one enormous
    # marker assertion.
    for ch in channels:
        set_aux(ch, name=f"state-bit{channels.index(ch)}", primary=False,
                role="state_signal")
    return cfg["signal"]


def clear_state_signal() -> dict:
    """Forget the state encoding, and stop retaining its channels."""
    cfg = get_usage()
    for ch in (cfg.get("signal") or {}).get("channels") or []:
        if aux_channel_cfg(ch).get("role") == "state_signal":
            clear_aux(ch)
    cfg["signal"] = {}
    _write_usage(cfg)
    return {}


def state_for_code(code: int) -> str:
    """Name mapped to a code, or None."""
    for name, c in (get_state_signal().get("codes") or {}).items():
        if c == int(code):
            return name
    return None


# ------------------------------------------------------------------ #
# State marks                                                          #
# ------------------------------------------------------------------ #
# The other way the target can declare its state: a character written to a UART
# wired to D0, which the instrument decodes at 460800 baud and reports at the
# sample it arrived at.  analysis.py holds the encoding and the reasoning about
# why it beats the two-pin code; what belongs here is only the project's
# symbol-to-state map, kept alongside the usage model for the same reason the
# 2-bit codes are -- so that removing a state removes its symbol with it.
#
# D0 CANNOT DO BOTH.  The pin is decoded as a serial stream and sampled as a
# logic level from the same input, so a D0 carrying a state-code bit or a region
# marker is not carrying bytes, and a D0 carrying bytes reads as a square wave
# at the bit rate to anything looking at its level.  Every combination that
# would put the two on the same pin is refused here rather than allowed to
# produce a capture that decodes into plausible nonsense.
def get_state_marks() -> dict:
    """{"symbols": {"standby": "A", ...}, "baud": 460800} or {}."""
    return dict(get_usage().get("marks") or {})


def mark_symbols() -> dict:
    """state name -> the character the firmware sends for it.  May be empty."""
    return dict(get_state_marks().get("symbols") or {})


def _d0_conflict() -> str:
    """Why D0 cannot carry the serial stream right now, or None."""
    sig = get_state_signal()
    if "D0" in (sig.get("channels") or []):
        return (
            "D0 already carries bit 0 of the two-pin state signal, and one pin "
            "cannot be both a logic level and a serial stream. Either drop to "
            "a one-pin signal on D1 alone (p1150_clear_state_signal, then "
            "p1150_set_state_signal(..., channels=['D1'])), or clear the "
            "signal entirely -- marks give thirty states where it gives four, "
            "so keeping both is rarely worth a pin.")
    if aux_channel_cfg("D0").get("role", "marker") == "marker" and \
            "D0" in get_aux().get("channels", {}):
        return (
            "D0 is declared as a region marker (p1150_set_aux), and one pin "
            "cannot be both a logic level and a serial stream. Move the marker "
            "to D1 or A0 -- or drop it, since a mark measures the same region "
            "and names it.")
    return None


def set_state_mark(state: str, symbol: str = None) -> dict:
    """Map one state name to the character the firmware sends for it.

    symbol may be omitted, in which case the next unused letter is assigned.
    Auto-assignment is offered because the letter itself carries no meaning --
    it is a handle the firmware and the report agree on -- and a developer
    naming five states should not have to keep a register of which letters are
    taken.
    """
    key = _key(state)
    conflict = _d0_conflict()
    if conflict:
        raise ValueError(conflict)

    marks = get_state_marks()
    taken = dict(marks.get("symbols") or {})
    if symbol is None:
        symbol = taken.get(key) or next(
            (c for c in MARK_ENTER if c not in taken.values()), None)
        if symbol is None:
            raise ValueError("All 30 mark symbols are in use.")
    symbol = str(symbol)
    if len(symbol) != 1 or symbol not in MARK_EXIT_OF_ENTER:
        raise ValueError(
            f"'{symbol}' cannot open a region. A mark is ONE character: a "
            f"capital letter A-Z, or one of the opening brackets ( [ {{ <. The "
            f"firmware sends that character on the way in and its partner on "
            f"the way out -- the matching lower-case letter, or the closing "
            f"bracket.")
    for other, s in taken.items():
        if s == symbol and other != key:
            raise ValueError(
                f"'{symbol}' is already the mark for '{other}'. Two states "
                f"cannot share one, since the capture could not tell them "
                f"apart. Pick another, or clear '{other}' first.")
    taken[key] = symbol

    cfg = get_usage()
    cfg["marks"] = {"symbols": taken, "baud": D0S_BAUD}
    _write_usage(cfg)
    return cfg["marks"]


def clear_state_marks(state: str = None) -> dict:
    """Forget one state's mark, or the whole scheme."""
    cfg = get_usage()
    marks = dict(cfg.get("marks") or {})
    if state is None:
        cfg["marks"] = {}
    else:
        symbols = dict(marks.get("symbols") or {})
        if symbols.pop(_key(state), None) is None:
            raise ValueError(f"No mark is declared for '{state}'.")
        cfg["marks"] = ({"symbols": symbols, "baud": D0S_BAUD} if symbols
                        else {})
    _write_usage(cfg)
    return cfg["marks"]


def state_for_mark(symbol: str) -> str:
    """Name mapped to a mark symbol, or None."""
    for name, s in mark_symbols().items():
        if s == symbol:
            return name
    return None


def mark_names() -> dict:
    """symbol -> state name, which is the direction a decoded capture needs."""
    return {s: n for n, s in mark_symbols().items()}


def d0s_recorded() -> bool:
    """Whether to keep the serial stream from D0 in a capture.

    On unless D0 has been given another job.  It is kept even when no marks are
    declared, and that is deliberate: the stream is sparse -- a few bytes where
    a level channel is a megabyte a second -- so retaining it costs nothing
    measurable, and it means a developer who wires the UART up and captures
    before declaring anything still has the marks in the run afterwards. The
    alternative is discovering at analysis time that the one capture worth
    having was taken with the stream switched off.
    """
    return "D0" not in get_aux().get("channels", {})


# ------------------------------------------------------------------ #
# Auxiliary marker inputs                                              #
# ------------------------------------------------------------------ #
def _aux_path() -> str:
    return os.path.join(storage.runs_dir(), _AUX_FILE)


def get_aux() -> dict:
    """Full aux configuration: {"channels": {...}, "primary": "D0"}."""
    try:
        with open(_aux_path(), "r") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("channels", {})
    return cfg


def _write_aux(cfg: dict) -> dict:
    with open(_aux_path(), "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def set_aux(channel: str, name: str = None, active_high: bool = True,
            threshold_mv: float = None, hysteresis_mv: float = None,
            primary: bool = True, role: str = "marker") -> dict:
    """Declare what the target drives into one auxiliary input.

    threshold_mv/hysteresis_mv always describe the SIGNAL's high level, never
    the assertion.  active_high then says which side of that means "asserted",
    so an active-low marker on a 3.3 V rail is still threshold_mv=1650 with
    active_high=False.  Keeping the two independent means the threshold does not
    have to be re-derived when the polarity changes.
    """
    channel = (channel or "").upper()
    if channel not in AUX_CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(AUX_CHANNELS)}, "
                         f"got '{channel}'.")
    if channel == "D0" and role == "marker" and mark_symbols():
        raise ValueError(
            "D0 carries the serial mark stream, and one pin cannot be both a "
            "logic level and a serial input -- a marker declared here would "
            "read the bytes as a square wave and the marks would stop "
            "decoding. Put the marker on D1 or A0. Or measure the region with "
            "a mark of its own instead: it needs no pin, no threshold and no "
            "polarity, and it comes out of the capture named.")
    if channel == "A0" and threshold_mv is None:
        raise ValueError(
            "A0 is an analog input, so threshold_mv is required: it is the "
            "millivolt level that separates a low from a high on the signal "
            "the target drives. Half the target's IO voltage is the usual "
            "choice -- 1650 for a 3.3 V GPIO, 900 for a 1.8 V GPIO. A0 accepts "
            "up to 17000 mV, so a 5 V or 12 V signal can be marked directly.")
    if threshold_mv is not None:
        if threshold_mv <= 0:
            raise ValueError("threshold_mv must be greater than zero.")
        limit = AUX_INPUT_MAX_MV.get(channel)
        if limit and threshold_mv > limit:
            raise ValueError(
                f"threshold_mv={threshold_mv:g} is above what {channel} accepts "
                f"({limit:g} mV). A signal that high must not be connected to "
                f"this input at all -- it would damage the P1150. "
                + ("Use A0, which accepts up to 17000 mV."
                   if channel != "A0" else
                   "Divide the signal down before connecting it."))
        if channel != "A0":
            raise ValueError(
                f"{channel} is a logic input and needs no threshold: the driver "
                f"already rescales it to fixed levels (under ~100 mV low, over "
                f"~900 mV high) regardless of whether the target drives 1.2 V "
                f"or 3.3 V. Call p1150_set_aux without threshold_mv.")

    entry = {"active_high": bool(active_high)}
    if role and role != "marker":
        entry["role"] = role
    if name:
        entry["name"] = name
    if threshold_mv is not None:
        entry["threshold_mv"] = float(threshold_mv)
        if hysteresis_mv is None:
            hysteresis_mv = max(AUX_HYST_MIN_MV,
                                AUX_HYST_FRACTION * float(threshold_mv))
        entry["hysteresis_mv"] = float(hysteresis_mv)
    elif hysteresis_mv is not None:
        entry["hysteresis_mv"] = float(hysteresis_mv)

    cfg = get_aux()
    cfg["channels"][channel] = entry
    if primary or not cfg.get("primary"):
        cfg["primary"] = channel
    return _write_aux(cfg)


def clear_aux(channel: str = None) -> dict:
    """Stop recording one aux channel, or all of them."""
    cfg = get_aux()
    if channel is None:
        cfg = {"channels": {}}
    else:
        cfg["channels"].pop((channel or "").upper(), None)
        if cfg.get("primary") not in cfg["channels"]:
            cfg["primary"] = next(iter(cfg["channels"]), None)
    return _write_aux(cfg)


def aux_channels() -> list:
    """Channels to retain during a capture.  Empty means aux is off."""
    return [c for c in AUX_CHANNELS if c in get_aux().get("channels", {})]


def aux_primary() -> str:
    """The channel the marker tools use when none is named, or None.

    A channel carrying the state signal is never chosen.  It is asserted for
    minutes at a time by design, so a marker tool pointed at one would report a
    single assertion covering most of the capture -- technically true, entirely
    useless, and easily mistaken for a marker that got stuck.
    """
    cfg = get_aux()
    channels = cfg.get("channels", {})
    usable = [c for c, e in channels.items() if e.get("role") != "state_signal"]
    primary = cfg.get("primary")
    if primary in usable:
        return primary
    return next(iter(usable), None)


def aux_marker_channels() -> list:
    """Declared channels that carry a region marker rather than the state
    signal."""
    channels = get_aux().get("channels", {})
    return [c for c in AUX_CHANNELS
            if c in channels and channels[c].get("role") != "state_signal"]


def aux_channel_cfg(channel: str) -> dict:
    """Per-channel settings, or an empty dict if that channel is not set up."""
    return dict(get_aux().get("channels", {}).get((channel or "").upper(), {}))
