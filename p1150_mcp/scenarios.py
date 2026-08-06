# -*- coding: utf-8 -*-
"""
MIT License

Measurement know-how, written for an agent to read.

None of this is derivable from the driver signatures, and getting it wrong
produces a capture that looks fine and means nothing -- a 100 ms window that
misses the wake burst entirely, or a 2 s "sleep" measurement taken while the
target was still booting.  Served by the p1150_measurement_guide tool.
"""

GUIDE = """\
# Measuring battery current with a P1150

## Units
Current is milliamps (mA) everywhere.  Charge is milliamp-hours (mAh); for a
single wake-up event, microamp-hours (uAh).  Voltage is millivolts (mV).
Battery capacity is quoted in mAh, so charge is reported the same way -- do not
convert to Coulombs.

## Ask for the battery capacity first
At the start of a project, before measuring anything, ask the developer what
capacity battery (in mAh) the target runs on, and record it with
p1150_set_battery.  It cannot be inferred from a waveform or from the code, and
it is what turns a current reading into something actionable: how long the
device lasts, what share of the battery a single wake-up or boot costs, how many
times an operation can run before the pack is flat, and whether a charging
current is a sensible C rate.  It persists, so it is asked once per project.

## The normal working order
1. p1150_connect(sn)                       -- ~15 s the first time (firmware +
                                              self-calibration), fast after
2. p1150_power_on(voltage_mv, ovc_ma)      -- sets voltage, over-current limit,
                                              then closes the probe relay
3. ... developer edits and re-flashes firmware over JTAG, power stays on ...
4. p1150_measure(...) or capture_start/stop -- as many times as wanted
5. p1150_compare(baseline, candidate)

Leave the power on between measurements.  Cutting it forces the target to
reboot, and a reboot is a large current event that contaminates the next
capture.

## Choosing a voltage
Match what the battery actually delivers, not its nominal label.  A single-cell
Li-ion is 4200 mV full, ~3700 mV nominal, ~3300 mV nearly empty; current draw
differs measurably across that range, so a regression comparison is only valid
when both runs used the same voltage.  Two alkaline cells in series are ~3000 mV
fresh.  A coin cell is ~3000 mV with a high internal resistance the P1150 does
not emulate by default.

Set ovc_ma above the target's true peak, not above its average.  A radio TX or
motor inrush can be 20x the average; an OVC set to the average trips instantly
and the target browns out, which looks like a firmware bug and is not.

## The six profiles worth knowing

### 1. Sleep / quiescent floor
What: the current when the target has nothing to do.  Usually microamps.
How: p1150_measure(duration_s=10..60).  No trigger.  Let the target settle for
     several seconds after boot before starting.
Read: sleep_floor_ma from the summary, and the deep_sleep/sleep buckets from
     p1150_segment.
Wrong looks like: floor sitting in the milliamps.  Sleep was never entered, or a
     peripheral (UART, ADC, sensor, external flash, LED, pull-up on a floating
     pin) is still enabled.  On most targets this is the single largest
     battery-life bug, and it is invisible without a measurement.

### 2. Boot / power-on inrush
What: the surge as the target powers up, plus the whole boot sequence.
How: p1150_measure(duration_s=..., connect_probe_during=True).  This starts
     streaming *before* closing the probe relay, so the target is powered up
     while already being measured.  A normal capture starts after power is
     already applied and misses the entire event.
Read: peak_ma, and total charge_mah for the boot.
Wrong looks like: a peak that trips OVC, or a boot that costs more charge than
     hours of sleep -- which matters a lot for a device that wakes cold often.

### 3. Periodic wake-up (BLE advertising, sensor poll, timer tick)
What: a low floor with regular bursts.  The dominant profile for battery
     devices, and the one where average current alone is misleading.
How: capture at least 10 full periods.  Advertising at 1 s intervals needs
     >= 15 s.  Use p1150_measure(duration_s=15..60), or capture_start /
     capture_stop around a scripted workload.
Read: p1150_events -- gives wake rate, burst duration, charge per wake (uAh),
     and duty cycle.  These are the three independent knobs; average current is
     just their product and cannot tell you which one moved.
Wrong looks like: fewer than ~5 events detected (capture was too short), or
     zero events (threshold sat above the bursts -- pass an explicit
     threshold_ma between the floor and the peak).

### 4. A single event on demand (radio TX, flash write, sensor read)
What: one burst, examined closely.
How: p1150_capture_single(timebase=..., trigger_ma=<between floor and peak>).
     Pick the shortest timebase that still contains the whole event: 10 ms for a
     BLE TX, 100 ms-1 s for a flash erase or a sensor warm-up.  Use
     position="TRIG_POS_CENTER" to see what happened just before the event too.
Wrong looks like: a trigger that never fires (level above the actual peak), or
     an event clipped at the edge of the window (timebase too short).

### 5. Scripted regression run
What: the same fixed workload, before and after a code change.
How: capture_start(label="baseline") -> run the workload -> capture_stop.
     Change code, flash, then repeat with label="candidate".  Keep the duration
     and the workload identical; p1150_compare withholds the total-charge
     comparison when the two durations differ by more than 5%, because
     accumulated mAh over a longer run is trivially larger and means nothing.
Read: p1150_compare(baseline, candidate).

### 6. Charging the battery
What: confirming the target actually charges the pack, and how fast.
Setup: the P1150 stays where the battery is (p1150_power_on as usual), and the
     developer then applies the target's charging source -- USB, wall adapter,
     wireless, solar.  Confirm with them that it is attached and enabled.
How: p1150_verify_charging(duration_s=10..60).
Read: the P1150 reports current flowing back into it as SINK current.  Because
     it sits exactly where the battery would, what it measures is what the
     battery would see: the charger's output minus whatever the rest of the
     target draws at that moment.  A single pair of battery terminals only
     carries that net, so the two cannot be separated -- and do not need to be.
     Charge rate is reported as a C rate: 1C fills the pack in about an hour and
     is what most designs aim for.
Wrong looks like:
     NOT_CHARGING    -- no sink current at all.  Charger not connected or not
                        enabled, charger IC not running, or an open charge path
                        to the battery terminals.
     NET_DISCHARGING -- a charger is delivering, but the rest of the target
                        consumes more than it supplies, so the battery never
                        fills.  Usually means the measurement was taken with the
                        radio or another high-power peripheral active, or the
                        charger's current limit is set below the target's own
                        consumption.  Re-measure with the target idle to
                        separate the two cases.
     INTERMITTENT    -- current flows only part of the time.  The charger is
                        cycling: thermal foldback, an input supply sagging under
                        load, or the charger repeatedly re-qualifying its input.
     A C rate far below the design intent means the charger is programmed low,
     or the target is eating most of what it delivers.

## Reading a regression
p1150_compare reports a verdict against a percentage threshold on average
current, and proposes a likely cause.  The causes map to distinct fixes:

  Sleep floor rose, bursts unchanged
      A peripheral, clock, or regulator was left enabled, or a GPIO is driving
      into something.  Look at what the code newly initialises and never
      de-initialises.

  Bursts got longer, rate unchanged
      More work per wake-up.  Look for added computation, a longer radio
      exchange, retries, or a busy-wait that used to be a sleep.

  Bursts got more frequent, duration unchanged
      A changed interval: timer period, advertising interval, sensor poll rate,
      connection interval.

  Burst peak rose
      Radio TX power, CPU clock speed, or a regulator that switched from a
      low-power mode to a high-power one.

  Everything shifted uniformly, no change in shape
      A constant offset: leakage, a pull-up resistor, an added always-on load,
      or a different board revision.

## Things that will fool you
- Compare like with like.  Same voltage, same duration, same workload, same
  board.  The P1150's own accuracy is far better than the run-to-run variation
  of a target doing slightly different work.
- Take a baseline more than once.  If two back-to-back baselines differ by more
  than the threshold you plan to use, the workload is not deterministic and no
  single comparison will be trustworthy.
- A JTAG debugger attached to the target draws current through the target's
  supply on some boards, and holding a core in debug halt prevents sleep
  entirely.  Detach the debugger for any measurement of sleep current.
- The first few seconds after power-on are boot, not steady state.  Exclude them
  unless boot is what is being measured.
- Averages hide duty cycle.  1 mA average can be 1 mA constant, or 1 uA for
  999 ms and 1 A for 1 ms.  Those need completely different fixes; always look
  at p1150_segment or p1150_events before concluding anything.
"""
