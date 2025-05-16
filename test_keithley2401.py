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

This script compares the P1150 measured current to a current set on the
Keithley 2401 Source Meter.  Other models within the family are known to work
with this script.

NOTES:
    - pyvisa needs pyserial installed to find COM instruments
    - P1150 Error Percent allows +/- 1uA of tolerance on the measurement.
      For example, if the set current is 20uA, P1150 measurement within 19-21uA
      is considered 0% error.
"""
import sys
import time
import math
from threading import Event
from timeit import default_timer as timer
import pyvisa
import P1150
import matplotlib.pyplot as plt
import logging
logger = logging.getLogger()
FORMAT = "%(asctime)s: %(filename)22s %(funcName)25s %(levelname)-5.5s :%(lineno)4s: %(message)s"
formatter = logging.Formatter(FORMAT)
logger.setLevel(logging.INFO)

# Set ports
LEITHLEY2401_COM_NUM = "4"
P1150_PORT = "COM3"  # use p1150_scan.py to determine this
P1150_ERROR_TOLERANCE_AMPS = 0.000001  # 1uA

PLOT_COLOR_MAP = {
    2000: "#070feb",
    4000: "#bcbee8",
    5000: "#1bba06",
    6000: "#9fdb97",
    8000: "#be09eb",
    10000: "#e4aaf2",
    12000: "#bd0844",
    16000: "#048576",
}

# List of voltages to sweep the TEST_CURRENTS_MA_LIST
# Note that only the integer part is used, two sweeps at each voltage
VOUT_MV_LIST = [2000.1, 2000.2,
                4000.1, 4000.2,
                5000.1, 5000.2,
                6000.1, 6000.2,
                8000.1, 8000.2,
                #10000.1, 10000.2,
                #12000.1, 12000.2,
                #14000.1, 14000.2,
                #16000.1, 16000.2,
                ]

# List of Keithley 2401 set currents.
TEST_CURRENTS_MA_LIST = [-0.00001, -0.000013, -0.00002, -0.00003, -0.00005, -0.00007,
                          -0.0001,  -0.00013,  -0.0002,  -0.0003,  -0.0005,  -0.0007,
                           -0.001,   -0.0013,   -0.002,   -0.003,   -0.005,   -0.007,
                            -0.01,    -0.013,    -0.02,    -0.03,    -0.05,    -0.07,
                             -0.1,     -0.13,     -0.2,     -0.3,     -0.5,     -0.7,
                               -1,]

# Keithley 2401 range values are basically *10 and one decade higher
# Its important to set the Keithley range correctly, else it has significant error
TEST_CURRENTS_RANGE_MA_LIST = [10.0 ** int(math.log10(abs(i))) for i in TEST_CURRENTS_MA_LIST]

DEFAULT_TBASE = P1150.P1150API.TBASE_SPAN_100MS
DEFAULT_TRIG_SRC = P1150.P1150API.TRIG_SRC_NONE
DEFAULT_TRIG_SLOPE = P1150.P1150API.TRIG_SLOPE_RISE
DEFAULT_TRIG_POSITION = P1150.P1150API.TRIG_POS_CENTER
DEFAULT_TRIG_LEVEL = 1
DEFAULT_ACQ_TIMEOUT = 10.0
DEFAULT_ACQ_HOLDOFF = 0.0
DEFAULT_OUTPUT_FILE = "export.csv"

STM32_APP_a43_FILE = "a43_app.signed.ico"  # P1150 normal application
TIME_RECONNECT_AFTER_FWLOAD_S = 5.0

# global context
G = {"p1150": None,
     "cli_cmd": None,
     "data": None,
     "port": P1150_PORT,
     "acq_complete_event": Event(),
}

consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(formatter)
consoleHandler.setLevel(logging.INFO)
logger.addHandler(consoleHandler)
logger.setLevel(logging.INFO)


def _p1150_acqcomplete(data: dict) -> None:
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

    # p1150_acquire_single is waiting for G["acq_complete_event"]


def p1150_acquire_single(timeout: float=DEFAULT_ACQ_TIMEOUT, holdoff: float=DEFAULT_ACQ_HOLDOFF) -> bool:
    G["acq_complete_event"].clear()
    G["data"] = None

    if holdoff > 0.0:
        logger.info(f"starting holdoff for {holdoff} seconds")
        time.sleep(holdoff)
        logger.info(f"holdoff done")

    logger.info("acquisition_start")

    success, response = G["p1150"].acquisition_start(P1150.P1150API.ACQUIRE_MODE_SINGLE)
    if not success:
        logger.error(f"acquisition_start: {response}")
        all_close()
        return False

    time.sleep(0.05)
    start = timer()
    while not G["acq_complete_event"].is_set():
        # wait for callback to populate G["data"]
        time.sleep(0.02)
        logger.info("waiting for acquisition event...")

        if timer() - start > timeout:
            logger.error(f"timeout: {timeout}")
            break

    success, response = G["p1150"].acquisition_stop()
    if not success:
        logger.error(f"acquisition_stop: {response}")
        return False

    logger.info("done")
    return True


def all_close():
    global rm

    logger.info(f"closing pyvisa")
    keithley_2401.write(":OUTP OFF")
    rm.close()

    logger.info(f"closing P1150")
    if G["p1150"]:
        _, _ = G["p1150"].probe(False)
        _, _ = G["p1150"].acquisition_stop()
        G["p1150"].close()

    G["p1150"] = None


if __name__ == '__main__':

    if input(f"Did you remember to set the COM port?? Using {P1150_PORT} right now...").lower() in ["no", "n"]:
        exit(1)

    connect_attempts = 2
    while connect_attempts >= 1:
        # From a cold boot P1150 is running the bootloader and the application FW
        # needs to be downloaded, and then P1150 must self calibrate. This process
        # takes ~15 seconds the first time. Once these steps are done, subsequent
        # connections are fast.

        try:
            G["p1150"] = P1150.P1150(port=P1150_PORT,
                                     logger=logger,
                                     cb_acquisition_get_data=_p1150_acqcomplete)

        except Exception as e:
            logger.info(e)
            exit(1)

        success, p1150_details = G["p1150"].ez_connect()
        if not success:
            logger.error(f"ez_connect {P1150_PORT}: {p1150_details}")
            G["p1150"].close()
            exit(1)

        if p1150_details["app"] == "a43":
            break

        connect_attempts -= 1

    if connect_attempts < 0:
        logger.error(f"ez_connect {P1150_PORT}: exceeded connect attempts")
        exit(1)

    try:
        rm = pyvisa.ResourceManager('@py')
        #logger.info(f"VISA Instruments {rm.list_resources()}")
        keithley_2401 = rm.open_resource(f"ASRL{LEITHLEY2401_COM_NUM}::INSTR",
                                         baud_rate=19200,
                                         data_bits=8,
                                         parity=pyvisa.constants.Parity.none)
        keithley_2401.read_termination = '\n'
        keithley_2401.write_termination = '\n'
        keithley_2401.write("*RST")
        logger.info("Keithley 2401 connection succeed")

    except:
        raise StopIteration("Error. Verify the connection (GPIB,RS232,USB,Ethernet) and its identifier")
        all_close()
        exit(1)

    # see https://github.com/pmasi/Keithley-Python-Interface
    keithley_2401.write(":SOUR:FUNC CURR")
    keithley_2401.write(":SOUR:CURR:RANG 0.001")
    keithley_2401.write(":SOUR:CURR:LEV -0.0001")
    keithley_2401.write(":OUTP ON")

    success, response = G["p1150"].set_timebase(DEFAULT_TBASE)
    if not success:
        logger.error(f"set_tbase  : {response}")
        all_close()
        sys.exit(1)

    trig_src = DEFAULT_TRIG_SRC
    trig_slope = DEFAULT_TRIG_SLOPE
    trig_pos = DEFAULT_TRIG_POSITION
    level = DEFAULT_TRIG_LEVEL
    success, response = G["p1150"].set_trigger(trig_src, trig_pos, trig_slope, level)
    if not success:
        logger.error(f"set_trigger: {response}")
        all_close()
        sys.exit(1)

    success, _ = G["p1150"].set_vout(1000)
    if not success:
        all_close()
        sys.exit(1)

    success, _ = G["p1150"].probe(True)
    if not success:
        all_close()
        sys.exit(1)

    csv_lines = ["vout, i_range, i_set, i_meas, i_err"]

    plt.xlabel("Current (Amps)")
    plt.ylabel("Error (%)")
    plt.title(f"DUT:{p1150_details["serial_hash"]} Percent Error vs Current")
    plt.xscale('log')
    last_range = TEST_CURRENTS_RANGE_MA_LIST[0]
    plt.axvline(x=(last_range - last_range/20.0), color='#37bf53')  # Keithley irange boundaries

    results = {}
    for vout in VOUT_MV_LIST:

        results[vout] = {}

        # put to a low current state while not working
        keithley_2401.write(f":SOUR:CURR:RANG {TEST_CURRENTS_RANGE_MA_LIST[0]}")
        keithley_2401.write(f":SOUR:CURR:LEV {TEST_CURRENTS_MA_LIST[0]}")

        success, _ = G["p1150"].set_vout(int(vout))
        if not success: break
        time.sleep(0.1)  # OUTPUT settle, also "cool off" wrap around (1Amp -> 10uA)

        for (isrc, irange) in zip(TEST_CURRENTS_MA_LIST, TEST_CURRENTS_RANGE_MA_LIST):
            logger.info(f"vout {vout}, current {isrc}, range {irange}")

            if last_range != irange:
                plt.axvline(x=(irange - irange/20.0), color='#37bf53')  # irange boundary
                last_range = irange

            keithley_2401.write(f":SOUR:CURR:RANG {irange}")
            keithley_2401.write(f":SOUR:CURR:LEV {isrc}")
            time.sleep(0.1)  # Keithley settle time, don't use lower than 50ms, 20ms just barely works

            success = p1150_acquire_single(timeout=DEFAULT_ACQ_TIMEOUT, holdoff=DEFAULT_ACQ_HOLDOFF)
            if not success: break

            _avg = sum(G["data"]["i"]) / len(G["data"]["i"]) / 1000.0  # avg and convert to Amps
            _percent_error = round((_avg - abs(isrc)) / abs(isrc) * 100.0, 3)  # note isrc is -ve
            logger.info(f"RESULT: {vout} set {isrc:0.7f}, avg {_avg:0.7f}, error {_percent_error:0.3f}")
            results[vout][abs(isrc)] = _avg

            csv_lines.append(f"{vout},{irange},{isrc:0.7f},{_avg:0.7f},{_percent_error:0.3f}")

    _, _ = G["p1150"].probe(False)

    # put to a low current state while not working
    keithley_2401.write(f":SOUR:CURR:RANG {TEST_CURRENTS_RANGE_MA_LIST[0]}")
    keithley_2401.write(f":SOUR:CURR:LEV {TEST_CURRENTS_MA_LIST[0]}")

    all_close()

    results_err = {"tolp1uA": {}, "tolm1uA": {}}
    for vout, i_values in results.items():
        results_err[vout] = {}
        for isrc, imeas in i_values.items():
            results_err[vout][isrc] = round((imeas - abs(isrc)) / abs(isrc) * 100.0, 3)

            # tolp/m 1uA don't need to be calculated every time but they are... lazy
            results_err["tolp1uA"][isrc] = round(((isrc + 0.000001) - isrc) / isrc * 100.0, 3)
            results_err["tolm1uA"][isrc] = round(((isrc - 0.000001) - isrc) / isrc * 100.0, 3)

        values = [results_err[vout][i] for i in results_err[vout].keys()]
        currents = [float(i) for i in results_err[vout].keys()]
        _color = PLOT_COLOR_MAP.get(int(vout), '#ab830a')
        plt.plot(currents, values, label=f"{vout}", marker='o', color=_color)

    # 1uA tolerance
    values = [results_err["tolp1uA"][i] for i in results_err["tolp1uA"].keys()]
    plt.plot(currents, values, label=f"tolp1uA", marker='', color='#000000')
    values = [results_err["tolm1uA"][i] for i in results_err["tolm1uA"].keys()]
    plt.plot(currents, values, label=f"tolm1uA", marker='', color='#000000')

    plt.ylim(-2, 2)

    plt.axhspan(-0.5, 0.5, facecolor='#ddebb5', alpha=0.2)
    plt.legend(loc='center left', bbox_to_anchor=(1, 0.5))
    plt.grid()
    plt.grid(True, 'minor', color='#ddddee')  # use a lighter color

    _filename = f"K2401_err_DUT_{p1150_details["serial_hash"]}.png"
    plt.savefig(_filename, bbox_inches='tight')
    plt.show()

    _filename = f"K2401_err_DUT_{p1150_details["serial_hash"]}.csv"
    with open(_filename, "w") as f:
        for l in csv_lines:
            f.write(f"{l}\n")
