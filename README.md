# P1150 Python Driver

A Python class for controlling the P1150 hardware.


## Installing


Please follow these steps.

### Requirements


Install Python requirements,

```commandline
python -m pip install -r requirements.txt
```


### COBS


COBS (Consistent Overhead Byte Stuffing) is built from c code, as follows,

```commandline
python -m pip install ./cobs
```


## Background Information

### P1150 Firmware

The P1150 uses an STM32H750 microcontroller, which has been factory programmed with a bootloader.
The bootloader has the name "a51" (internal Sistemi project number).  The purpose of the bootloader
is to load the "application" FW image (AFI).

- a51_bl.elf - Bootloader ELF
- a43_app.elf - Application ELF
- a43_app.signed.ico - Application hex
