# -*- coding: utf-8 -*-
"""
MIT License

Copyright (c) 2024-2025 sistemicorp

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

This file should NOT BE ALTERED.
"""
import os
import struct
from dataclasses import dataclass
from threading import Lock, Event
import numpy as np
from . import uclog
import traceback
import cbor2
import serial
import serial.tools.list_ports
import hashlib
from time import sleep
from timeit import default_timer as timer


class StubLogger(object):
    """ stub out logger if none is provided"""
    def info(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass
    def debug(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def critical(self, *args, **kwargs): pass


@dataclass(frozen=True)
class P1150API:
    """ P1150 Constants

    """

    ACQUIRE_MODE_RUN = "ACQUIRE_MODE_RUN"
    ACQUIRE_MODE_SINGLE = "ACQUIRE_MODE_SINGLE"
    ACQUIRE_MODE_LOGGER = "ACQUIRE_MODE_LOGGER"
    ACQUIRE_MODE_LIST = [
        ACQUIRE_MODE_RUN,
        ACQUIRE_MODE_SINGLE,
        ACQUIRE_MODE_LOGGER
    ]

    TRIG_SRC_NONE = "TRIG_SRC_NONE"
    TRIG_SRC_CUR  = "TRIG_SRC_CUR"
    TRIG_SRC_D0   = "TRIG_SRC_D0"
    TRIG_SRC_D0S  = "TRIG_SRC_D0S"
    TRIG_SRC_D1   = "TRIG_SRC_D1"
    TRIG_SRC_A0A  = "TRIG_SRC_A0A"
    TRIG_SRC_LIST = [
        TRIG_SRC_NONE,
        TRIG_SRC_CUR,
        TRIG_SRC_D0,
        TRIG_SRC_D0S,
        TRIG_SRC_D1,
        TRIG_SRC_A0A,
    ]

    TRIG_POS_CENTER = "TRIG_POS_CENTER"
    TRIG_POS_LEFT   = "TRIG_POS_LEFT"
    TRIG_POS_RIGHT  = "TRIG_POS_RIGHT"
    TRIG_POS_LIST = [
        TRIG_POS_CENTER,
        TRIG_POS_LEFT,
        TRIG_POS_RIGHT,
    ]

    TRIG_SLOPE_RISE = "TRIG_SLOPE_RISE"
    TRIG_SLOPE_FALL = "TRIG_SLOPE_FALL"
    TRIG_SLOPE_EITHER = "TRIG_SLOPE_EITHER"
    TRIG_SLOPE_LIST = [
        TRIG_SLOPE_RISE,
        TRIG_SLOPE_FALL,
        TRIG_SLOPE_EITHER,
    ]

    TBASE_SPAN_10MS  = "TBASE_SPAN_10MS"
    TBASE_SPAN_20MS  = "TBASE_SPAN_20MS"
    TBASE_SPAN_50MS  = "TBASE_SPAN_50MS"
    TBASE_SPAN_100MS = "TBASE_SPAN_100MS"
    TBASE_SPAN_200MS = "TBASE_SPAN_200MS"
    TBASE_SPAN_500MS = "TBASE_SPAN_500MS"
    TBASE_SPAN_1S    = "TBASE_SPAN_1S"
    TBASE_SPAN_2S    = "TBASE_SPAN_2S"
    TBASE_SPAN_5S    = "TBASE_SPAN_5S"
    TBASE_SPAN_10S   = "TBASE_SPAN_10S"
    TBASE_SPAN_LIST = [
        TBASE_SPAN_10MS,
        TBASE_SPAN_20MS,
        TBASE_SPAN_50MS,
        TBASE_SPAN_100MS,
        TBASE_SPAN_200MS,
        TBASE_SPAN_500MS,
        TBASE_SPAN_1S,
        TBASE_SPAN_2S,
        TBASE_SPAN_5S,
        TBASE_SPAN_10S,
    ]

    DEMO_CAL_LOAD_NONE = "DEMO_CAL_LOAD_NONE"
    DEMO_CAL_LOAD_2M   = "DEMO_CAL_LOAD_2M_"
    DEMO_CAL_LOAD_200K = "DEMO_CAL_LOAD_200K_"
    DEMO_CAL_LOAD_20K  = "DEMO_CAL_LOAD_20K_"
    DEMO_CAL_LOAD_2K   = "DEMO_CAL_LOAD_2K_"
    DEMO_CAL_LOAD_LIST = [
        DEMO_CAL_LOAD_NONE,
        DEMO_CAL_LOAD_2M,
        DEMO_CAL_LOAD_200K,
        DEMO_CAL_LOAD_20K,
        DEMO_CAL_LOAD_2K,
    ]

    AUX_D01_DISABLE = 0
    AUX_D01_ENABLE = 1

    ERROR_NONE = 0
    ERROR_I2C = (1 << 0)
    ERROR_HAL = (1 << 1)
    ERROR_INIT = (1 << 2)
    ERROR_INIT_TMP = (1 << 3)
    ERROR_INIT_VMAIN = (1 << 4)
    ERROR_INIT_ADC = (1 << 5)
    ERROR_INIT_USBPD = (1 << 6)
    ERROR_TEMPERATURE = (1 << 8)  # thermal fault
    ## = (1 << 7)   reserved
    ERROR_VOUT_FAILURE = (1 << 9)
    ERROR_CAL = (1 << 10)
    ERROR_PROBE_CON = (1 << 11)
    ERROR_SRC_CURRENT = (1 << 12)
    ERROR_SNK_CURRENT = (1 << 13)

    ERROR_ACT_DISCONNECT = (1 << 0)
    ERROR_ACT_RESET = (1 << 1)
    ERROR_ACT_LOCKOUT = (1 << 2)
    ERROR_ACT_SENDLOG = (1 << 3)


class UCLogger(object):
    """ ucLogger Instance
    - handles communications

    """
    TIMEOUT_CMD = 0.02

    def __init__(self, port="COM1",
                 cb_uclog_log = None,
                 cb_uclog_plot = None,
                 cb_uclog_async = None,
                 logger=StubLogger(),
                 **kw):
        super(UCLogger, self).__init__()

        self._port = port
        self.connected = False
        self.logger = logger
        self._ucLogServer = None
        self._cb_uclog_log = cb_uclog_log
        self._cb_uclog_plot = cb_uclog_plot
        self._cb_uclog_async = cb_uclog_async
        self._cb_uclog_adc = None
        self._lock_responses = Lock()
        self._cmd_responses = {}
        self._result_event = Event()
        self._adc_frame_count = None
        self._low_pass_filter = False
        self._low_pass_filter_i_cache = [0.0, 0.0]
        self._low_pass_filter_isnk_cache = [0.0, 0.0]

        try:
            _t = self._port
            driver_dir = os.path.dirname(__file__)

            app = kw.get('app', "a43")
            if app == "a43":
                elf_file = os.path.join(driver_dir, "firmware", "a43_app.logdata")

            elif app == "a57":
                elf_file = os.path.join(driver_dir, "firmware", "a57_app.logdata")

            else:
                raise ValueError("app must be a43, a57")

            self.logger.info(f"{app}: {elf_file}")

            bl_elf_file = os.path.join(driver_dir, "firmware", "a51_bl.logdata")
            self.logger.info(bl_elf_file)

            _e = uclog.decoders([elf_file, bl_elf_file])
            self.logger.info(f"Using target: {_t}, elf {_e}")

            self._ucLogServer = uclog.LogClientServer(_t,
                                                      _e,
                                                      {'log': self._uclog_log, # log messages
                                                       0: self._uclog_cmdres,  # cmd/response
                                                       1: self._uclog_async,   # asynchronous messages
                                                       2: self._uclog_plot,    # debug plotter
                                                       3: self._uclog_adc})    # adc stream

            self.connected = True

        except serial.SerialException as e:
            self.connected = False
            self.logger.error(e)

        except Exception as e:
            self.connected = False
            self.logger.error(e)
            traceback.print_exc()
            if self._ucLogServer:
                self._ucLogServer.shutdown()
            self._ucLogServer = None

    def is_connected(self):
        return self.connected

    def _uclog_plot(self, item):
        """ ucLogger Plot stream default handler

        """
        self.logger.info(item)
        if self._cb_uclog_plot is None:
            pass
        else:
            self._cb_uclog_plot(item)

    def _uclog_log(self, item):
        """ ucLogger log stream default handler
        - if ucLogger GUI, then usually set to an instance of UnitLogger:Logger
        - else use logger
        """
        if self._cb_uclog_log:
            # TODO: although it is cool to put log items in another tab,
            #       its actually better to see these in the main log
            #       Remove that tab?  Eventually the tab will be removed for release?
            self._cb_uclog_log(item)

        try:
            _, _, lvl, file, line, msg = item
            if lvl == "ERROR":
                self.logger.error(f"== {lvl:5}:{file:30}:{line:4}: {msg}")
            else:
                self.logger.info(f"== {lvl:5}:{file:30}:{line:4}: {msg}")

        except Exception as e:
            self.logger.error(e)
            self.logger.error(item)

    def _uclog_async(self, _item):
        """ Response handler to Port 1 destined for the GUI (not this Klass)
        - these are asynchronous messages from the target
        - these are generally in the form of { f: asc_*, s: true, ... }

        - this gets pushed onto the thread queue of GUI client
        """
        #self.logger.info(_item)
        item = cbor2.loads(_item)
        #self.logger.info(item)

        with self._lock_responses:
            # calls core_klass.py:Core._asc_handler(item)
            if self._cb_uclog_async:
                self._cb_uclog_async(item)

    def _uclog_adc(self, _item):
        """ ADC stream handler
        - ucLog port 3
        """
        #self.logger.debug(_item)

        #if len(_item) != 660 or 662:
        #    self.logger.error(f"invalid adc frame length {len(_item)}")
        #    return

        if self._cb_uclog_adc is None: return

        #print(len(_item))
        try:
            item = cbor2.loads(_item)

            # Convert the list of bytes back to int32 and scale to mAmps (float) using numpy
            item["i"] = np.frombuffer(item["i"], dtype='<f4').copy()
            item["i"] = np.round(item["i"] / 1000000.0, 6)

            # Convert the list of bytes back to int32 and scale to mAmps (float) using numpy
            item["isnk"] = np.frombuffer(item["isnk"], dtype='<f4').copy()
            item["isnk"] = np.round(item["isnk"] / 1000000.0, 6)

            # Convert the list of bytes back to uint16 using numpy
            item["a0"] = np.frombuffer(item["a0"], dtype='<u2').copy().astype(float)
            item["a0"] = np.round(item["a0"], 0)

            # Convert the list of bytes back to uint8 using numpy
            item["d01"] = np.frombuffer(item["d01"], dtype='u1').copy()

            # Convert the list of bytes back to char and then to string list
            # Note: numpy isn't significantly faster for string/object conversions, but we keep it consistent
            item["d0s"] = struct.unpack('<cccccccccccccccccccccccccccccccccccccccccccccccccc', item["d0s"])
            item["d0s"] = [i.decode('utf-8') for i in item["d0s"]]

            # check frame counter, to detect missing packets
            if self._adc_frame_count is None:
                self._adc_frame_count = item['c']
            elif item['c'] != self._adc_frame_count + 1:
                self.logger.error(f"frame count error at {self._adc_frame_count}")

        except Exception as e:
            self.logger.error(e)
            self.logger.error(f"last good frame {self._adc_frame_count}")
            self.logger.error(_item)
            return

        if self._low_pass_filter:
            def lpf(x: np.ndarray, z: list) -> tuple[np.ndarray, list]:
                # Initialize cache if first run
                if z[0] == 0.0:
                    z[0], z[1] = x[0], x[1]

                # Concatenate cache with new data
                # t becomes [z0, z1, x0, x1, ... x49]
                t = np.concatenate((z, x))

                # Weights for the moving average
                w = np.array([0.11, 0.78, 0.11])

                # Use numpy's valid convolution to process the batch
                # This yields len(x) samples because we added 2 from cache
                r = np.convolve(t, w, mode='valid')
                r = np.round(r, 6)

                # Return processed array and update cache with last 2 samples of original x
                return r, x[-2:].tolist()

            item["i"], self._low_pass_filter_i_cache = \
                lpf(item["i"], self._low_pass_filter_i_cache)
            item["isnk"], self._low_pass_filter_isnk_cache = \
                lpf(item["isnk"], self._low_pass_filter_isnk_cache)

        self._adc_frame_count = item['c']
        #if self._adc_frame_count % 5000 == 0:
        #    self.logger.info(f"frame count {self._adc_frame_count}")  # i[0] {item['i'][0]}")
        ra = item['a']
        if ra > 32768:
            self.logger.warning(f">>>>>>>>>>>>> {ra}")
        elif ra > 4096:
            self.logger.info(f">>>>>>>>>>>>> {ra}")

        self._cb_uclog_adc(item)  # this calls P1125:adc_stream_in

    def _uclog_cmdres(self, _item):
        """ Command Response handler on Port 0 default handler
        - works with uclog_response()
        - received responses from target on port 0 end up here
        - cache the response and set _result_event to unblock uclog_response()

        - these are generally in the form of { f: cmd_*, s: true, ... }
        - responses MUSt have fields "f" (function/method) and "s" (success flag)
        """
        # self.logger.debug(_item)
        item = cbor2.loads(_item)
        if "f" not in item:
            self.logger.error(item)
            return

        if item["f"] != "cmd_adc":
            self.logger.info(item)

        with self._lock_responses:
            f = item["f"]
            # logger.info(f"GOT: {item}")
            if f in self._cmd_responses:
                self._cmd_responses[f].append(item)
                self._result_event.set()

            else:
                self._cmd_responses[f] = []
                self._cmd_responses[f].append(item)
                self.logger.warning(f"unexpected response {self._cmd_responses[f]}")

    def uclog_response(self, payload: dict) -> tuple[bool, list[dict] | None]:
        """ helper to send cbor commands and wait for response
        - all commands sent on ucLog port 0 must respond and NOT BLOCK on STM32
        - companion helper _uclog_cmdres gets the response and sets _result_event
        - this is a BLOCKING call to the GUI, because of that, there is only one
          outstanding response, although the design of this (could) supports multiple
          outstanding responses from multiple commands...

        :param payload: { "f": <"cmd_*">, ["argX": <valueX>}], ...}
        :return: success, result/error
                   where, success = True/False
        """
        # logger.info(f"SEND: {payload}")
        f = payload["f"]
        with self._lock_responses:
            # remove any previous responses
            # expect only one response, but we collect all
            self._cmd_responses[f] = []

            try:
                # clear response event, wait below, set in _uclog_cmdres
                self._result_event.clear()
                # send payload to the target
                self._ucLogServer[0](cbor2.dumps(payload))

            except Exception as e:
                self.logger.error(e)
                return False, None

        # wait for the target to send response back
        retries = 8
        while True:

            retries -= 1
            if 1 <= retries < 4:
                self.logger.warning(f"""{f} timeout, retries {retries}, responses {self._cmd_responses}""")

            elif retries == 0:
                self.logger.error(f"""{f} timeout, retries {retries}, responses {self._cmd_responses}""")
                return False, None

            got = self._result_event.wait(timeout=0.10)
            if not got: continue

            with self._lock_responses:
                if f in self._cmd_responses and len(self._cmd_responses[f]):
                    # success is the last command response
                    success = self._cmd_responses[f][-1]["s"]
                    resp = self._cmd_responses.pop(f)
                    #self.logger.info(f'RESP: {resp}')
                    return success, resp

    def uclog_close(self):
        with self._lock_responses:
            if self._ucLogServer:
                self._ucLogServer.shutdown()
                self._ucLogServer = None
            self._cb_uclog_log = None
            self._cb_uclog_plot = None
            self._cb_uclog_cmdres = None
            self._cb_uclog_async = None
        self.logger.info("closed")


class P1150(UCLogger):
    """ P1150 Class

    Notes:
    - All commands are blocking
    - All commands send back a success flag, if False, client must handle error.
    - AI was used to convert the original code to use numpy arrays and take advantage
      of any fast APIs via numpy

    """
    # Fake Voltages for D0/1 signals to be plotted, these are safe to change
    D0_VLOW = 20.0
    D1_VLOW = 40.0
    D0_VHIGH = 1000.0
    D1_VHIGH = 1100.0

    # Do not change any of these next constants
    TIME_RECONNECT_AFTER_FWLOAD_S = 5.0

    DELAY_WAIT_CALIBRATION_START_S = 15
    DELAY_WAIT_CALIBRATION_POLL_S = 1
    RETRIES_CALIBRATION_POLL = 60

    RETRIES_ACQUISITION_COMPLETE = 10
    DELAY_WAIT_ACQUISITION_POLL_S = 0.01

    ADC_SAMPLE_RATE = 125000.0

    TBASE_MAP = {
        P1150API.TBASE_SPAN_10MS: 0.010,
        P1150API.TBASE_SPAN_20MS: 0.020,
        P1150API.TBASE_SPAN_50MS: 0.050,
        P1150API.TBASE_SPAN_100MS: 0.100,
        P1150API.TBASE_SPAN_200MS: 0.200,
        P1150API.TBASE_SPAN_500MS: 0.500,
        P1150API.TBASE_SPAN_1S: 1.0,
        P1150API.TBASE_SPAN_2S: 2.0,
        P1150API.TBASE_SPAN_5S: 5.0,
        P1150API.TBASE_SPAN_10S: 10.0,
    }

    EVENT_SHUTDOWN = "EVENT_SHUTDOWN"

    def __init__(self, cb_acquisition_get_data=None, **kw):
        """ Init

        args:
        : param  port="COM1"
        : param  cb_uclog_log = None,
        : param  cb_uclog_cmdres = None,
        : param  cb_uclog_plot = None,
        : param  cb_uclog_async = None,
        : param  cb_uclog_adc = None,
        : param  logger

        """
        super(P1150, self).__init__(**kw)
        self._lock = Lock()
        self._lock_stream = Lock()
        self._hwver = None

        self._cb_uclog_adc = self.adc_stream_in
        self.cb_acquisition_get_data = cb_acquisition_get_data

        self._buffered_adc_frame_count = None
        self._acquire = False
        self._acquire_mode = P1150API.ACQUIRE_MODE_RUN
        self._acquire_triggered = Event()
        self._acquire_datardy = Event()
        self._trigger_level = 1
        self._trigger_pos = P1150API.TRIG_POS_CENTER
        self._trigger_slope = P1150API.TRIG_SLOPE_RISE
        self._trigger_src = P1150API.TRIG_SRC_NONE
        self._trigger_idx = 0
        self._trigger_idx_precond = False
        self._mahr_stop_time_s = 60
        self._timebase_span = self.TBASE_MAP[P1150API.TBASE_SPAN_100MS]
        self._timebase_t_recalc = True
        self._osc_t = []
        self.NUM_SAMPLES = 0

        self._trig_src_map = {
            P1150API.TRIG_SRC_CUR: "i",
            P1150API.TRIG_SRC_A0A: "a0",
            P1150API.TRIG_SRC_D0:  "d0",
            P1150API.TRIG_SRC_D0S: "d0s",
            P1150API.TRIG_SRC_D1:  "d1"
        }

        # this is a deque() with a max length, reason for this
        # is for the OSC mode, where there is a window of data that
        # waits for trigger to occur.  Using a deque with max length
        # simplifies the buffer management... data falls out the que automatically
        self._adc = None

        self._tmr_start = 0.0  # for benchmarking with timer()

        # _adc_buf is a temporary holding buffer
        # Using fixed-size numpy arrays for numeric data
        self._adc_buf = {
            "t": np.zeros(12500),
            "i": np.zeros(12500),
            "a0": np.zeros(12500),
            "d0": np.zeros(12500),
            "d0s": ["" for _ in range(12500)],  # d0s remains a list for strings
            "d1": np.zeros(12500),
            "isnk": np.zeros(12500),
            "len": 0  # track current fill level
        }

    def adc_stream_in(self, item) -> None:
        if not self._acquire:
            return

        with self._lock_stream:
            self._event_adc_stream_in(item)

    def _event_adc_stream_in(self, item):
        """
        Triggering
        - regardless of triggering setup, data is accumulated until the
          buffer (of size timespan reflected as self.NUM_SAMPLES) is filled
        - the buffer is a deque with a maximum size, self.NUM_SAMPLES, which means
          the buffer stays at maxsize, new samples replace old samples, FIFO.
        - then for every stream packet of samples, samples are added one by one
          to the buffer, and the trigger setup/condition is checked.

        Performance
        - when collecting samples, function takes ~30 usec
        """
        #start = timer()

        # extract d0/1 from the byte of d01 using batch operations and local bindings
        d01 = item['d01']
        d0_vh, d0_vl = self.D0_VHIGH, self.D0_VLOW
        d1_vh, d1_vl = self.D1_VHIGH, self.D1_VLOW

        # Vectorized digital channel calculation
        item["d0"] = (d01 & 0x1) * d0_vh + d0_vl
        item["d1"] = ((d01 >> 1) & 0x1) * d1_vh + d1_vl

        def _append_and_trigger(item: dict, trig_src: str, level: float | int | str, slope: str) -> None:
            """ Append data and trigger
            - self._adc is a fixed length deque, the length of the queue
              is set for one full timebase
            """
            adc = self._adc
            li, la0, ld0, ld1, lisnk = item['i'], item['a0'], item['d0'], item['d1'], item['isnk']
            ld0s = item['d0s']

            idx_trigger_pos, level_num, precond, triggered_event = self._trigger_idx, level, self._trigger_idx_precond, self._acquire_triggered

            if slope == P1150API.TRIG_SLOPE_EITHER:
                if adc[trig_src][idx_trigger_pos] < self._trigger_level:
                    slope = P1150API.TRIG_SLOPE_RISE
                else:
                    slope = P1150API.TRIG_SLOPE_FALL

            # Process samples one-by-one to maintain perfect alignment
            n = len(li)
            for i in range(n):
                # Slice the arrays to shift left by 1 without full np.roll
                for key in ["i", "a0", "d0", "d1", "isnk"]:
                    adc[key][:-1] = adc[key][1:]

                adc["i"][-1] = li[i]
                adc["a0"][-1] = la0[i]
                adc["d0"][-1] = ld0[i]
                adc["d1"][-1] = ld1[i]
                adc["isnk"][-1] = lisnk[i]
                adc["d0s"] = adc["d0s"][1:] + [ld0s[i]]

                # Check trigger at the configured trigger index
                # Note: We check the value at the 'trigger point' in the buffer
                val = adc[trig_src][idx_trigger_pos]

                if isinstance(level_num, (int, float)):
                    if slope == P1150API.TRIG_SLOPE_RISE:
                        if not precond:
                            if val < level_num: precond = True
                        elif val > level_num:
                            triggered_event.set()
                            break
                    else:  # slope == P1150API.TRIG_SLOPE_FALL:
                        if not precond:
                            if val > level_num: precond = True
                        elif val < level_num:
                            triggered_event.set()
                            break
                elif isinstance(level_num, str):
                    if val == level_num:
                        triggered_event.set()
                        break

            self._trigger_idx_precond = precond

        if self._acquire_datardy.is_set():
            # Buffer new data into _adc_buf if GUI hasn't picked up previous trigger
            n = len(item['i'])
            cur = self._adc_buf["len"]
            if cur + n <= 12500:
                for key in ["i", "a0", "d0", "d1", "isnk"]:
                    self._adc_buf[key][cur:cur + n] = item[key]
                self._adc_buf["d0s"][cur:cur + n] = item["d0s"]
                self._adc_buf["len"] += n
            else:
                self.logger.error("_adc_buf overflow")

            if self._buffered_adc_frame_count and self._buffered_adc_frame_count != item['c']:
                self.logger.error(f"adc frame count {item['c']}")
            self._buffered_adc_frame_count = item['c'] + 1

            return

        if self._adc_buf['len'] > 0:
            # Consume buffered data
            n = self._adc_buf['len']
            for key in ["i", "a0", "d0", "d1", "isnk"]:
                self._adc[key] = np.roll(self._adc[key], -n)
                self._adc[key][-n:] = self._adc_buf[key][:n]

            self._adc["d0s"] = self._adc["d0s"][n:] + self._adc_buf["d0s"][:n]
            self._adc_buf['len'] = 0

        # debug log to check health of streaming
        if self._buffered_adc_frame_count and self._buffered_adc_frame_count != item['c']:
            self.logger.warning(f"adc frame count {item['c']}")
        self._buffered_adc_frame_count = item['c'] + 1

        # fill up the buffer initial state
        if self._adc["fill"] < self.NUM_SAMPLES:
            n = len(item['i'])
            cur = self._adc["fill"]
            end = min(cur + n, self.NUM_SAMPLES)
            take = end - cur

            for key in ["i", "a0", "d0", "d1", "isnk"]:
                self._adc[key][cur:end] = item[key][:take]
            self._adc["d0s"][cur:end] = item["d0s"][:take]
            self._adc["fill"] = end

            if self._adc["fill"] < self.NUM_SAMPLES:
                return

        # At this point there is a full buffer of data representing the TIMEBASE setting
        #self.logger.info(f"self._adc len {len(self._adc['i'])}, mode {self._acquire_mode}, _acquire_triggered {self._acquire_triggered.is_set()}")

        # OSCILLOSCOPE MODE, also used by MAMPHR Mode (ACQUIRE_MODE_RUN)
        if self._acquire_mode in [P1150API.ACQUIRE_MODE_RUN, P1150API.ACQUIRE_MODE_SINGLE]:
            if self._trigger_src == P1150API.TRIG_SRC_NONE:
                self._acquire_triggered.set()

            else:
                # trigger detection on each incoming batch of 50 samples
                if not self._acquire_triggered.is_set():
                    src = self._trig_src_map[self._trigger_src]
                    _append_and_trigger(item, src, self._trigger_level, self._trigger_slope)

            if self._acquire_triggered.is_set():
                # fill in time values
                if self._trigger_pos == P1150API.TRIG_POS_CENTER:
                    t_start = -1 * self._timebase_span / 2
                elif self._trigger_pos == P1150API.TRIG_POS_LEFT:
                    t_start = -1 * self._timebase_span / 4
                else:
                    t_start = -3 * self._timebase_span / 4

                if self._timebase_t_recalc:  # create self._osc_t only once
                    self._timebase_t_recalc = False
                    self._osc_t = t_start + np.arange(self.NUM_SAMPLES) / self.ADC_SAMPLE_RATE

                self._adc["t"] = self._osc_t

        # LOGGER MODE
        elif self._acquire_mode == P1150API.ACQUIRE_MODE_LOGGER:
            if not self._acquire_triggered.is_set() and len(self._adc["i"]) >= self.NUM_SAMPLES :
                self.logger.info(f"triggered, frame count {self._adc_frame_count}, length {len(self._adc['i'])} / {self.NUM_SAMPLES}")
                #self._tmr_start = timer()
                self._acquire_triggered.set()

        # send only one asc_triggered message by checking both flags
        if self._acquire_triggered.is_set() and not self._acquire_datardy.is_set():
            self._acquire_datardy.set()
            self._trigger_idx_precond = False

            if self.cb_acquisition_get_data:
                d = {"t": [*self._adc["t"]],
                     "i": self._adc["i"].copy(),
                     "a0": self._adc["a0"].copy(),
                     "d0": self._adc["d0"].copy(),
                     "d0s": self._adc["d0s"].copy(),
                     "d1": self._adc["d1"].copy(),
                     "isnk": self._adc["isnk"].copy()}

                self.cb_acquisition_get_data(d)
                self._event_clear_datardy()

        #delta = timer() - start
        #self.logger.info(f"transfered {delta:0.6f}")

    def _event_clear_datardy(self) -> None:
        # takes ~11 usec
        self.NUM_SAMPLES = int(self.ADC_SAMPLE_RATE * self._timebase_span)

        self._adc = {
            "t": np.zeros(self.NUM_SAMPLES),
            "i": np.zeros(self.NUM_SAMPLES),
            "a0": np.zeros(self.NUM_SAMPLES),
            "d0": np.zeros(self.NUM_SAMPLES),
            "d0s": ["" for _ in range(self.NUM_SAMPLES)],
            "d1": np.zeros(self.NUM_SAMPLES),
            "isnk": np.zeros(self.NUM_SAMPLES),
            "fill": 0  # track initial fill
        }
        self._acquire_triggered.clear()
        self._acquire_datardy.clear()
        #self.logger.info(f"self._adc cleared")

    def _set_trigger_idx(self) -> None:
        if self._trigger_pos == P1150API.TRIG_POS_CENTER:
            self._trigger_idx = int(self.NUM_SAMPLES / 2)
        elif self._trigger_pos == P1150API.TRIG_POS_LEFT:
            self._trigger_idx = int(self.NUM_SAMPLES / 4)
        else:
            self._trigger_idx = int(self.NUM_SAMPLES - self.NUM_SAMPLES / 4)

        self._timebase_t_recalc = True

    def close(self):
        with self._lock:
            self.uclog_close()

    def temperature_update(self) -> tuple[bool, list[dict] | None]:
        """ Trigger a temperature update
        - use when start connection to target to get the current temperature

        :return: success <True/False>,
                 result "{.f:s,.success:?}"
        """
        with self._lock:
            payload = {"f": "cmd_temp102_trigger"}
            return self.uclog_response(payload)

    def status(self) -> tuple[bool, list[dict] | None]:
        """ Get Status

        :return: success <True|False>,
                 result {'f': 'cmd_status', 's': True, 't_degc': 36, 'acquiring': False,
                         'vout': 500, 'cal_done': False, 'probe': False, 'ovc_ma': 3210, 'err': 0, 'err_act': 0}
        """
        with self._lock:
            payload = {"f": "cmd_status"}
            return self.uclog_response(payload)

    def vout_metrics(self) -> tuple[bool, list[dict] | None]:
        """ Get VOUT hardware capabilities

        :return: success <True|False>,
                 result {'f': 'cmd_vout_metrics', 's': True, 'max': 17000, 'min': 500, 'step': 10}
        """
        with self._lock:
            payload = {"f": "cmd_vout_metrics"}
            return self.uclog_response(payload)

    def cal_status(self) -> tuple[bool, list[dict] | None]:
        """ Get Calibration Status
        - Client is meant to poll this function once calibration has been started

        :return: success <True|False>,
                 result {'f': 'cmd_cal_status', 's': True, 'cal_done': False,
                         'progress': 77, 'vout_set': 13200, 'vout': 13097,
                         'dacc': 2910, 'err': 0, 'err_act': 0}
        """
        with self._lock:
            payload = {"f": "cmd_cal_status"}
            return self.uclog_response(payload)

    def calibrate(self, force: bool=False, blocking: bool=True) -> tuple[bool, list[dict] | None]:
        """ Calibrate (this can take ~20 seconds)

        :param force: <True|False>, if True, starts a calibration
        :param blocking: <True|False>
        :return: success <True|False>
        """
        with self._lock:
            # determine if calibration has been done
            payload = {"f": "cmd_cal_status"}
            success, result = self.uclog_response(payload)
            if not success:
                return False, result

            cal_complete = result[0]["cal_done"]
            if not cal_complete or force:
                self.logger.info("Calibrating... this will take a minute...")
                payload = {"f": "cmd_cal", "force": force}
                success, result = self.uclog_response(payload)
                if not success:
                    return False, result

                if blocking:
                    # poll to determine if calibration has completed
                    retries = self.RETRIES_CALIBRATION_POLL
                    while not cal_complete and retries:
                        sleep(self.DELAY_WAIT_CALIBRATION_POLL_S)
                        retries -= 1
                        payload = {"f": "cmd_cal_status"}
                        success, result = self.uclog_response(payload)
                        if not success or retries == 0:
                            return False, result
                        self.logger.info(f"{result[0]}")
                        cal_complete = result[0]["cal_done"]

            return True, result

    def set_vout(self, value_mv: int, ch: int=1) -> tuple[bool, list[dict] | None]:
        """ Set VOUT

        :param value_mv: <int>
        :return:  success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_vout", "mv": value_mv}  # in mV
            return self.uclog_response(payload)

    def set_vout_remote_sense(self, en: bool=False) -> tuple[bool, list[dict] | None]:
        """ Enable/Disable Pseudo Remote Sense on VOUT

        :param en: <True/False>
        :return:  success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_vout_rs", "en": en}
            return self.uclog_response(payload)

    def set_ovc(self, value_ma: int) -> tuple[bool, list[dict] | None]:
        """ Set Over Current in mA

        :param value_ma: <int>
        :return:  success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_ovrcur", "ma": value_ma}  # in ma
            return self.uclog_response(payload)

    def set_timebase(self, span: str) -> tuple[bool, list[dict] | None]:
        """ Set Timebase

        :param span: <one of TBASE_SPAN_LIST>
        :return:  success <True/False>, result <json/None>
        """
        with self._lock_stream:
            self._timebase_span = self.TBASE_MAP[span]
            # abort the current acquisition
            self._event_clear_datardy()
            self._set_trigger_idx()
            return True, None

    def set_trigger(self,
                    src: str=P1150API.TRIG_SRC_NONE,
                    pos: str=P1150API.TRIG_POS_LEFT,
                    slope: str=P1150API.TRIG_SLOPE_RISE,
                    level: str | int=1) -> tuple[bool, list[dict] | None]:
        """ Set Trigger

        :param src: <P1150API.TRIG_SRC_*>
        :param pos: <P1150API.TRIG_POS_*>
        :param slope: <P1150API.TRIG_SLOPE_*>
        :param level: <int in mV or mA> || character for D0s
        :return: success <True/False>, result <json/None>
        """
        with self._lock_stream:

            if src in [P1150API.TRIG_SRC_D0, P1150API.TRIG_SRC_D1]:
                self._trigger_level = int(self.D0_VHIGH / 2)

            else:
                self._trigger_level = level

            self._trigger_pos = pos
            self._trigger_slope = slope
            self._trigger_src = src
            self.logger.info(f"pos {pos}, slope {slope}, src {src}, level {level}")
            return True, None

    def set_cal_load(self, loads: list=[P1150API.DEMO_CAL_LOAD_NONE]) -> tuple[bool, list[dict] | None]:
        """ Set Calibration Load

        - more than one load can be specified where the resultant loads are in parallel

        :param loads: [P1150API.DEMO_CAL_LOAD_*, ...]
        :return: success <True/False>, result <json/None>
        """
        load_bit_mask = 0x0
        if P1150API.DEMO_CAL_LOAD_NONE in loads:
            pass
        else:
            if P1150API.DEMO_CAL_LOAD_10 in loads:   load_bit_mask |= 0x1
            if P1150API.DEMO_CAL_LOAD_20 in loads:   load_bit_mask |= 0x2
            if P1150API.DEMO_CAL_LOAD_40 in loads:   load_bit_mask |= 0x4
            if P1150API.DEMO_CAL_LOAD_100 in loads:  load_bit_mask |= 0x100
            if P1150API.DEMO_CAL_LOAD_200 in loads:  load_bit_mask |= 0x8
            if P1150API.DEMO_CAL_LOAD_400 in loads:  load_bit_mask |= 0x200
            if P1150API.DEMO_CAL_LOAD_2K in loads:   load_bit_mask |= 0x10
            if P1150API.DEMO_CAL_LOAD_20K in loads:  load_bit_mask |= 0x20
            if P1150API.DEMO_CAL_LOAD_200K in loads: load_bit_mask |= 0x40
            if P1150API.DEMO_CAL_LOAD_2M in loads:   load_bit_mask |= 0x80

        with self._lock:
            payload = {"f": "cmd_iload", "set": load_bit_mask}
            return self.uclog_response(payload)

    def set_cal_sweep(self, sweep: bool) -> tuple[bool, list[dict] | None]:
        """ Set Calibration Load Sweep

        :param sweep: <True/False>
        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_iload_sweep", "en": sweep}
            return self.uclog_response(payload)

    def acquisition_start(self, mode: str) -> tuple[bool, list[dict] | None]:
        """ Start Acquisition (Single mode)

        :param mode: <P1150API.ACQUIRE_MODE_*>
        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            if self._acquire:
                # when a43 is acquiring this command has no affect
                payload = {"f": "cmd_adc", "en": True}
                return self.uclog_response(payload)

            self.NUM_SAMPLES = int(self.ADC_SAMPLE_RATE * self._timebase_span)
            self._set_trigger_idx()
            self._acquire_mode = mode
            self._event_clear_datardy()
            self._acquire = True
            #self.logger.info(f"NUM_SAMPLES {self.NUM_SAMPLES}, timebase {self._timebase_span}, trig idx {self._trigger_idx}")
            payload = {"f": "cmd_adc", "en": True}
            return self.uclog_response(payload)

    def acquisition_stop(self) -> tuple[bool, list[dict] | None]:
        """ Stop/Abort Acquisition

        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            self._acquire = False
            self._acquire_triggered.clear()
            self._event_clear_datardy()
            self._adc_frame_count = None
            self._buffered_adc_frame_count = 0

            # Clear buffered data by resetting the length and zeroing the arrays
            self._adc_buf["len"] = 0
            for key in ["i", "a0", "d0", "d1", "isnk"]:
                self._adc_buf[key].fill(0.0)
            self._adc_buf["d0s"] = ["" for _ in range(12500)]

            payload = {"f": "cmd_adc", "en": False}
            return self.uclog_response(payload)

    def acquisition_complete(self) -> tuple[bool, list[dict]]:
        """ Poll Acquisistion Complete

        :param retries: number of polling retries
        :return: success <True/False>, {"triggered": <bool>}}
        """
        with self._lock:
            return True, [{"triggered": self._acquire_datardy.is_set()}]

    def acquisition_get_data(self) -> tuple[bool, dict]:
        """ Get Acquisition Data

        NOTE: The returned data must be of deepcopy type,as the
              adc buffer is needed here for the next acquisition

        :return: success <True/False>, result <json/None>
        """
        # NOTE: performance this is taking 1-2ms
        #       includes the cost of self._event_clear_datardy()

        if not self._acquire_datardy.is_set():
            self.logger.error("No data to get")
            return False, {"ERROR": "No data to get"}

        # Return copies of the numpy arrays
        d = {
            "t": self._adc["t"].copy(),
            "i": self._adc["i"].copy(),
            "a0": self._adc["a0"].copy(),
            "d0": self._adc["d0"].copy(),
            "d1": self._adc["d1"].copy(),
            "isnk": self._adc["isnk"].copy()
        }

        self._event_clear_datardy()
        #delta = timer() - self._tmr_start
        #self.logger.info(f"{delta}")
        return True, d

    def probe(self, ch: int=1, connect: bool=True, hard_connect: bool=False, rs_comp: bool=False) -> tuple[bool, list[dict] | None]:
        """ Set Probe Connect

        If the probe is not connected, connecting will fail.  The P1125 can detect
        whether the probe is connected or not.  See probe_status()

        Setting hard_connect will bypass the soft start feature.

        :param connect: <True/False>
        :param hard_connect: <True/False>
        :param rs_comp: <True/False> enable Source Resistance VOUT Compensation
        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_probe", "v": connect, "hard": hard_connect, "comp": rs_comp}
            return self.uclog_response(payload)

    def clear_error(self) -> tuple[bool, list[dict] | None]:
        """ Clear Error

        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_error_clear"}
            return self.uclog_response(payload)

    def led_blink(self) -> tuple[bool, list[dict] | None]:
        """ blink P1150 LED
        - can be used to identify P1150 in case of multiple P1150s

        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_led_blink"}
            return self.uclog_response(payload)

    def ping(self) -> tuple[bool, list[dict] | None]:
        """ Bootloader Ping

        resp: {'f': 'cmd_ping', 's': <True/False>, 'app': <"a51"||"a43">
               'version': '@(#)1-0-gXXXXXXX', 'serial': <byte string>}

        app: a51 - bootloader, a43 - application

        """
        with self._lock:
            payload = {"f": "cmd_ping"}
            success, rsp = self.uclog_response(payload)
            self.logger.info(f"ping resp: {rsp}")
            if rsp is not None:
                rsp = rsp[-1]
                if 'serial' in rsp:
                    serial = struct.unpack('<III', rsp['serial'])
                    rsp['serial'] = f'{serial[2]:08X}-{serial[1]:08X}-{serial[0]:08X}'
                    ser = hashlib.shake_128(rsp['serial'].encode()).hexdigest(4).upper()
                    rsp['serial_hash'] = f'{ser}'

                if "hwver" in rsp:
                    self._hwver = f'{rsp["hwver"]:08X}'
                    self.logger.info(self._hwver)

                    if self._hwver in ["A0431100"]:
                        # compensate for a4311 INA/OPA filter peaking
                        self._low_pass_filter = True

                return success, [rsp]

            else:
                self.logger.error("ping failed to process response")
                return False, None

    def bootloader_init(self) -> tuple[bool, list[dict] | None]:
        """ Bootloader Init

        {'f': 'bl_init', 's': <True/False>}

        """
        with self._lock:
            payload = {"f": "bl_init"}
            return self.uclog_response(payload)

    def bootloader_block(self, data: bytes) -> tuple[bool, list[dict] | None]:
        """ Bootloader Send Block to Load

        {'rsp': 'bl_block', 's': <True/False>}

        """
        with self._lock:
            payload = {"f": "bl_block", "data": data}
            return self.uclog_response(payload)

    def bootloader_done(self) -> tuple[bool, list[dict] | None]:
        """ Bootloader Done

        {'rsp': 'bl_done', 's': <True/False>}

        """
        with self._lock:
            payload = {"f": "bl_done"}
            return self.uclog_response(payload)

    def cmd(self, cmd: dict) -> tuple[bool, list[dict] | None]:
        """ Send raw command to target

        :param cmd: {'f': 'cmd_*", 'arg': 'value', ...}
        :return:
        """
        with self._lock:
            return self.uclog_response(cmd)

    def ez_connect(self, progress_callback=None) -> tuple[bool, dict | None]:
        """ Connect to P1150, upload AFI if necessary, Calibrate if necessary
        - hides all the details of connecting to P1150
        - if success False, the client should close()

        :param progress_callback: <function> called with progress percentage and message
                                  def _connect_progress(progress: float, msg: str) -> None:
        :return: success <True/False>, response <dict>
        """
        if progress_callback is not None:
            progress_callback(0, "Start")

        success, response = self.ping()
        if not success:
            self.logger.error(f"ping {self._port}: {response}")
            return False, {"ERROR": "ping failed"}

        # command responses are a list, and almost always have only one item,
        # however its possible more responses are present, always just take last
        _response = response[-1]

        if not _response["hs"]:
            self.logger.error(f"high speed required: {response}")
            return False, {"ERROR": "high speed required"}

        if _response["app"] == "a51":
            # P1150 is running bootloader, now we must download the app (a43)

            success, result = self.bootloader_init()
            if not success:
                self.logger.error(f"high speed required: {_response}")
                return False, {"ERROR": "bootloader_init failed", "response": _response}

            self.logger.info(f"bootloader_init {result}")
            mtu = result[0]['mtu']

            driver_dir = os.path.dirname(__file__)
            firmware_file = os.path.join(driver_dir, "firmware", "a43_app.signed.ico")
            with open(firmware_file, 'rb') as f:
                data = f.read()
            self.logger.info(f'a43_app.signed.ico len: {len(data)}')

            if progress_callback is not None:
                progress_callback(1, "Bootloader")

            while len(data) > 0:
                d, data = data[:mtu], data[mtu:]
                success, result = self.bootloader_block(d)
                if not success:
                    self.logger.error(f"bootloader_block {result}")
                    return False, {"ERROR": "bootloader_block failed", "response": result}
                # logger.info(f"bootloader_block {result}")

            success, result = self.bootloader_done()
            if not success:
                self.logger.error(f"bootloader_done {result}")
                return False, {"ERROR": "bootloader_done failed", "response": result}

            # the P1150 is rebooting and will re-enumerate with USB
            self.close()
            sleep(0.200)  # allow time for device to disconnect/reboot

            if progress_callback is not None:
                progress_callback(2, "Reconnect")

            # wait for host OS to see the COM port return
            start = timer()
            ports_connected = [p.device for p in serial.tools.list_ports.comports()]
            found = False
            while timer() - start < self.TIME_RECONNECT_AFTER_FWLOAD_S:
                ports_connected = [p.device for p in serial.tools.list_ports.comports()]
                if self._port in ports_connected:
                    self.logger.info(f"port {self._port} re-found")
                    found = True
                    break
                self.logger.info(f"port {self._port} waiting...")
                sleep(0.05)

            if not found:
                self.logger.error(f"P1150 {self._port} not FOUND in {ports_connected} - timeout")
                return False, {"ERROR": f"P1150 not found on port {self._port} after FW update"}

            # wait for reconnect, although the port is found, sometimes more delay is required
            sleep(1)
            # client must retry to re-connect to application FW (a43)
            return True, _response

        # application a43 is running
        success, response_status = self.status()
        if not success:
            self.logger.error(f"status {self._port}: {response_status}")
            return False, {"ERROR": "status failed"}

        response_status = response_status[-1]
        self.logger.info(f"status {self._port}: {response_status}")

        if progress_callback is not None:
            progress_callback(3, "Connected")

        # calibration
        if not response_status['cal_done']:
            success, result = self.calibrate(blocking=False, force=True)
            if not success:
                self.logger.error(f"calibrate {result}")
                return False, {"ERROR": "calibrate start failed"}

            self.logger.info(f"calibrate {result}")

            while True:
                sleep(0.5)

                success, result = self.cal_status()
                if not success:
                    self.logger.error(f"cal_status {result}")
                    return False, {"ERROR": "calibrate in progress failed"}

                self.logger.info(f"cal_status: {result}")
                if progress_callback is not None:
                    progress_callback(result[-1]['progress'], "Calibrating")

                if result[-1]['cal_done']:
                    response_status['cal_done'] = True
                    break

        if progress_callback is not None:
            progress_callback(100, "Done")

        # all done
        _response.update(response_status)
        return True, _response
