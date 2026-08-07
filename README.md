# P1150 Python Driver

A Python class for controlling the P1150 hardware.

The P1150 Driver here is the same one used for the P1150 GUI, a web application that runs in
the browser at https://sistemicorp.github.io/a73-PxxxxWASMGUI/ -- there is nothing to install.

You should be familiar with the GUI and your DUT current profile before attempting to automate
measurements.

Create a private clone of this repo and add your own automation scripts.  A fork will create a
public repo, and you probably don't want that.


## Installing


P1150 is developed and tested with Python 3.12.

NOTE: https://stackoverflow.com/questions/77364550/attributeerror-module-pkgutil-has-no-attribute-impimporter-did-you-mean


### Requirements


It is recommended (but not required) to install the project into a Python virtual environment so the dependencies
stay isolated from your system Python.

Create and activate a virtual environment:
```commandline
python -m venv .venv
```

Activate it:

**Widows**
```commandline
.venv\Scripts\activate
```

**Linux**
```commandline
source .venv/bin/activate
```


Then install Python requirements.

```commandline
python -m pip install -r requirements.txt
```

The `pxxxx` driver itself is a prebuilt shared library loaded with ctypes, so there is
nothing to compile.  `matplotlib` is the only requirement, and only the plotting examples
need it.

## Run "hello, P1150"

In keeping with tradition, a "Hello, World" program, `p1150_hello.py`, is given as an example
of a minimal program.  Find the serial bumber of your P1150 on the back of the P1150.

```commandline
python p1150_hello.py --sn FE823374
```


`p1150_hello.py` performs the following tasks,

* Connects to the P1150, calibrates if this is the first time connecting.
* Set VOUT.
* Turn on internal Cal loads in sweep mode.
* Take a single shot acquisition.
* Plot acquisition.
* Close

NOTE: The first time P1150 is connected the firmware will be loaded and calibrated. This
can take ~10 seconds.  But this happens only the first time you connect.  Subsequent
connections will be faster.


## Connecting by Serial Number

Every example takes a required `--sn` argument, and identifies the P1150 with it,

```python
port = PXXXX.get_port_from_sn(args.sn)
if port is None:
    ...  # that P1150 is not attached
p1150 = PXXXX(port=port)
```

This is the preferred way to connect, and the only one that behaves predictably when
more than one P1150 is attached.  `PXXXX` does accept a port name (`COM7`,
`/dev/ttyACM0`) directly, but support for connecting that way will be deprecated.


# Support

Send an email to info@sistemi.ca for support.  Include a full log along with a description of the problem.
Confirm that you are using the latest version of the driver.  Report the version printed by
`PXXXX.version()`, which is the version of the shared library in the `pxxxx` folder.


# P1150 Common API

The driver is the `PXXXX` class in the `pxxxx` folder, and the API constants are in
`PxxxxAPI`.

```python
from pxxxx import PXXXX, PxxxxAPI

port = PXXXX.get_port_from_sn("FE823374")
p1150 = PXXXX(port=port, logger=logger, cb_acquisition_get_data=my_callback)
success, details = p1150.ez_connect(calibrate=True)
```

    get_port_from_sn(sn: str) -> str | None:      # static, find the port for a serial number
    list_ports(max_ports: int=16) -> list[str]:   # static, every attached P1150

    ping(self) -> (bool, list[dict]):
    ez_connect(self, calibrate: bool=True, progress_callback=None) -> (bool, dict):
    status(self) -> (bool, list[dict]):

    calibrate(self, force: bool=False, blocking: bool=True) -> (bool, list[dict]):
    cal_status(self) -> (bool, list[dict]):

    set_trigger(self, src: str=PxxxxAPI.TRIG_SRC_NONE, pos: str=PxxxxAPI.TRIG_POS_LEFT, slope: str=PxxxxAPI.TRIG_SLOPE_RISE, level: int=1) -> (bool, None):
    set_timebase(self, span: str) -> (bool, None):
    acquisition_start(self, mode: str) -> (bool, list[dict]):
    acquisition_complete(self) -> (bool, list[dict]):
    acquisition_stop(self) -> (bool, list[dict]):
    acquisition_get_data(self) -> (bool, dict):

    set_ovc(self, value_ma: int) -> (bool, list[dict]):
    vout_metrics(self) -> (bool, list[dict]):
    set_vout(self, value_mv: int) -> (bool, list[dict]):
    probe(self, connect: bool=True, hard_connect: bool=False, rs_comp: bool=False) -> (bool, list[dict]):

    clear_error(self) -> (bool, list[dict]):
    temperature_update(self) -> (bool, list[dict]):
    set_cal_sweep(self, sweep: bool) -> (bool, list[dict]):


