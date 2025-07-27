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
import uclog
import traceback
import cbor2
import serial
import serial.tools.list_ports
from collections import deque
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
    TRIG_SRC_D1   = "TRIG_SRC_D1"
    TRIG_SRC_A0A  = "TRIG_SRC_A0A"
    TRIG_SRC_LIST = [
        TRIG_SRC_NONE,
        TRIG_SRC_CUR,
        TRIG_SRC_D0,
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
    DEMO_CAL_LOAD_400  = "DEMO_CAL_LOAD_400_"
    DEMO_CAL_LOAD_200  = "DEMO_CAL_LOAD_200_"
    DEMO_CAL_LOAD_100  = "DEMO_CAL_LOAD_100_"
    DEMO_CAL_LOAD_40   = "DEMO_CAL_LOAD_40_"
    DEMO_CAL_LOAD_20   = "DEMO_CAL_LOAD_20_"
    DEMO_CAL_LOAD_10   = "DEMO_CAL_LOAD_10_"
    DEMO_CAL_LOAD_LIST = [
        DEMO_CAL_LOAD_NONE,
        DEMO_CAL_LOAD_2M,
        DEMO_CAL_LOAD_200K,
        DEMO_CAL_LOAD_20K,
        DEMO_CAL_LOAD_2K,
        DEMO_CAL_LOAD_400,
        DEMO_CAL_LOAD_200,
        DEMO_CAL_LOAD_100,
        DEMO_CAL_LOAD_40,
        DEMO_CAL_LOAD_20,
        DEMO_CAL_LOAD_10,
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
            _h = uclog.hostport(None)

            base_dir = os.path.dirname(__file__)

            app = kw.get('app', "a43")
            if app == "a43":
                elf_file = os.path.join(base_dir, "assets", "a43_app.logdata")

            elif app == "a57":
                elf_file = os.path.join(base_dir, "assets", "a57_app.logdata")

            else:
                raise ValueError("app must be a43, a57")

            self.logger.info(f"{app}: {elf_file}")

            bl_elf_file = os.path.join(base_dir, "assets", "a51_bl.logdata")
            self.logger.info(bl_elf_file)

            _e = uclog.decoders([elf_file, bl_elf_file])
            self.logger.info(f"Using target: {_t}, host {_h}, elf {_e}")

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
            self._cb_uclog_log.uclog_item(item)

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
            # Convert the list of bytes back to int32
            item["i"] = struct.unpack('<' + 'f' * (len(item["i"]) // 4), item["i"])
            item["i"] = [i / 1000000.0 for i in item["i"]]  # convert to mAmps (float)

            # Convert the list of bytes back to int32
            item["isnk"] = struct.unpack('<' + 'f' * (len(item["isnk"]) // 4), item["isnk"])
            item["isnk"] = [i / 1000000.0 for i in item["isnk"]]  # convert to mAmps (float)

            # Convert the list of bytes back to int16
            item["a0"] = struct.unpack('<' + 'H' * (len(item["a0"]) // 2), item["a0"])

            # Convert the list of bytes back to int8
            item["d01"] = struct.unpack('<' + 'B' * (len(item["d01"]) // 1), item["d01"])

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
            # insert cached 2 samples from the tail of the last streamed packet
            item["i"] = self._low_pass_filter_i_cache + item["i"]
            item["isnk"] = self._low_pass_filter_isnk_cache + item["isnk"]

            # wma_list = [sum(data[i + j] * weights[j] for j in range(window_size)) / sum(weights)
            #            for i in range(len(data) - window_size + 1)]
            w = (0.11, 0.78, 0.11)  # must add up to 1.0
            item["i"] = [(sum(item["i"][i + j] * w[j] for j in range(3))) for i in range(48)]
            # cache the last two values for next streamed packet
            self._low_pass_filter_i_cache = item["i"][-2:]
            # final result without the last two (which are cached)
            item["i"] = item["i"][:-2]

            item["isnk"] = [(sum(item["isnk"][i + j] * w[j] for j in range(3))) for i in range(48)]
            self._low_pass_filter_isnk_cache = item["isnk"][-2:]
            item["isnk"] = item["isnk"][:-2]

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

    def uclog_response(self, payload: dict) -> (bool, list):
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

    """
    # Fake Voltages for D0/1 signals to be plotted, these are safe to change
    D0_VLOW = 20
    D1_VLOW = 40
    D0_VHIGH = 1000
    D1_VHIGH = 1100

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
        self.NUM_SAMPLES = int(self._timebase_span * self.ADC_SAMPLE_RATE)

        self._trig_src_map = {
            P1150API.TRIG_SRC_CUR: "i",
            P1150API.TRIG_SRC_A0A: "a0",
            P1150API.TRIG_SRC_D0:  "d0",
            P1150API.TRIG_SRC_D1:  "d1"
        }

        # this is a deque() with a max length, reason for this
        # is for the OSC mode, where there is a window of data that
        # waits for trigger to occur.  Using a deque with max length
        # simplifies the buffer management... data falls out the que automatically
        self._adc = None

        self._tmr_start = 0.0  # for benchmarking with timer()

        # _adc_buf is a temporary holding buffer, used when ADC data is incoming
        # and the previous triggered data hasn't yet been picked up by the GUI
        # 12500 size represents 100ms of data
        self._adc_buf = {"t": deque(maxlen=12500),
                         "i": deque(maxlen=12500),
                         "a0": deque(maxlen=12500),
                         "d0": deque(maxlen=12500),
                         "d1": deque(maxlen=12500),
                         "isnk": deque(maxlen=12500)}

    def adc_stream_in(self, item):
        if not self._acquire: return
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

        # extract d0/1 from the byte of d01
        item["d0"], item["d1"] = ([((i & 0x1) * self.D0_VHIGH + self.D0_VLOW) for i in item['d01']],
                                  [(((i & 0x2) >> 1) * self.D1_VHIGH + self.D1_VLOW) for i in item['d01']])

        def _append_and_trigger(item: dict, trig_src: str, level: int, slope: str):
            """ Append data and tigger
            - self._adc is a fixed length dequeue, the length of the queue
              is set for one full timebase

            :param item: dict of list of samples
            :param trig_src:
            :param level:
            :param slope:
            """
            if slope == P1150API.TRIG_SLOPE_EITHER:
                # if slope is either then for each batch of 50 samples,
                # detect if signal will be falling or rising relative to trigger level
                if self._adc[trig_src][self._trigger_idx] < self._trigger_level:
                    slope = P1150API.TRIG_SLOPE_RISE
                else:
                    slope = P1150API.TRIG_SLOPE_FALL

            # for each batch (50) of incoming data, detect signal crossing trigger level
            for i in range(len(item['i'])):
                # append new sample to the buffer
                self._adc["i"].append(item['i'][i])
                self._adc["a0"].append(item['a0'][i])
                self._adc["d0"].append(item["d0"][i])
                self._adc["d1"].append(item["d1"][i])
                self._adc["isnk"].append(item['isnk'][i])

                # check if trigger happened
                if slope == P1150API.TRIG_SLOPE_RISE:
                    if not self._trigger_idx_precond:
                        if self._adc[trig_src][self._trigger_idx] < level:
                            self._trigger_idx_precond = True

                    else:
                        if self._adc[trig_src][self._trigger_idx] > level:
                            self._acquire_triggered.set()
                            break

                else:  # slope == P1150API.TRIG_SLOPE_FALL:
                    if not self._trigger_idx_precond:
                        if self._adc[trig_src][self._trigger_idx] > level:
                            self._trigger_idx_precond = True

                    else:
                        if self._adc[trig_src][self._trigger_idx] < level:
                            self._acquire_triggered.set()
                            break

        if self._acquire_datardy.is_set():
            #self.logger.warning("incoming data with self._acquire_datardy set")
            # new incoming data when previous data still not consumed, buffer new data into _adc_buf
            # benchmarked at ~0.010 ms (~10usec) per call
            self._adc_buf["i"].extend(item['i'])
            self._adc_buf["a0"].extend(item['a0'])
            self._adc_buf["d0"].extend(item["d0"])
            self._adc_buf["d1"].extend(item["d1"])
            self._adc_buf["isnk"].extend(item['isnk'])

            # TODO: check if _adc_buf buffer overflows its 12500 samples long
            #       if it has overflowed, then we have dropped incoming data
            if 10000 < len(self._adc_buf["i"]) < 12000:
                self.logger.warning(f"_adc_buf len {len(self._adc_buf['i'])}")
            elif len(self._adc_buf["i"]) > 12000:
                self.logger.error(f"_adc_buf len {len(self._adc_buf['i'])}")

            if self._buffered_adc_frame_count and self._buffered_adc_frame_count != item['c']:
                self.logger.error(f"adc frame count {item['c']}")
            self._buffered_adc_frame_count = item['c'] + 1

            return

        if self._adc_buf['i']:  # if not empty - some buffered data came in
            # this if clause takes 10-20 usec to run
            # if there is buffered data, consume all of it
            self._adc["i"].extend(self._adc_buf['i'])
            self._adc["a0"].extend(self._adc_buf['a0'])
            self._adc["d0"].extend(self._adc_buf['d0'])
            self._adc["d1"].extend(self._adc_buf['d1'])
            self._adc["isnk"].extend(self._adc_buf['isnk'])

            # clear this buffer from when data coming is when _acquire_datardy set
            # this is important to this if() clause not to be true a 2nd time
            # TODO: is it faster to just re-init self._adc_buf rather than clear()?
            self._adc_buf['i'].clear()
            self._adc_buf['a0'].clear()
            self._adc_buf['d0'].clear()
            self._adc_buf['d1'].clear()
            self._adc_buf['isnk'].clear()

        # debug log to check health of streaming
        if self._buffered_adc_frame_count and self._buffered_adc_frame_count != item['c']:
            self.logger.warning(f"adc frame count {item['c']}")
        self._buffered_adc_frame_count = item['c'] + 1

        # fill up the buffer, regardless of trigger setup, regardless of mode
        if len(self._adc["i"]) < self.NUM_SAMPLES:
            self._adc["i"].extend(item['i'])
            self._adc["a0"].extend(item['a0'])
            self._adc["d0"].extend(item["d0"])
            self._adc["d1"].extend(item["d1"])
            self._adc["isnk"].extend(item['isnk'])

            if len(self._adc["i"]) <= self.NUM_SAMPLES:
                #if len(self._adc["i"]) % 1000 == 0:
                #    delta = timer() - start
                #    self.logger.info(f" filling {delta:0.6f}, {len(self._adc['i'])}")
                return

        # At this point there is a full buffer of data representing the TIMEBASE setting
        #self.logger.info(f"self._adc len {len(self._adc['i'])}")

        # OSCILLOSCOPE MODE
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

                # TODO: move this to gui_osc.py, so there there is one less array ("t") to send up,
                #       note that logger and mamhr do not use "t"
                # TODO: the list(s) could be pre-calculated and the correct one used, reducing runtime processing
                self._adc["t"] = [t_start + i / self.ADC_SAMPLE_RATE for i in range(self.NUM_SAMPLES)]
                #self.logger.info(f"triggered, frame count {self._adc_frame_count}, length {len(self._adc['i'])} / {self.NUM_SAMPLES}")

        # LOGGER MODE (also used by MAMPHR mode)
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
                     "i": [*self._adc["i"]],
                     "a0": [*self._adc["a0"]],
                     "d0": [*self._adc["d0"]],
                     "d1": [*self._adc["d1"]],
                     "isnk": [*self._adc["isnk"]]}

                self.cb_acquisition_get_data(d)
                self._event_clear_datardy()

        #delta = timer() - start
        #self.logger.info(f"transfered {delta:0.6f}")

    def _event_clear_datardy(self):
        # takes ~11 usec
        self.NUM_SAMPLES = int(self.ADC_SAMPLE_RATE * self._timebase_span)

        self._adc = {"t": deque(maxlen=self.NUM_SAMPLES),
                     "i": deque(maxlen=self.NUM_SAMPLES),
                     "a0": deque(maxlen=self.NUM_SAMPLES),
                     "d0": deque(maxlen=self.NUM_SAMPLES),
                     "d1": deque(maxlen=self.NUM_SAMPLES),
                     "isnk": deque(maxlen=self.NUM_SAMPLES)}
        self._acquire_triggered.clear()
        self._acquire_datardy.clear()
        #self.logger.info(f"self._adc cleared")

    def _set_trigger_idx(self):
        if self._trigger_pos == P1150API.TRIG_POS_CENTER:
            self._trigger_idx = int(self.NUM_SAMPLES / 2)
        elif self._trigger_pos == P1150API.TRIG_POS_LEFT:
            self._trigger_idx = int(self.NUM_SAMPLES / 4)
        else:
            self._trigger_idx = int(self.NUM_SAMPLES - self.NUM_SAMPLES / 4)

    def close(self):
        with self._lock:
            self.uclog_close()

    def temperature_update(self) -> (bool, dict):
        """ Trigger a temperature update
        - use when start connection to target to get the current temperature

        :return: success <True/False>,
                 result "{.f:s,.success:?}"
        """
        with self._lock:
            payload = {"f": "cmd_temp102_trigger"}
            return self.uclog_response(payload)

    def status(self) -> (bool, dict):
        """ Get Status

        :return: success <True|False>,
                 result {'f': 'cmd_status', 's': True, 't_degc': 36, 'acquiring': False,
                         'vout': 500, 'cal_done': False, 'probe': False, 'ovc_ma': 3210, 'err': 0, 'err_act': 0}
        """
        with self._lock:
            payload = {"f": "cmd_status"}
            return self.uclog_response(payload)

    def vout_metrics(self) -> (bool, dict):
        """ Get VOUT hardware capabilities

        :return: success <True|False>,
                 result {'f': 'cmd_vout_metrics', 's': True, 'max': 17000, 'min': 500, 'step': 10}
        """
        with self._lock:
            payload = {"f": "cmd_vout_metrics"}
            return self.uclog_response(payload)

    def cal_status(self) -> (bool, dict):
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

    def calibrate(self, force: bool=False, blocking: bool=True) -> (bool, dict):
        """ Calibrate (this can take ~20 seconds)

        :param force: <True|False>, if True, starts a calibration
        :param blocking: <True|False>
        :return: success <True|False>
        """
        with self._lock:
            # determine if calibration has been done
            payload = {"f": "cmd_cal_status"}
            success, result = self.uclog_response(payload)
            if not success: return False, result

            cal_complete = result[0]["cal_done"]
            if not cal_complete or force:
                self.logger.info("Calibrating... this will take a minute...")
                payload = {"f": "cmd_cal", "force": force}
                success, result = self.uclog_response(payload)
                if not success: return False, result

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

    def set_vout(self, value_mv: int) -> (bool, dict):
        """ Set VOUT

        :param value_mv: <int>
        :return:  success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_vout", "mv": value_mv}  # in mV
            return self.uclog_response(payload)

    def set_ovc(self, value_ma: int) -> (bool, dict):
        """ Set Over Current in mA

        :param value_ma: <int>
        :return:  success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_ovrcur", "ma": value_ma}  # in ma
            return self.uclog_response(payload)

    def set_timebase(self, span: str) -> (bool, dict):
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
                    level: int=1) -> (bool, dict):
        """ Set Trigger

        :param src: <P1150API.TRIG_SRC_*>
        :param pos: <P1150API.TRIG_POS_*>
        :param slope: <P1150API.TRIG_SLOPE_*>
        :param level: <int in mV or uA>
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

    def set_cal_sweep(self, sweep: bool) -> (bool, dict):
        """ Set Calibration Load Sweep

        :param sweep: <True/False>
        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_iload_sweep", "en": sweep}
            return self.uclog_response(payload)

    def acquisition_start(self, mode: str) -> (bool, dict):
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

    def acquisition_stop(self) -> (bool, dict):
        """ Stop/Abort Acquisition

        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            self._acquire = False
            self._acquire_triggered.clear()
            self._event_clear_datardy()
            self._adc_frame_count = None
            self._buffered_adc_frame_count = 0

            # clear any in waiting data
            self._adc_buf['i'].clear()
            self._adc_buf['a0'].clear()
            self._adc_buf['d0'].clear()
            self._adc_buf['d1'].clear()
            self._adc_buf['isnk'].clear()

            payload = {"f": "cmd_adc", "en": False}
            return self.uclog_response(payload)

    def acquisition_complete(self) -> (bool, dict):
        """ Poll Acquisistion Complete

        :param retries: number of polling retries
        :return: success <True/False>, {"triggered": <bool>}}
        """
        with self._lock:
            return True, {"triggered": self._acquire_datardy.is_set()}

    def acquisition_get_data(self) -> (bool, dict):
        """ Get Acquisition Data

        NOTE: The returned data must be of deepcopy type,as the
              adc buffer is needed here for the next acquisition

        :return: success <True/False>, result <json/None>
        """
        # NOTE: performance this is taking 1-2ms
        #       includes the cost of self._event_clear_datardy()

        if not self._acquire_datardy.is_set():
            self.logger.error("No data to get")
            return False, "No data to get"

        # NOTE: performance this is taking ~1ms, tried alternatives,
        #       copy(), list(), which were slower
        #       used timeit.timer() in _event_p1125_acqdata_log to benchmark.
        d = {"t": [*self._adc["t"]],
             "i": [*self._adc["i"]],
             "a0": [*self._adc["a0"]],
             "d0": [*self._adc["d0"]],
             "d1": [*self._adc["d1"]],
             "isnk": [*self._adc["isnk"]]}

        self._event_clear_datardy()
        #delta = timer() - self._tmr_start
        #self.logger.info(f"{delta}")
        return True, d

    def probe(self, connect: bool=True, hard_connect: bool=False) -> (bool, dict):
        """ Set Probe Connect

        If the probe is not connected, connecting will fail.  The P1125 can detect
        whether the probe is connected or not.  See probe_status()

        Setting hard_connect will bypass the soft start feature.

        :param connect: <True/False>
        :param hard_connect: <True/False>
        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_probe", "v": connect, "hard": hard_connect}
            return self.uclog_response(payload)

    def clear_error(self) -> (bool, dict):
        """ Clear Error

        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_error_clear"}
            return self.uclog_response(payload)

    def led_blink(self) -> (bool, dict):
        """ blink P1150 LED
        - can be used to identify P1150 in case of multiple P1150s

        :return: success <True/False>, result <json/None>
        """
        with self._lock:
            payload = {"f": "cmd_led_blink"}
            return self.uclog_response(payload)

    def ping(self) -> (bool, dict):
        """ Bootloader Ping

        resp: {'f': 'cmd_ping', 's': <True/False>, 'app': <"a51"||"a43">
               'version': '@(#)1-0-gXXXXXXX', 'serial': <byte string>}

        app: a51 - bootloader, a43 - application

        """
        with self._lock:
            payload = {"f": "cmd_ping"}
            rsp = self.uclog_response(payload)
            if (len(rsp) == 2) and (rsp[1] is not None) and (len(rsp[1]) == 1):

                if 'serial' in rsp[1][0]:
                    serial = struct.unpack('<III', rsp[1][0]['serial'])
                    rsp[1][0]['serial'] = f'{serial[2]:08X}-{serial[1]:08X}-{serial[0]:08X}'
                    ser = hashlib.shake_128(rsp[1][0]['serial'].encode()).hexdigest(4).upper()
                    rsp[1][0]['serial_hash'] = f'{ser}'

                if "hwver" in rsp[1][0]:
                    self._hwver = f'{rsp[1][0]["hwver"]:08X}'
                    self.logger.info(self._hwver)

                    if self._hwver in ["A0431100"]:
                        # compensate for a4311 INA/OPA filter peaking
                        self._low_pass_filter = True

            return rsp

    def bootloader_init(self) -> (bool, dict):
        """ Bootloader Init

        {'f': 'bl_init', 's': <True/False>}

        """
        with self._lock:
            payload = {"f": "bl_init"}
            return self.uclog_response(payload)

    def bootloader_block(self, data: bytes) -> (bool, dict):
        """ Bootloader Send Block to Load

        {'rsp': 'bl_block', 's': <True/False>}

        """
        with self._lock:
            payload = {"f": "bl_block", "data": data}
            return self.uclog_response(payload)

    def bootloader_done(self) -> (bool, dict):
        """ Bootloader Done

        {'rsp': 'bl_done', 's': <True/False>}

        """
        with self._lock:
            payload = {"f": "bl_done"}
            return self.uclog_response(payload)

    def cmd(self, cmd: dict) -> (bool, dict):
        """ Send raw command to target

        :param cmd: {'f': 'cmd_*", 'arg': 'value', ...}
        :return:
        """
        with self._lock:
            return self.uclog_response(cmd)

    def ez_connect(self) -> (bool, dict):
        """ Connect to P1150, upload AFI if necessary, Calibrate if necessary
        - hides all the details of connecting to P1150
        - if success False, client should close()

        :return: success <True/False>, response <dict>
        """
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

            firmware_file = os.path.join(os.path.dirname(__file__), "assets", "a43_app.signed.ico")
            with open(firmware_file, 'rb') as f:
                data = f.read()
            self.logger.info(f'a43_app.signed.ico len: {len(data)}')

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
                if result[-1]['cal_done']:
                    break

        # all done
        return True, _response
