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

## Consider asking for a marker GPIO
If the target has a spare GPIO -- and nearly every embedded target does -- one
wire to the P1150's A0, D0 or D1 input, plus two lines of firmware around the
code being measured, turns every question of the form "what does this feature
cost" from an estimate into a measurement.  It is worth raising with the
developer early, before a session is spent inferring boundaries from a current
trace.  p1150_marker_guide has the firmware and wiring detail; profile 5 below
is the short version.

## The seven profiles worth knowing

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

### 5. A specific piece of code, marked by the target itself
What: current over exactly the region a GPIO on the target marks -- one
     function, one driver, one feature -- rather than over whatever the trace
     happens to show.
How: ask the developer for a spare GPIO and a wire to A0, D0 or D1; have the
     firmware raise it at the start of the work and lower it at the end.
     p1150_set_aux declares it, p1150_aux_check confirms the wiring,
     p1150_marker_stats reports per-invocation charge, and
     p1150_compare_marker checks that region alone for regressions.  Read
     p1150_marker_guide first -- it has the firmware and wiring details.
Read: charge_uah and excess_uah per occurrence.  Excess is the cost
     attributable to the marked code; raw charge also contains the idle floor
     the target was drawing anyway.
Why bother: profile 3 infers where an event starts from a current threshold,
     which only works when the work stands out above the floor and shifts a
     little between runs.  A marker states the boundaries, so quiet work is
     measurable too and two builds are compared over provably the same path.
Wrong looks like: zero occurrences -- the code did not run, the lead is on the
     wrong pin, or the polarity is inverted.  p1150_aux_check says which.

### 6. Scripted regression run
What: the same fixed workload, before and after a code change.
How: capture_start(label="baseline") -> run the workload -> capture_stop.
     Change code, flash, then repeat with label="candidate".  Keep the duration
     and the workload identical; p1150_compare withholds the total-charge
     comparison when the two durations differ by more than 5%, because
     accumulated mAh over a longer run is trivially larger and means nothing.
Read: p1150_compare(baseline, candidate).

### 7. Charging the battery
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
- A threshold-detected event is only as good as the threshold.  If the answer
  matters, mark the region with a GPIO instead -- see p1150_marker_guide.
"""


MARKER_GUIDE = """\
# Marking a code region with a GPIO

## What this buys
Current alone shows what the target drew, not which code drew it.  Everything
else in this server infers the boundaries of an event from the current trace --
which works when the work stands out clearly above the idle floor, and stops
working when it does not.  A GPIO raised around the work being measured removes
the inference: the firmware states where the region begins and ends, and the
P1150 records that signal on an auxiliary input sample-for-sample alongside
current.

That gives three things a current threshold cannot:
* Exact charge per invocation, for work of any size -- including work whose
  current draw never rises far above the sleep floor.
* Two builds compared over provably the same code path, instead of over two
  threshold crossings that may not correspond.
* Duration and current separated.  Code that got slower and code that started
  drawing more look identical in average current and need different fixes.

Nearly every embedded target has a pin free for this.  It is worth asking for.

## What the developer has to do
Two things, and both need agreeing before any of the aux tools are useful:

1. Allow a few lines of instrumentation in the firmware (below).
2. Connect a wire from that pin to the P1150's A0, D0 or D1 input, with the
   grounds in common.

Neither can be done from here.  Ask for both explicitly, and confirm the wire is
on before measuring.

## The firmware change
Raise the pin as the last thing before the work, lower it as the first thing
after.  Direct register access, not a HAL call that might block or log:

    // Marker on the pin wired to the P1150 aux input.
    #define MARK_HIGH()   (GPIOB->BSRR = (1u << 5))        // set PB5
    #define MARK_LOW()    (GPIOB->BSRR = (1u << (5 + 16))) // clear PB5

    void sensor_read(void) {
        MARK_HIGH();
        ... the work being measured ...
        MARK_LOW();
    }

Vendor equivalents, all single-cycle and safe in an ISR:
    STM32     GPIOx->BSRR = pin  /  GPIOx->BSRR = pin << 16
    nRF5x     NRF_P0->OUTSET = (1<<n)  /  NRF_P0->OUTCLR = (1<<n)
    ESP32     GPIO.out_w1ts = (1<<n)  /  GPIO.out_w1tc = (1<<n)
    Zephyr    gpio_pin_set_dt(&mark, 1) / 0   (slower; fine for ms-scale work)

Configure the pin as a push-pull output at startup, and set it low there so a
capture that begins mid-boot starts from a known state.

## Where to put the assertions
* Around the whole operation, including any wait it does.  If the code sleeps
  waiting for a sensor, that sleep is part of what the feature costs.
* On every exit path.  An early `return` between MARK_HIGH and MARK_LOW leaves
  the marker stuck high, and the assertion then runs until the next unrelated
  MARK_LOW -- which reads as one enormous invocation rather than as an error.
  A `goto done` / single-exit shape, or a small RAII-style wrapper in C++, is
  worth it here.
* Not around a region that can nest or recurse.  A second MARK_HIGH inside the
  first is invisible, and the pair of MARK_LOWs ends the region early.  For a
  region that nests, use a depth counter and only touch the pin at depth 0 --
  or mark the inner region on the other digital input instead.
* Not inside an interrupt that can pre-empt an already-marked region, unless it
  is on its own pin.  Two unrelated things sharing one marker cannot be told
  apart afterwards.

