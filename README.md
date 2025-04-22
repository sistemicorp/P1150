# P1150 Python Driver

A Python class for controlling the P1150 hardware.

The P1150 Driver here is the same one used for the P1150 GUI available at www.sistemi.ca/p1150.


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
of a minimal program.  This program performs the following tasks,

* Sends a "ping" to the P1150 to determine if the bootloader or the application is running.
* If the bootloader is running, it will load the application.
* Determine if the P1150 has been previously calibrated.
  * if not, start calibration, and wait for it to complete.
* Set VOUT.
* Turn on internal Cal loads in sweep mode.
* Take a single shot acquisition.
* Plot acquisition.
* Close

## P1150 Common API

    
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


> Portions  ©2025 Sistemi Corp - licensed under MIT
> 
> Portions  ©2025 Unit Circle Inc - licensed under Apache 2.0
