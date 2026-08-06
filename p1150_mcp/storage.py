# -*- coding: utf-8 -*-
"""
MIT License

On-disk store for captured runs.

A capture is millions of samples, far past what can be handed to an agent, so
the samples live in a file and the agent only ever sees a run_id plus a summary.
Runs persist between sessions on purpose: a baseline recorded before a week of
firmware work has to still be there to compare against afterwards.
"""
import os
import json
import re
import datetime as _dt

import numpy as np

# An MCP client config that declares the variable but leaves it blank yields
# "", which os.environ.get would hand back in place of the default -- so treat
# empty as unset throughout.
RUNS_DIR = os.environ.get("P1150_RUNS_DIR") or \
    os.path.join(os.getcwd(), ".p1150_runs")

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(label: str) -> str:
    return _SAFE.sub("-", (label or "run").strip())[:48] or "run"


def runs_dir() -> str:
    os.makedirs(RUNS_DIR, exist_ok=True)
    return RUNS_DIR


def save(label: str, i_ma: np.ndarray, meta: dict,
         isnk_ma: np.ndarray = None) -> str:
    """Store a capture, returning its run_id."""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}_{_slug(label)}"
    path = os.path.join(runs_dir(), run_id + ".npz")
    meta = dict(meta or {})
    meta.update({"run_id": run_id, "label": label,
                 "created": _dt.datetime.now().isoformat(timespec="seconds")})
    arrays = {"i": i_ma.astype(np.float32, copy=False),
              "meta": np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8)}
    if isnk_ma is not None:
        arrays["isnk"] = isnk_ma.astype(np.float32, copy=False)
    # Uncompressed: a several-minute capture is hundreds of MB of noisy float32
    # that barely compresses, and savez_compressed would stall the tool call
    # for many seconds to save little disk.
    np.savez(path, **arrays)
    return run_id


def load(run_id: str):
    """Return (current array in mA, metadata dict) for a stored run."""
    i, _, meta = load_full(run_id)
    return i, meta


def load_full(run_id: str):
    """Return (i, isnk, metadata).  isnk is None for runs stored before the
    sink channel was retained, so charging analysis has to check for it."""
    path = os.path.join(runs_dir(), run_id + ".npz")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No run '{run_id}'. Use p1150_list_runs to see stored runs.")
    with np.load(path) as z:
        meta = json.loads(bytes(z["meta"]).decode()) if "meta" in z else {}
        isnk = z["isnk"] if "isnk" in z.files else None
        return z["i"], isnk, meta


def list_runs(limit: int = 25) -> list:
    d = runs_dir()
    names = sorted((f[:-4] for f in os.listdir(d) if f.endswith(".npz")),
                   reverse=True)[:limit]
    out = []
    for run_id in names:
        try:
            _, meta = load(run_id)
        except Exception:
            meta = {}
        out.append({
            "run_id": run_id,
            "label": meta.get("label"),
            "created": meta.get("created"),
            "duration_s": meta.get("duration_s"),
            "avg_ma": meta.get("avg_ma"),
            "charge_mah": meta.get("charge_mah"),
        })
    return out


def delete(run_id: str) -> bool:
    path = os.path.join(runs_dir(), run_id + ".npz")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False
