# -*- coding: utf-8 -*-
"""
MIT License

Per-project settings, persisted next to the stored runs.

Two things belong here, and for the same reason: neither can be derived from a
current waveform or guessed from the code, both are properties of the bench
rather than of any one measurement, and both would otherwise have to be re-asked
every session.

* Battery capacity turns raw current into the numbers a developer actually
  reasons about: how long the device lasts, what share of the battery one
  wake-up costs, whether a measured charge current is a sensible C rate.  The
  cell's chemistry and internal resistance, alongside the voltage the target
  stops working at, turn a measured current surge into the question that
  matters -- would the real battery sag far enough to reset it.

* Auxiliary-input setup describes what the target drives into A0/D0/D1 and what
  counts as "asserted" on it.  A0 is an analog input reported in millivolts, so
  the logic threshold is a decision about the target's IO voltage, not something
  the instrument knows.
"""
import os
import json

from . import storage
from .analysis import AUX_INPUT_MAX_MV

_FILE = "battery.json"
_AUX_FILE = "aux.json"

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
                brownout_mv: int = None) -> dict:
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
    cfg["source"] = "p1150_set_battery"
    with open(_path(), "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def capacity_mah():
    """Configured capacity in mAh, or None."""
    return get().get("capacity_mah") or None


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
            primary: bool = True) -> dict:
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
    """The channel the analysis tools use when none is named, or None."""
    cfg = get_aux()
    return cfg.get("primary") or next(iter(cfg.get("channels", {})), None)


def aux_channel_cfg(channel: str) -> dict:
    """Per-channel settings, or an empty dict if that channel is not set up."""
    return dict(get_aux().get("channels", {}).get((channel or "").upper(), {}))
