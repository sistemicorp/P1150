# P1150

Two things live here, for two audiences:

* `pxxxx/` and the `p1150_*.py` scripts — the ctypes driver for the P1150
  battery-current measurement instrument, plus worked examples.  This is what
  customers of the hardware clone the repo for.
* `p1150_mcp/` — an MCP server that lets an agent drive the instrument while a
  developer works on target firmware.  Registered by `.mcp.json`.

## This drives real hardware and can destroy the user's board

`p1150_power_on` puts the voltage you choose straight onto the target's battery
terminals.  Nothing clamps it — not `P1150_SN`, not the driver.  Too high kills
the target and there is no undo.

**Ask the developer for the voltage and confirm it before the first
`p1150_power_on` of a session.  Never infer it from context, from the firmware
source, or from what a previous project used.**

Only one program can own a P1150 at a time.  The user must disconnect the
instrument in the web GUI before the server can take it, and `p1150_disconnect`
hands it back.  For the same reason, never run `p1150_hello.py` or the other
demo scripts while the MCP server holds the device.

## Read the guide tools before measuring

`p1150_measurement_guide`, `p1150_battery_life_guide`, `p1150_state_mark_guide`,
`p1150_state_signal_guide`, `p1150_inrush_guide` and `p1150_marker_guide` are
written for you rather than for the user.  They carry
the method — window lengths, what a sleep measurement actually requires, how to
judge a voltage sag — none of which is derivable from the tool signatures.
Getting it wrong produces a capture that looks fine and means nothing: a 100 ms
window that misses the wake burst, or a "sleep" figure taken while the target was
still booting.  Call the relevant guide before the first measurement of a kind,
not after a result looks strange.

`p1150_start` reports where a project has got to and what to do next; prefer it
to answering an open question about the instrument from general knowledge.

Ask for the battery capacity once per project and record it with
`p1150_set_battery`.  It cannot be inferred from a waveform, it is what turns
milliamps into battery life, and it persists in `.p1150_runs/battery.json`, so
it is asked once and not re-asked each session.

Battery life needs a second thing that cannot be measured: how the product
divides its time between its states.  That comes from the developer, persists in
`.p1150_runs/usage.json` via `p1150_set_usage_state`, and is what lets a week be
predicted from a minute — `p1150_battery_life_guide` has the method.  No single
capture is a battery life, and the per-capture
`projected_days_if_continuous` is named for its assumption for that reason.

When you are also writing the target's firmware, have it declare its own state.
It turns "the target was in standby while this was measured" from something a
person asserts into a fact recorded beside the current, and one capture then
yields every state's current at once.  Raise it before spending a session
staging baselines by hand.  Two mechanisms, and the choice is whether the MCU
has a UART to spare:

* `p1150_state_mark_guide` — one character written to a UART wired to D0, at
  **460800 baud and no other rate**, upper case entering a region and lower case
  leaving it.  Prefer this one.  Thirty regions rather than four, and they nest,
  so what a feature costs can be separated from the state it ran inside.
* `p1150_state_signal_guide` — a 2-bit code on two spare GPIOs into D0/D1, for a
  target with no UART free or whose deepest sleep gates the UART's clock off.

D0 carries one or the other, never both — it is decoded as a serial stream and
sampled as a level from the same input, and `config.py` refuses every
combination that would put a logic signal and the marks on that pin together.

The serial stream is recorded in every capture whenever D0 is free, even before
any mark is named: it is stored as (sample index, byte) pairs, so it costs
kilobytes where a level channel costs a megabyte a second, and a run taken
before the states were declared still splits afterwards.

`p1150_battery_pie` is what turns any of this into the sentence a developer
acts on — "standby is 71% of the battery".  Prefer it to reciting contributor
figures.

## Captures are large

Sampling is 125 kSa/s per channel — about a megabyte per second, so the 900 s
default cap is roughly 900 MB (`device.py:80`).  Captures are written to
`.p1150_runs/`, which is gitignored.  Never commit one, and never read a `.npz`
from there directly; that is what `p1150_summary`, `p1150_segment`,
`p1150_events` and `p1150_compare` are for.  The samples stay on disk precisely
so they never have to pass through a context window.

## Environment

The virtual environment is `.venv/` at the repo root, and `.mcp.json` runs
`.venv/Scripts/python.exe`.  On Linux and macOS the path is `.venv/bin/python`
— register a local-scope server rather than editing the tracked file; the
README gives the command.

Three requirement sets, split because most users need only the first:

| File | For |
|---|---|
| `requirements.txt` | driver and demo plotting |
| `requirements_mcp.txt` | the MCP server |
| `requirements_keithley2401.txt` | calibration harness, needs a bench SMU |

Nothing is version-pinned, and `server.py` deliberately accepts both `mcp` 1.x
and 2.0 — keep that fallback when touching the import.

The driver is a prebuilt shared library loaded with ctypes (`pxxxx/pxxxx.dll`,
`pxxxx/libpxxxx.so`).  There is nothing to compile.  The `*.so` line in
`.gitignore` carries a deliberate `!pxxxx/libpxxxx.so` exception — do not drop
it, or Linux customers get a repo with no driver in it.

**The binaries stay in git here, and that is the deliberate exception.** The
other two consumers of this driver (`a53-P1150DLL`, `a73-PxxxxWASMGUI`) untrack
theirs and fetch from an `a72-PxDLL` release at build time.  They can: they are
private and have CI.  This repository is **public** and its README tells
customers to clone it, while `a72-PxDLL` is **private** — a customer cannot
fetch its release assets.  Untracking `pxxxx/` here would break every clone.
Do not "finish the job" by applying the other repos' rule to this one.

What was wrong was never the tracking; it was the drift.  Measured 2026-09-03,
`libpxxxx.so` was at a72 `0.1-40` and `pxxxx.dll` at `0.1-41` **in the same
directory**, about twenty commits behind, and `PXXXX.py` differed from a72's by
seven lines — so a P1150 user on Linux and one on Windows were running
different drivers.  So how they arrive is now fixed instead:

```bash
tools/sync_from_a72.sh 0.3        # or no argument: the tag in PXXXX_VERSION
```

It pulls the `linux-x64` **and** `windows-x64` legs of one a72 release, refuses
to write anything unless both report the same commit and the same firmware set,
writes `PXXXX_VERSION` and `pxxxx/manifest.json`, and leaves the result staged
for one commit.  Both platforms move together or neither does.  `PXXXX.py` comes
from the bundle too — stop hand-maintaining it.

Read the header comment before editing it; in particular, each file has a
designated leg because the two legs' shared text files differ by line endings
(the signing box checks out CRLF), so taking a text file from whichever leg was
read last would flip a thousand lines of `PXXXX.py` on alternate syncs.

## Conventions

Every file opens with the MIT header block.  Keep it.

Comments here explain *why*, at length, and that is the house style — see
`storage.py` on why runs persist between sessions, or `device.py` on why one
connection is held open for the life of the process.  Match it when editing.  A
comment restating what the line does is worse than no comment.

The tool docstrings in `server.py` are the agent-facing documentation and are
written to be read mid-task, not skimmed once.  When you change what a tool
does, change its docstring in the same edit.

## There is no test suite, and most of this needs the bench

There is no `tests/` directory.  `test_keithley2401.py` is a calibration
procedure requiring a Keithley 2401 SMU, not a unit test.

Without a P1150 attached you can verify that the modules import and that
`python -m p1150_mcp` answers an MCP `initialize` and lists its tools.  Anything
that touches `SESSION` — connect, power, measure, capture — needs the
instrument.  Report that work as untested rather than as verified.
