# P1150 Python Driver

A Python class for controlling the P1150 hardware.

The P1150 Driver here is the same one used for the P1150 GUI available at www.sistemi.ca/p1150.

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


# P1150 Official GUI

The P1150 GUI is built upon these technologies,
* **[dearpygui](https://github.com/hoffstadt/DearPyGui)**
* **[Nuitka](https://nuitka.net/)**

Using the `PXXXX.py` driver you could make your own GUI.  The official GUI uses the same P1150 driver.

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
