# -*- coding: utf-8 -*-
"""
MIT License

Per-project battery settings, persisted next to the stored runs.

Battery capacity is what turns raw current into the numbers a developer
actually reasons about: how long the device lasts, what share of the battery one
wake-up costs, whether a measured charge current is a sensible C rate.  None of
that is derivable from a current waveform alone, and it cannot be guessed from
the code -- so the agent is told to ask for it once, and it is kept here rather
than re-asked every session.
"""
import os
import json

from . import storage

_FILE = "battery.json"


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


def set_battery(capacity_mah: float, chemistry: str = None,
                nominal_mv: int = None) -> dict:
    cfg = {"capacity_mah": float(capacity_mah), "source": "p1150_set_battery"}
    if chemistry:
        cfg["chemistry"] = chemistry
    if nominal_mv:
        cfg["nominal_mv"] = int(nominal_mv)
    with open(_path(), "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def capacity_mah():
    """Configured capacity in mAh, or None."""
    return get().get("capacity_mah") or None
