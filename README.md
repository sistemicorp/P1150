# P1150 Python Driver

A Python class for controlling the P1150 hardware.

The P1150 Driver here is the same one used for the P1150 GUI available at www.sistemi.ca/p1150.


## Installing


Please follow these steps.

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