## Usage
    

P1150 API calls have this pattern,

```python
    success, response = p1150.set_vout(P1150_VOUT_MV)
    if not success:
        logger.error(f"{response}")
        p1150.close()
```
* `success` (bool) indicates where the function call succeeded or not.
* `response` (dict) contains information.

Appropriate error handling when `success` is False should be implemented. 

Error handling is not implemented in the examples.


# Example Scripts

Every script takes a required `--sn` argument, the serial number on the back of the
P1150.  The Python module `matplotlib` is required for the plotting scripts.


## p1150_scan.py

Reports whether a P1150 is attached and which firmware it is running, without
connecting to it fully.  Useful as a first check, and the only script that leaves the
P1150 in the state it found it.


## p1150_hello.py

A minimum script that enables P1150 Demo mode sweep of internal Calibration resistors and takes a single shot
acquisition and plots the result.


## p1150_hello_probe.py

Extends the `p1150_hello.py` script by connecting the Probe to a target that is assumed to be connected.  This
script does not use the Demo mode sweep.


## p1150_csv.py

Creates a csv file of measurements for a period of time set in the code.  This example only creates the csv
file, and does not plot it.  Use Excel or other tool to plot the results.


## test_keithley2401.py

This script uses an external Keithley 2401 Source Meter controlled with PyVisa to measure the P1150 Error. To
use this script be sure to install `requirements_keithley2401.txt`.


# MCP Server (for AI agents)

