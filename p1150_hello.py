# -*- coding: utf-8 -*-
"""
MIT License

Copyright (c) 2025 Sistemi Corp

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

A minimal example program to do a single shot measurement from P1150 using the
internal calibration loads in a sweep pattern.  This example does not engage
the probe.

"""
import time
from threading import Event
from timeit import default_timer as timer
from p1150_driver import P1150
import matplotlib.pyplot as plt
import logging

logger = logging.getLogger()
FORMAT = "%(asctime)s: %(filename)22s %(funcName)25s %(levelname)-5.5s :%(lineno)4s: %(message)s"
formatter = logging.Formatter(FORMAT)
consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(formatter)
consoleHandler.setLevel(logging.INFO)
logger.addHandler(consoleHandler)
logger.setLevel(logging.INFO)

# scanning for P1150 is not shown in this example, please see p1150_scan.py.
P1150_PORT = "/dev/ttyACM2"  # use p1150_scan.py to determine this
DEFAULT_ACQ_TIMEOUT = 10.0
P1150_VOUT_MV = 4000

# global dict for data
G = {"acq_complete_event": Event(),  # indicates when acq is complete
     "data": None,                   # acq data will be stored here
}


def _cb_p1150_acqcomplete(data: dict) -> None:
    """ A callback for P1150 acquisition data
    - acquisition_start is waiting for G["acq_complete_event"]
    - this function needs to copy the data and return ASAP
    - currents 'i', 'isnk' are in mA
    - Aux A0 in milliVolts

    :param data: acq data {'t': [...], 'i': [...], 'a0': [...], 'd0': [...], 'd1': [...], 'isnk': [...]}
    """
    G["data"] = data
    logger.info("Acquisition complete/triggered")
    G["acq_complete_event"].set()


def _cb_p1150_async(data: dict) -> None:
    """ A callback for P1150 asynchronous messages
    - Ammeter message every ~1s
    - Temperature updates
    - this callback is optional

    :param data: dict
    """
    logger.debug(f"p1150 async: {data}")


# Must use __main__ due to multiprocessing within P1150 driver
if __name__ == '__main__':

    if input(f"Did you remember to set the COM port?? Using {P1150_PORT} right now...").lower() in ["no", "n"]:
        exit(1)

    logger.info(f"attempting connect on {P1150_PORT}...")
    connect_attempts = 2
    while connect_attempts >= 1:
        # From a cold boot P1150 is running the bootloader and the application FW
        # needs to be downloaded, and then P1150 must self calibrate. This process
        # takes ~15 seconds the first time. Once these steps are done, subsequent
        # connections are fast.

        try:
            p1150 = P1150.P1150(port=P1150_PORT,
                                logger=logger,
                                cb_uclog_async=_cb_p1150_async,
                                cb_acquisition_get_data=_cb_p1150_acqcomplete)

        except Exception as e:
            logger.info(e)
            exit(1)

        success, p1150_details = p1150.ez_connect()
        if not success:
            logger.error(f"ez_connect {P1150_PORT}: {p1150_details}")
            p1150.close()
            exit(1)

        if p1150_details["app"] == "a43":
            break

        connect_attempts -= 1

    if connect_attempts < 0:
        logger.error(f"ez_connect {P1150_PORT}: exceeded connect attempts")
        exit(1)

    logger.info(f"P1150 connected on {P1150_PORT}")
    logger.info(f"P1150 details {p1150_details}")  # use this response for P1150 details

    # P1150 is now ready to be controlled
    # in this example P1150 will measure its internal Cal resistors sweep

    success, response = p1150.set_vout(P1150_VOUT_MV)
    if not success:
        logger.error(f"{response}")
        p1150.close()
        exit(1)

    # Use demo load
    success, response = p1150.set_cal_sweep(sweep=True)
    if not success:
        logger.error(f"{response}")
        p1150.close()
        exit(1)

    time.sleep(0.02)  # wait for load to settle

    success, response = p1150.acquisition_start(P1150.P1150API.ACQUIRE_MODE_SINGLE)
    if not success:
        logger.error(f"acquisition_start: {response}")
        p1150.close()
        exit(1)

    # Wait for acq to complete, see _cb_p1150_acqcomplete()
    start = timer()
    while not G["acq_complete_event"].is_set():
        # wait for callback to populate G["data"]
        time.sleep(0.02)
        logger.info("waiting for acquisition event...")

        if timer() - start > DEFAULT_ACQ_TIMEOUT:
            logger.error(f"timeout: {DEFAULT_ACQ_TIMEOUT}")
            break

    # Always stop aqc when finished so that streaming stops
    success, response = p1150.acquisition_stop()
    if not success:
        logger.error(f"acquisition_stop: {response}")
        p1150.close()
        exit(1)

    # turn off demo load
    success, response = p1150.set_cal_sweep(sweep=False)
    if not success:
        logger.error(f"{response}")
        p1150.close()
        exit(1)

    # plot the results
    plt.xlabel("Time (s)")
    plt.ylabel("Current (mAmps)")
    plt.title(f"P1150:{p1150_details['serial_hash']} Current vs Time")
    plt.yscale('log')

    plt.plot(G["data"]["t"], G["data"]["i"])

    plt.grid(True, 'both', color='#ddddee', axis="both")  # use a lighter color
    plt.show()

    p1150.close()  # ALWAYS close
    exit(0)

