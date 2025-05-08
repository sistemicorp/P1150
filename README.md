# P1150 Python Driver

A Python class for controlling the P1150 hardware.

The P1150 Driver here is the same one used for the P1150 GUI available at www.sistemi.ca/p1150.

You should be familiar with the GUI and your DUT current profile before attempting to automate
measurements.


## Installing


P1150 is developed and tested with Python 3.12.  


### Requirements


Install Python requirements,

```commandline
python -m pip install -r requirements.txt
```


### COBS


COBS (Consistent Overhead Byte Stuffing) and is used for the protocol between PC and 
P1150.  COBS is built from c code, as follows,

```commandline
python -m pip install ./cobs
```

## Run "hello, p1150"

In keeping with tradition, a "Hello, World" program, `p1150_hello.py`, is given as an example
of a minimal program.  

*BEFORE* you run `p1150_hello.py`, and with all the examples, you need to set the 
COM port inside the code. The easiest way to find the COM port is to run `p1150_scan.py`.


    >python p1150_scan.py
    P1150      : COM15, serial number FE823374


`p1150_hello.py` performs the following tasks,

* Connects to the P1150, calibrates if this is the first time connecting.
* Set VOUT.
* Turn on internal Cal loads in sweep mode.
* Take a single shot acquisition.
* Plot acquisition.
* Close


## P1150 Common API

All of the P1150 API calls have this form,

```python
    success, response = p1150.set_vout(P1150_VOUT_MV)
    if not success:
        logger.error(f"{response}")
        p1150.close()
```
* `success` (bool) indicates where the function call succeeded or not.
* `response` (dict) contains information.

Appropriate error handling when `success` is False should be implemented. 


    
    ping(self) -> (bool, dict):
    ez_connect(self) -> (bool, dict):
    status(self) -> (bool, dict):

    calibrate(self, force: bool=False, blocking: bool=True) -> (bool, dict):
    cal_status(self) -> (bool, dict):

    set_trigger(self, src: str=P1150API.TRIG_SRC_NONE, pos: str=P1150API.TRIG_POS_LEFT, slope: str=P1150API.TRIG_SLOPE_RISE, level: int=1) -> (bool, dict):
    set_timebase(self, span: str) -> (bool, dict):
    acquisition_start(self, mode: str) -> (bool, dict):
    acquisition_complete(self) -> (bool, dict):
    acquisition_stop(self) -> (bool, dict):
    acquisition_get_data(self) -> (bool, dict):

    set_ovc(self, value_ma: int) -> (bool, dict):
    vout_metrics(self) -> (bool, dict):
    set_vout(self, value_mv: int) -> (bool, dict):
    probe(self, connect: bool=True, hard_connect: bool=False) -> (bool, dict):

    clear_error(self) -> (bool, dict):
    temperature_update(self) -> (bool, dict):
    set_cal_sweep(self, sweep: bool) -> (bool, dict):

    


## Background Information

### COMS Protocol

The protocol over the serial port is an implementation of CBOR/COBS.  The protocol also uses
the ELF file for extracting various variables and strings.

- a51_bl.elf - Bootloader ELF
- a43_app.elf - Application ELF

The COMs protocol is beyond the scope of this driver, you should not need to debug it, or
alter it in any way.

The P1150 stream a lot of data very quickly, on the order of 2500 packets/s.

### P1150 Firmware

The P1150 uses an STM32H750 microcontroller, which has been factory programmed with a bootloader.
The bootloader has the name "a51" (internal Sistemi project number).  The purpose of the bootloader
is to load the "application" FW image (AFI) (project number a43).  The AFI needs to be loaded onto the STM32H750
each time it is powered up or reset.

Within the `assets` folder the hex file of the AFI is called, `a43_app.signed.ico`.

The bootloader will only load signed images for security purposes.

Because the AFI is loaded each time the P1150 is used, the version of the AFI always
matches this repo.

### P1150 Official GUI

The P1150 GUI is built upon these technologies,
* **[dearpygui](https://github.com/hoffstadt/DearPyGui)**
* **[Nuitka](https://nuitka.net/)**

Using the `P1150.py` driver you could make your own GUI.

The biggest hurdle in making a GUI is handling all the data in the plot.  Most plotting
frameworks are limited to a few 100k points.  Whereas with P1150 you will want to plot
millions.


# Example Scripts

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



> Portions  ©2025 Sistemi Corp - licensed under MIT
> 
> Portions  ©2025 Unit Circle Inc - licensed under Apache 2.0