Two markers are supported at once (D0 and D1, or one of those plus A0), so an
outer and an inner region, or two independent features, can be measured in the
same capture.

## Keep the instrumentation in both builds
Driving a pin costs a small amount of current itself, and the P1150's input
presents a small load.  It is far below anything being measured, but it is not
zero -- so leave the marker code in for BOTH the baseline and the candidate
build, and it cancels exactly.  Removing it for the "clean" run introduces a
difference that has nothing to do with the change under test.

## Choosing the input -- and the voltage limits
                 accepts          reported as
    D0, D1       1.2 - 3.3 V      rescaled to fixed levels: under ~100 mV for a
                                  low, over ~900 mV for a high, whatever the
                                  target actually drives
    A0           0 - 17 V         millivolts as measured

FIRST, CHECK THE TARGET'S IO VOLTAGE against that table.  D0 and D1 tolerate
3.3 V and no more; a 5 V or 12 V signal on either damages the P1150.  A0 takes
up to 17 V, so it is the input for anything above 3.3 V -- and no divider is
needed for a 5 V GPIO, a 12 V rail, or a relay drive.

Otherwise prefer D0/D1.  Because the driver rescales them, a 1.8 V target and a
3.3 V target produce exactly the same trace and neither needs a threshold: the
marker just works.  Reach for A0 when both digital inputs are taken, when the
signal exceeds 3.3 V, or when it is not a clean logic level.

A0 does need a threshold, because it reports what it measures.  Use half the
target's IO voltage:
    5.0 V GPIO -> threshold_mv=2500
    3.3 V GPIO -> threshold_mv=1650
    1.8 V GPIO -> threshold_mv=900
    1.2 V GPIO -> threshold_mv=600
Hysteresis defaults to 10% of the threshold, which suits a short direct lead.
Widen it if p1150_aux_check reports NOISY.

The threshold always describes the SIGNAL, never the assertion.  A marker that
idles high and is pulled low is still threshold_mv=1650 on a 3.3 V rail, with
active_high=False.

A target driving below 1.2 V may not register on D0/D1 at all.  Use A0, whose
threshold can be set anywhere.

## Wiring
* Confirm the target's IO voltage against the table above BEFORE connecting
  anything.  Above 3.3 V it goes to A0, never to D0 or D1.
* One wire from the target GPIO to the aux input, and a common ground.  The
  ground is usually already shared through the P1150's probe at the battery
  terminals -- confirm it, because a marker referenced to a different ground
  reads as noise.
* Keep the lead short.  A long unshielded lead picks up the target's own
  switching and blurs the edges.
* Do not leave the aux input floating when nothing drives it.  A floating input
  reads as NOISY and produces thousands of phantom assertions.

## Timing resolution
The P1150 samples every 8 us.  An assertion needs to be held for at least ~50 us
(several samples) to be measured reliably; one shorter than 8 us can fall
between samples and be missed entirely.  For work faster than that, mark a loop
of N iterations and divide -- the per-invocation cost then comes out of the
arithmetic rather than out of the sampling.

## The working order
1. Agree the pin and the wire with the developer; get the firmware instrumented.
2. p1150_set_aux(channel="D0", name="sensor_read")     -- add threshold_mv for A0
3. p1150_aux_check()  -- with the developer exercising the marked code.  Do not
   skip this.  A marker that never asserts produces a capture that looks
   entirely normal and contains nothing to analyse.
4. Capture as usual: p1150_measure, or capture_start/capture_stop around the
   workload.  The aux channel is recorded automatically once declared.
5. p1150_marker_stats(run_id)     -- per-invocation duration, charge, excess.
6. After a code change, capture again and p1150_compare_marker(baseline,
   candidate).

To look at one invocation in detail rather than the population, use
p1150_capture_single(trigger_on="D0") -- the capture then starts on the marker's
own edge, which is exact where a current trigger is a guess.

## When the marker does not appear
p1150_aux_check names which of these it is:
  STUCK_LOW   The marked code did not run during the check, the GPIO was never
              configured as an output, or the lead is on the wrong pad.  Ask the
              developer to trigger the code and re-check before suspecting the
              wiring.
  STUCK_HIGH  Usually inverted polarity -- re-declare with active_high=False.
              Otherwise a MARK_LOW is being missed on some exit path, or the pin
              is left asserted at the end.
  NOISY       A floating input, a missing common ground, or a threshold sitting
              inside the noise.  Check the ground first, then widen
              hysteresis_mv.
  A0 only     If the configured threshold is not between the two levels the
              signal actually reaches, the check says so and suggests one taken
              from the levels it measured.  That is more reliable than assuming
              the target's IO voltage.

## Reading the result
p1150_marker_stats reports both raw charge per invocation and EXCESS charge.
Excess subtracts what the target was drawing outside the marker, and is the
figure to attribute to the marked code -- raw charge also contains the idle
floor, which makes a long-running marker look expensive when most of that time
the target was doing nothing in particular.

p1150_compare_marker judges on charge per invocation, so it is unaffected by how
many times the workload happened to run.  When it reports a regression, look at
which of duration and current moved: duration means the code got slower, current
means it enables something different while it runs.  It also reports current
outside the marker separately, so an unrelated change to the sleep floor is not
misattributed to the feature.
"""