`p1150_mcp/` is an [MCP](https://modelcontextprotocol.io) server that lets an AI
coding agent drive a P1150 while you develop firmware: power the target, measure
battery current, and tell you whether a code change made it worse.

```commandline
python -m pip install -r requirements_mcp.txt
```

`.mcp.json` in this repo registers the server with Claude Code, which asks once
to approve it.  That file is a Claude Code convention rather than part of MCP
itself, so Claude Desktop, VS Code and Cursor each want the same command
(`python -m p1150_mcp`) written into their own config instead.

The committed entry runs `.venv/Scripts/python.exe`, so name the virtual
environment `.venv` as above.  On Linux and macOS the interpreter is
`.venv/bin/python` instead.  Register that as a personal entry rather than
editing the tracked file — a local-scope server overrides the project one and is
stored outside the repo, so it survives every `git pull`:

```commandline
claude mcp add --env PYTHONPATH="$PWD" --transport stdio p1150 \
  -- "$PWD/.venv/bin/python" -m p1150_mcp
```

The settings below are declared in `.mcp.json` as `${VAR:-}`, so exporting one
in your shell is enough to reach the server.  Set the serial number for your own
bench that way rather than editing the tracked file.  Optional environment
settings:

| Variable | Purpose |
|---|---|
| `P1150_SN` | Default serial number, so you need not repeat it |
| `P1150_BATTERY_MAH` | Seeds the battery capacity (see below) |
| `P1150_RUNS_DIR` | Where captures are stored (default `.p1150_runs/`) |
| `P1150_MAX_CAPTURE_S` | Cap on a background capture (default 900 s ≈ 900 MB) |

## What it is for

Ask the agent things like:

* *"Power the target at 3700 mV and measure its sleep current."*
* *"Take a baseline, then I'll flash the new build and we'll compare."*
* *"Battery life dropped — find out what changed."*
* *"I've plugged in the USB charger — is it actually charging the battery?"*
* *"Add a marker GPIO around `sensor_read()` and tell me what one call costs."*
* *"This board sometimes won't start on a cold morning — is it inrush?"*
* *"I power-gate the sensor rail — check what that costs when it switches on."*

## Battery settings

The agent is told to ask you, once per project, what capacity battery the target
runs on, and to record it with `p1150_set_battery`.  It persists in
`.p1150_runs/battery.json`, so it is asked once and not again.

It cannot be inferred from a waveform or from the code, and it is what turns a
current reading into something you can act on: projected battery life, the share
of the pack one wake-up or one boot costs, how many times an operation can run
before the battery is flat, and whether a measured charging current is a
sensible C rate.

Three optional settings go in the same place and matter only for the inrush
assessment below: `chemistry`, the cell's internal resistance `esr_mohm` if you
have measured it, and `brownout_mv` — the lowest terminal voltage the target
still runs at, being the regulator's dropout or the MCU's brown-out reset level,
whichever is higher.  Settings merge, so each can be added when it comes up.

A typical session: `p1150_connect` → `p1150_power_on(3700, 500)` → the target
stays powered while you edit and re-flash over JTAG →
`p1150_capture_start("baseline")` … run the workload …  `p1150_capture_stop` →
change code, repeat → `p1150_compare(baseline, candidate)`.

## Why it is not a 1:1 wrapper of the driver

The P1150 streams 125,000 samples/second.  Ten seconds is 1.25 million numbers,
which cannot be handed to a language model.  So captures are written to disk and
every tool returns a summary of at most a few hundred values; the agent passes a
`run_id` around instead of the samples.

For the same reason the tools are task-shaped, not register-shaped.  Sequences
with a mandatory order — vout before probe, timebase before acquisition, stop
before close — are collapsed into a single tool, so the agent chooses *what* to
measure rather than re-deriving the driver's protocol.  The connection is held
open across tool calls so the target stays powered between measurements.

## Tools

**Project** — `p1150_set_battery`, `p1150_get_battery`

**Device** — `p1150_list_devices`, `p1150_connect`, `p1150_disconnect`,
`p1150_status`, `p1150_clear_error`, `p1150_self_test`

**Power** — `p1150_power_on`, `p1150_power_off`

**Capture** — `p1150_measure` (fixed duration), `p1150_capture_start` /
`p1150_capture_status` / `p1150_capture_stop` (open-ended, for the
edit-flash-run loop), `p1150_capture_single` (one triggered event)

**Charging** — `p1150_verify_charging`, `p1150_charge_summary`

**Inrush** — `p1150_inrush_check` (finds surges in any stored run, including the
switched-rail kind that shows up in an ordinary capture), `p1150_inrush_test`
(power-cycles the target to catch the power-up surge specifically)

**Markers** — `p1150_set_aux`, `p1150_get_aux`, `p1150_clear_aux`,
`p1150_aux_check` (wiring verification), `p1150_marker_stats`,
`p1150_compare_marker`

**Analysis** — `p1150_summary`, `p1150_segment` (time and charge per current
band), `p1150_events` (wake-up rate, burst length, charge per wake),
`p1150_compare` (regression verdict plus a likely cause), `p1150_plot`,
`p1150_list_runs`

**Guidance** — `p1150_measurement_guide` returns the measurement know-how the
agent needs: how to choose a voltage and over-current limit, the nine current
profiles worth knowing and what capture length each needs, and the mistakes that
produce measurements which look fine but mean nothing.
`p1150_marker_guide` covers the GPIO-marker workflow below, and
`p1150_inrush_guide` the inrush one.

Charge is reported in mAh (µAh for a single wake-up event), matching how battery
capacity is specified.

## Marking a code region with a GPIO

The P1150's auxiliary inputs are recorded sample-for-sample alongside current:

| Input | Accepts | Reported as |
|---|---|---|
| `D0`, `D1` | 1.2 – 3.3 V logic | rescaled to fixed levels (`<100 mV` low, `>900 mV` high) whatever the target drives — so no threshold is needed |
| `A0` | 0 – 17 V analog | millivolts as measured — needs a `threshold_mv` |

**D0 and D1 tolerate 3.3 V and no more.** A 5 V or 12 V signal goes to A0,
which takes up to 17 V directly and needs no divider.

Wire a spare target GPIO to one of them, raise it around the code you want to
measure, and the boundaries of that region become a fact the firmware states
rather than something inferred from a current threshold:

```c
#define MARK_HIGH()  (GPIOB->BSRR = (1u << 5))         // set PB5
#define MARK_LOW()   (GPIOB->BSRR = (1u << (5 + 16)))  // clear PB5

void sensor_read(void) {
    MARK_HIGH();
    ...                 // the work being measured
    MARK_LOW();
}
```

```
p1150_set_aux(channel="D0", name="sensor_read")   # threshold_mv too, for A0
p1150_aux_check()                                 # confirms the wiring first
p1150_measure(30, "baseline")                     # aux recorded automatically
p1150_marker_stats(run_id)                        # per-call charge and duration
p1150_compare_marker(baseline, candidate)         # did this feature regress?
```

This is what `p1150_events` cannot do.  Threshold-detected bursts only work when
the event stands out above the idle floor, and the detected boundaries shift
between runs; a marker measures quiet work just as well and compares two builds
over provably the same code path.  `p1150_marker_stats` reports duration, charge
and *excess* charge per invocation — excess being the cost above what the target
was drawing anyway, which is the part attributable to the marked code.
`p1150_compare_marker` judges on charge per invocation, so it is unaffected by
how many times the workload ran, and it separates the marked code getting
*slower* from it *drawing more* while it runs — different symptoms, different
fixes.

`p1150_capture_single(trigger_on="D0")` triggers a one-shot capture from the
marker's own edge, which is exact where a current trigger is a guess.

Two things are needed from you and cannot be done from the agent side: the
firmware instrumentation, and a wire from the pin to the aux input with grounds
in common.  `p1150_marker_guide` is written for the agent and covers where to
place the assertions (and where not — nesting, early returns, ISRs), choosing
between A0 and D0/D1 against the target's IO voltage, thresholds, and the
failure modes.

Aux channels cost as much memory per capture as a current channel, so they are
recorded only once `p1150_set_aux` has declared one.  The P1150 samples every
8 µs, so hold a marker for at least ~50 µs; for faster work, mark a loop of N
iterations and divide.

## Inrush current

A P1150 is a low-impedance supply.  Asked for 2 A for a millisecond while a
target's bulk capacitance charges, it delivers 2 A and holds its output voltage —
so the surge shows up on the trace and the target boots perfectly.

A battery cannot do that.  It sags by

    voltage lost = surge current × cell internal resistance

and that sag is what resets the target.  Since a cell's internal resistance is at
its highest when it is aged, cold, and at a low state of charge — three
conditions that stack — a board with an inrush problem passes every bench test on
a fresh cell and then fails on cold mornings, on old units, near the end of the
battery.  Inrush is a common root cause of exactly that class of field return,
and without an instrument at the battery terminals there is nothing to see but an
intermittent reset that will not reproduce indoors.

So the server flags it whether or not anyone asked.  **Every capture is
screened**, whatever it was taken for, and one containing a surge comes back
with a warning attached.

### Two kinds, and they are not equally important

**At power-up** — the surge when the battery is first connected.  In a real
product that happens *once*, on the assembly line: the cell is fitted and stays
there.  So it is usually acceptable, and the analysis ranks it low.  It still
matters for a user-replaceable battery, a pack protection FET that can
re-connect under load, a hot-pluggable rail, or a coin cell — and it matters if
it trips protection.

**While the target is running** — a rail being switched, and the one that hurts.
Power-gating an LDO or SMPS to save current is standard practice in a battery
product; at the instant firmware enables that regulator, the decoupling
capacitance downstream of it is discharged, and a discharged capacitor is a
short circuit.  The current is limited only by resistance in the path.  That
repeats every duty cycle, for the life of the product, at every temperature and
state of charge.

The proper fix for the second is a regulator with **soft-start**, which ramps
its output instead of stepping it.  Parts that have one cost the same as parts
that do not — but it is a schematic decision, so finding this during firmware
development is worth far more than finding it after the boards exist, and
developers usually do not find out until then.

```
p1150_measure(30, "duty-cycle")   # ordinary capture — screened automatically
p1150_inrush_check(run_id)        # full analysis of any stored capture
p1150_inrush_test()               # power-cycles the target, catches power-up
```

For a switched rail, no special capture is needed: record the target doing its
normal work and the screen finds it.  `p1150_inrush_check` then reports how
often it repeats, whether the timing is regular (a timer) or event-driven, what
each switch-on costs, and — usefully — what that adds up to as an average
current, which answers whether the power-gating is saving anything at all.  A
rail cycled 10 times a second whose capacitance takes 0.1 µAh to refill costs
about 3.6 mA on average, which is more than many such rails draw when simply
left on.

`p1150_inrush_test` opens the probe relay, arms a current-triggered one-shot
capture, and only then closes the relay — so the instrument is already waiting
when the surge arrives.  This matters: `p1150_measure(connect_probe_during=True)`
captures the boot sequence but re-arms between chunks, and a millisecond-long
event at the front of it can land in a gap.  A clean result from this test says
nothing about switched rails.

Either way the result reports the peak, its width, what the target settles at
either side of it, and the estimated terminal-voltage sag for a fresh cell, a
part-aged one, and an aged cold one near flat.  With `brownout_mv` set it says
outright whether the target would reset.  The charge in a surge is negligible —
this is a reliability finding, not a battery-life one.

A duty-cycled wake burst is not reported as inrush: a radio waking for 6 ms at
300 mA is a load being driven, not capacitance charging, and the analysis
separates them on width and on how far the peak stands above the settled
current.

**Set the over-current limit high for this.**  A limit below the surge silently
clips it: a 2 A surge measured behind a 500 mA limit records as a 500 mA plateau
and reads as clean.  The analysis lowers its own detection threshold to just
under whatever limit was in force and marks such a peak as a lower bound, but the
real number is not recoverable without re-measuring.  A trip during the test is
not a failed measurement — it is the finding.

`p1150_inrush_guide` is written for the agent and covers the causes, what to ask
you for, how to judge the sag, the fixes in the order they are usually worth
trying (staggering rails in firmware and slew-limiting a load switch come well
before an NTC limiter), and the confounders that make a measured surge smaller
than the real one.

## Verifying charging

`p1150_verify_charging` checks that the target actually charges its battery.
Leave the P1150 connected at the battery terminals as usual, apply the target's
own charging source (USB, wall adapter, wireless, solar), and the P1150 reports
the current flowing back into it as **sink** current.

Because the P1150 sits exactly where the battery would, what it measures *is*
what the battery sees: the charger's output minus whatever the rest of the target
is drawing at that moment.  A single pair of battery terminals only carries that
net, so the two never need separating.

Three outcomes: `CHARGING` (with the C rate and an estimated time to full),
`NET_DISCHARGING` (a charger is present but the target consumes more than it
delivers, so the battery never fills), and `NOT_CHARGING` (no current flows in at
all).  A charger that cycles on and off is reported as `INTERMITTENT`, which an
average alone would hide.

## Safety

`p1150_power_on` puts the requested voltage straight onto the target's battery
terminals, and the agent chooses that value.  Confirm it before the first call
in a session; too high will destroy the target and there is no undo.  Setting
`P1150_SN` does not constrain voltage — nothing does.

Only one program can own a P1150 at a time, so disconnect the P1150 from the web
GUI before using the MCP server, and call `p1150_disconnect` to hand it back.

## Measurement caveats

A JTAG debugger attached to the target draws current through its supply on many
boards, and a halted core cannot enter sleep.  Detach the debugger before
measuring sleep current.

Compare like with like: same voltage, same workload, same duration.  If two
back-to-back baselines differ by more than your threshold, the workload is not
repeatable and no single comparison is trustworthy.

The P1150 is not a battery.  It holds its output voltage where a cell would sag,
so a target that draws a large surge runs perfectly here and browns out in the
field — see the inrush section above.

Captures taken over a timebase longer than 1 s arrive decimated: the acquisition
buffer holds 125,000 samples, which is one second at full rate.  The effective
sample rate is recorded with each run and used when reporting durations, but a
millisecond-scale event cannot be resolved in a 10 s window at all.


# P1150 Official GUI

The official GUI is a web application,

**https://sistemicorp.github.io/a73-PxxxxWASMGUI/**

It runs entirely in the browser -- there is nothing to download, install or update, and the
P1150 is reached from the page itself.  Open the link and connect.

Using the `PXXXX` driver you could make your own GUI.  The official GUI uses the same P1150 driver.

The biggest hurdle in making a GUI is handling all the data in the plot.  Most plotting
frameworks are limited to a few 100k points.  Whereas with P1150 you will want to plot
millions.



## Background Information

### COMS Protocol

The P1150 uses the serial port on the PC.  If you are on Linux, confirm that your user account
has permission to access the serial port.

The P1150 streams a lot of data very quickly, on the order of 2500 packets/s, with an aggregate ~2MB/s.
That may not sound like a lot, but if the PC does not extract the data quickly enough, the P1150
will not be able to buffer all that data.

### P1150 Firmware Loading and Calibration

The P1150 uses an STM32H750 microcontroller, which has been factory programmed with a bootloader.
The bootloader has the name "a51" (internal Sistemi project number).  The purpose of the bootloader
is to load the "application" FW image (AFI) (project number a43).  The AFI needs to be loaded onto the STM32H750
each time it is powered up or reset.

The AFI is embedded in the `pxxxx` shared library, so there is no separate firmware file
to manage; `ez_connect()` loads it automatically when the P1150 answers as "a51".

The bootloader will only load signed images for security purposes.

Because the AFI is loaded each time the P1150 is used, the version of the AFI always
matches this repo.  Loading the AFI takes ~1 second.  

After the AFI is loaded the P1150 will enter calibration, which takes ~10 seconds.  After
alibration the P1150 will be ready to take measurements.  Calibration is only performed once.

### Updating This Repo

The driver is a prebuilt shared library, so a `git pull` is all that is needed to pick up
a new version of it -- there is nothing to uninstall or rebuild.  Check which version you
have with,

```python
from pxxxx import PXXXX
print(PXXXX.version())
```

> Portions  ©2026 Sistemi Corp - licensed under MIT
> 
> Portions  ©2026 Unit Circle Inc - licensed under Apache 2.0
