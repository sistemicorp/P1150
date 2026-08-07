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
#
# The fallback anchors on the project root rather than the working directory.
# Claude Code sets CLAUDE_PROJECT_DIR in the server's environment; a client
# started from a subdirectory would otherwise scatter runs across the disk, and
# put them outside the .p1150_runs/ line in .gitignore that keeps hundreds of
# megabytes of samples out of the repository.
_ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
RUNS_DIR = os.environ.get("P1150_RUNS_DIR") or \
    os.path.join(_ROOT, ".p1150_runs")

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(label: str) -> str:
    return _SAFE.sub("-", (label or "run").strip())[:48] or "run"


def runs_dir() -> str:
    os.makedirs(RUNS_DIR, exist_ok=True)
    return RUNS_DIR


_AUX_PREFIX = "aux_"


def save(label: str, i_ma: np.ndarray, meta: dict,
         isnk_ma: np.ndarray = None, aux: dict = None) -> str:
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
    # Aux channels are stored raw, not as the decoded logic. The threshold that
    # turns millivolts into an assertion is a setting the developer may well get
    # wrong on the first attempt, and keeping the samples means it can be
    # corrected and the run re-analysed instead of re-captured.
    for name, arr in (aux or {}).items():
        if arr is not None:
            arrays[_AUX_PREFIX + name] = arr.astype(np.float32, copy=False)
    # Uncompressed: a several-minute capture is hundreds of MB of noisy float32
    # that barely compresses, and savez_compressed would stall the tool call
    # for many seconds to save little disk.
    np.savez(path, **arrays)
    return run_id


def load(run_id: str):
    """Return (current array in mA, metadata dict) for a stored run."""
    i, _, _, meta = load_all(run_id)
    return i, meta


def load_meta(run_id: str) -> dict:
    """Metadata only, without reading the samples.

    An npz is a zip of independently stored members, so pulling out the metadata
    member costs a few hundred bytes where load_all() would bring a capture of
    hundreds of megabytes into memory.  Anything that scans ACROSS runs -- the
    listing, or finding the newest baseline for a usage state -- has to come
    through here, or answering a question about labels reads the entire store.
    """
    path = os.path.join(runs_dir(), run_id + ".npz")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No run '{run_id}'. Use p1150_list_runs to see stored runs.")
    with np.load(path) as z:
        return json.loads(bytes(z["meta"]).decode()) if "meta" in z else {}


def _run_ids(limit: int = None) -> list:
    """Stored run ids, newest first.  The id begins with a sortable timestamp,
    so the filename order is the chronological order and no file needs opening
    to establish it."""
    d = runs_dir()
    names = sorted((f[:-4] for f in os.listdir(d) if f.endswith(".npz")),
                   reverse=True)
    return names[:limit] if limit else names


def latest_for_state(state: str) -> dict:
    """Metadata of the newest capture measuring a named usage state, or None.

    This is how a battery-life estimate finds its inputs without the agent
    having to remember run ids across sessions: the newest run measuring a state
    wins.  Re-measuring after a firmware change therefore supersedes the old
    baseline automatically, which is what stops a stale sleep figure quietly
    surviving into an estimate made a week later.

    Two kinds of run qualify.  One captured for a single state carries its name
    directly.  One taken while the target signalled its own state carries a
    current for every state it passed through, and any of those counts -- so a
    single sweep of the device doing its real work supplies every baseline at
    once.  A sweep is reported with the state's own current substituted for the
    whole-capture average, since that average is a mix of every state in it.
    """
    want = (state or "").strip().lower()
    if not want:
        return None
    for run_id in _run_ids():
        try:
            meta = load_meta(run_id)
        except Exception:
            continue
        if (meta.get("state") or "").strip().lower() == want:
            return meta
        for name, ma in (meta.get("state_currents") or {}).items():
            if name.strip().lower() == want:
                return dict(meta, state=name, avg_ma=ma,
                            from_state_sweep=True,
                            sweep_time_pct=(meta.get("state_times_pct")
                                            or {}).get(name))
    return None


def load_full(run_id: str):
    """Return (i, isnk, metadata).  isnk is None for runs stored before the
    sink channel was retained, so charging analysis has to check for it."""
    i, isnk, _, meta = load_all(run_id)
    return i, isnk, meta


def load_all(run_id: str):
    """Return (i, isnk, aux, metadata).

    isnk is None, and aux empty, for runs stored before those channels were
    retained -- so anything relying on them has to check rather than assume.
    """
    path = os.path.join(runs_dir(), run_id + ".npz")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No run '{run_id}'. Use p1150_list_runs to see stored runs.")
    with np.load(path) as z:
        meta = json.loads(bytes(z["meta"]).decode()) if "meta" in z else {}
        isnk = z["isnk"] if "isnk" in z.files else None
        aux = {f[len(_AUX_PREFIX):]: z[f]
               for f in z.files if f.startswith(_AUX_PREFIX)}
        return z["i"], isnk, aux, meta


def list_runs(limit: int = 25) -> list:
    out = []
    for run_id in _run_ids(limit):
        try:
            meta = load_meta(run_id)
        except Exception:
            meta = {}
        row = {
            "run_id": run_id,
            "label": meta.get("label"),
            "created": meta.get("created"),
            "duration_s": meta.get("duration_s"),
            "avg_ma": meta.get("avg_ma"),
            "charge_mah": meta.get("charge_mah"),
        }
        # Only present on captures taken as a usage-state baseline, and the
        # reason one run out of a session's dozen is the one an estimate uses.
        if meta.get("state"):
            row["state"] = meta["state"]
            row["baseline_verdict"] = meta.get("baseline_verdict")
        out.append(row)
    return out


def delete(run_id: str) -> bool:
    path = os.path.join(runs_dir(), run_id + ".npz")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False
