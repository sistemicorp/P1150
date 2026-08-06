# -*- coding: utf-8 -*-
"""
MIT License

PXXXX - Python ctypes wrapper around the pxxxx C DLL.

Mirrors the P1150 class API so existing application code requires
only minor changes (import + class name).
"""
import ctypes
import os
import sys
import json
import struct
import platform
from dataclasses import dataclass
from threading import Lock, Event
import hashlib

# ------------------------------------------------------------------ #
# DLL location                                                         #
# ------------------------------------------------------------------ #
def _find_dll():
    """Locate the compiled pxxxx shared library."""
    here = os.path.dirname(__file__)
    candidates = []
    if sys.platform == "win32":
        names = ["pxxxx.dll"]
    elif sys.platform == "darwin":
        names = ["libpxxxx.dylib", "pxxxx.dylib"]
    else:
        names = ["libpxxxx.so", "pxxxx.so"]

    # Search order: pxxxx/ dir, pxxxx_dll/build, pxxxx_dll/build/Release
    search_dirs = [
        here,
        os.path.join(here, "..", "pxxxx_dll", "build"),
        os.path.join(here, "..", "pxxxx_dll", "build", "Release"),
        os.path.join(here, "..", "pxxxx_dll", "build", "Debug"),
    ]
    for d in search_dirs:
        for n in names:
            p = os.path.normpath(os.path.join(d, n))
            if os.path.isfile(p):
                return p

    raise FileNotFoundError(
        f"pxxxx DLL not found. Build pxxxx_dll with CMake first.\n"
        f"Searched in: {[os.path.normpath(d) for d in search_dirs]}"
    )

_lib = None
def _get_lib():
    global _lib
    if _lib is None:
        _lib = ctypes.CDLL(_find_dll())
        _setup_lib(_lib)
    return _lib

# ------------------------------------------------------------------ #
# ctypes structures                                                    #
# ------------------------------------------------------------------ #
PXXXX_MAX_SAMPLES = 125000
PXXXX_PORT_STR    = 64

class PxxxxAcqData(ctypes.Structure):
    _fields_ = [
        ("t",        ctypes.c_float * PXXXX_MAX_SAMPLES),
        ("i",        ctypes.c_float * PXXXX_MAX_SAMPLES),
        ("isnk",     ctypes.c_float * PXXXX_MAX_SAMPLES),
        ("a0",       ctypes.c_float * PXXXX_MAX_SAMPLES),
        ("d0",       ctypes.c_float * PXXXX_MAX_SAMPLES),
        ("d1",       ctypes.c_float * PXXXX_MAX_SAMPLES),
        ("d0s",      ctypes.c_uint8 * PXXXX_MAX_SAMPLES),
        ("n_samples",ctypes.c_int),
        # Optional extra current streams (e.g. P3260). Valid only when the
        # matching has_* flag is set; not low-pass filtered. Appended to match
        # the C struct's field order exactly.
        ("i2",       ctypes.c_float * PXXXX_MAX_SAMPLES),
        ("i3",       ctypes.c_float * PXXXX_MAX_SAMPLES),
        ("has_i2",   ctypes.c_int),
        ("has_i3",   ctypes.c_int),
    ]

class PxxxxPing(ctypes.Structure):
    _fields_ = [
        ("app",         ctypes.c_char * 8),
        ("version",     ctypes.c_char * 64),
        ("serial",      ctypes.c_char * 32),
        ("serial_hash", ctypes.c_char * 16),
        ("model",       ctypes.c_char * 16),
        ("hwver",       ctypes.c_uint32),
        ("hs",          ctypes.c_int),
    ]

class PxxxxStatus(ctypes.Structure):
    _fields_ = [
        ("t_degc",    ctypes.c_float),
        ("acquiring", ctypes.c_int),
        ("vout",      ctypes.c_int),
        ("cal_done",  ctypes.c_int),
        ("probe",     ctypes.c_int),
        ("ovc_ma",    ctypes.c_int),
        ("err",       ctypes.c_uint32),
        ("err_act",   ctypes.c_uint32),
    ]

class PxxxxCalStatus(ctypes.Structure):
    _fields_ = [
        ("cal_done",  ctypes.c_int),
        ("progress",  ctypes.c_int),
        ("vout_set",  ctypes.c_int),
        ("vout",      ctypes.c_int),
        ("dacc",      ctypes.c_int),
        ("err",       ctypes.c_uint32),
        ("err_act",   ctypes.c_uint32),
    ]

class PxxxxVoutMetrics(ctypes.Structure):
    _fields_ = [
        ("max",  ctypes.c_int),
        ("min",  ctypes.c_int),
        ("step", ctypes.c_int),
    ]

class PxxxxConnectResult(ctypes.Structure):
    # Keep in sync with pxxxx_connect_result_t in pxxxx.h
    _fields_ = [
        ("ping",   PxxxxPing),
        ("status", PxxxxStatus),
        ("uclog",  ctypes.c_char * 64),
    ]

# ------------------------------------------------------------------ #
# Callback types                                                       #
# ------------------------------------------------------------------ #
CB_ACQ_T      = ctypes.CFUNCTYPE(None, ctypes.POINTER(PxxxxAcqData), ctypes.c_void_p)
CB_ASYNC_T    = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p)
CB_PROGRESS_T = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
CB_LOG_T      = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)

# ------------------------------------------------------------------ #
# Library function signatures                                          #
# ------------------------------------------------------------------ #
def _setup_lib(lib):
    void_p = ctypes.c_void_p
    int_t  = ctypes.c_int

    lib.pxxxx_create.restype  = void_p
    lib.pxxxx_create.argtypes = []

    lib.pxxxx_destroy.restype  = None
    lib.pxxxx_destroy.argtypes = [void_p]

    lib.pxxxx_set_cb_acq.restype  = None
    lib.pxxxx_set_cb_acq.argtypes = [void_p, CB_ACQ_T, void_p]

    lib.pxxxx_set_cb_async.restype  = None
    lib.pxxxx_set_cb_async.argtypes = [void_p, CB_ASYNC_T, void_p]

    lib.pxxxx_set_cb_log.restype  = None
    lib.pxxxx_set_cb_log.argtypes = [void_p, CB_LOG_T, void_p]

    lib.pxxxx_find_port.restype  = int_t
    lib.pxxxx_find_port.argtypes = [ctypes.c_char_p, ctypes.c_char_p, int_t]

    # ports is a char[max][PXXXX_PORT_STR] block; ctypes passes the buffer as a
    # plain pointer, so c_char_p is the right declaration for the array param.
    if hasattr(lib, "pxxxx_list_ports"):
        lib.pxxxx_list_ports.restype  = int_t
        lib.pxxxx_list_ports.argtypes = [ctypes.c_char_p, int_t]

    # Ethernet symbols. pxxxx/ is vendored into consuming repos by copying, so
    # a stale libpxxxx.so can end up beside a current PXXXX.py. Declaring these
    # unguarded would raise AttributeError here and break the entire module for
    # serial users too; degrade to "no network support" instead. Callers see it
    # as AttributeError from list_net_devices / set_log_callback.
    if hasattr(lib, "pxxxx_set_cb_log_global"):
        lib.pxxxx_set_cb_log_global.restype  = None
        lib.pxxxx_set_cb_log_global.argtypes = [CB_LOG_T, void_p]

    # devs is a char[max][PXXXX_PORT_STR] block; ctypes passes the buffer as a
    # plain pointer, so c_char_p is the right declaration for the array param.
    if hasattr(lib, "pxxxx_list_net_devices"):
        lib.pxxxx_list_net_devices.restype  = int_t
        lib.pxxxx_list_net_devices.argtypes = [ctypes.c_char_p, int_t, int_t]

    lib.pxxxx_connect.restype  = int_t
    lib.pxxxx_connect.argtypes = [void_p, ctypes.c_char_p]

    lib.pxxxx_ez_connect.restype  = int_t
    lib.pxxxx_ez_connect.argtypes = [
        void_p, ctypes.c_char_p, int_t,
        CB_PROGRESS_T, void_p,
        ctypes.POINTER(PxxxxConnectResult)
    ]

    lib.pxxxx_close.restype  = int_t
    lib.pxxxx_close.argtypes = [void_p]

    lib.pxxxx_is_connected.restype  = int_t
    lib.pxxxx_is_connected.argtypes = [void_p]

    lib.pxxxx_ping.restype  = int_t
    lib.pxxxx_ping.argtypes = [void_p, ctypes.POINTER(PxxxxPing)]

    lib.pxxxx_status.restype  = int_t
    lib.pxxxx_status.argtypes = [void_p, ctypes.POINTER(PxxxxStatus)]

    lib.pxxxx_vout_metrics.restype  = int_t
    lib.pxxxx_vout_metrics.argtypes = [void_p, ctypes.POINTER(PxxxxVoutMetrics)]

    lib.pxxxx_temperature_update.restype  = int_t
    lib.pxxxx_temperature_update.argtypes = [void_p]

    lib.pxxxx_set_vout.restype  = int_t
    lib.pxxxx_set_vout.argtypes = [void_p, int_t]

    lib.pxxxx_set_vout_rs.restype  = int_t
    lib.pxxxx_set_vout_rs.argtypes = [void_p, int_t]

    lib.pxxxx_set_ovc.restype  = int_t
    lib.pxxxx_set_ovc.argtypes = [void_p, int_t]

    lib.pxxxx_set_cal_load.restype  = int_t
    lib.pxxxx_set_cal_load.argtypes = [void_p, ctypes.c_uint32]

    lib.pxxxx_set_cal_sweep.restype  = int_t
    lib.pxxxx_set_cal_sweep.argtypes = [void_p, int_t]

    lib.pxxxx_probe.restype  = int_t
    lib.pxxxx_probe.argtypes = [void_p, int_t, int_t, int_t]

    lib.pxxxx_clear_error.restype  = int_t
    lib.pxxxx_clear_error.argtypes = [void_p]

    lib.pxxxx_led_blink.restype  = int_t
    lib.pxxxx_led_blink.argtypes = [void_p]

    lib.pxxxx_cal_status.restype  = int_t
    lib.pxxxx_cal_status.argtypes = [void_p, ctypes.POINTER(PxxxxCalStatus)]

    lib.pxxxx_calibrate.restype  = int_t
    lib.pxxxx_calibrate.argtypes = [void_p, int_t, int_t]

    lib.pxxxx_set_timebase.restype  = int_t
    lib.pxxxx_set_timebase.argtypes = [void_p, int_t]

    lib.pxxxx_set_trigger.restype  = int_t
    lib.pxxxx_set_trigger.argtypes = [void_p, int_t, int_t, int_t, ctypes.c_float]

    lib.pxxxx_acquisition_start.restype  = int_t
    lib.pxxxx_acquisition_start.argtypes = [void_p, int_t]

    lib.pxxxx_acquisition_stop.restype  = int_t
    lib.pxxxx_acquisition_stop.argtypes = [void_p]

    lib.pxxxx_acquisition_complete.restype  = int_t
    lib.pxxxx_acquisition_complete.argtypes = [void_p, ctypes.POINTER(int_t)]

    lib.pxxxx_acquisition_get_data.restype  = int_t
    lib.pxxxx_acquisition_get_data.argtypes = [void_p, ctypes.POINTER(PxxxxAcqData)]

    lib.pxxxx_bootloader_init.restype  = int_t
    lib.pxxxx_bootloader_init.argtypes = [void_p, ctypes.POINTER(int_t)]

    lib.pxxxx_bootloader_block.restype  = int_t
    lib.pxxxx_bootloader_block.argtypes = [void_p, ctypes.c_char_p, int_t]

    lib.pxxxx_bootloader_done.restype  = int_t
    lib.pxxxx_bootloader_done.argtypes = [void_p]

    lib.pxxxx_reset.restype  = int_t
    lib.pxxxx_reset.argtypes = [void_p, int_t]

    lib.pxxxx_cmd.restype  = int_t
    lib.pxxxx_cmd.argtypes = [void_p, ctypes.c_char_p, ctypes.c_char_p, int_t]

    lib.pxxxx_version.restype  = ctypes.c_char_p
    lib.pxxxx_version.argtypes = []


# ------------------------------------------------------------------ #
# Port discovery (standalone, no device needed)                        #
# ------------------------------------------------------------------ #
def list_ports(max_ports: int = 16) -> list[str]:
    """List every serial port with a PXXXX device attached.

    Uses the DLL's own USB enumeration, so no pyserial is required. Falls back
    to pyserial only for a stale DLL that predates pxxxx_list_ports.
    """
    lib = _get_lib()

    if hasattr(lib, "pxxxx_list_ports"):
        buf = ctypes.create_string_buffer(max_ports * PXXXX_PORT_STR)
        n = lib.pxxxx_list_ports(buf, max_ports)
        if n <= 0:
            return []
        raw = buf.raw
        return [raw[i * PXXXX_PORT_STR:(i + 1) * PXXXX_PORT_STR]
                   .split(b"\0", 1)[0].decode(errors="replace")
                for i in range(n)]

    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return []
    return [p.device for p in lp.comports()
            if p.vid == PXXXX_USB_VID and p.pid == PXXXX_USB_PID]


def get_port_from_sn(sn: str | None = None, list_all: bool = False):
    """Find COM/tty port for a P1150 device by serial number."""
    lib = _get_lib()
    port_buf = ctypes.create_string_buffer(PXXXX_PORT_STR)

    if list_all:
        return list_ports()

    if sn is None:
        raise ValueError("sn must be provided")

    rc = lib.pxxxx_find_port(sn.encode(), port_buf, PXXXX_PORT_STR)
    return port_buf.value.decode() if rc == 0 else None


def list_net_devices(timeout_ms: int = 1500, max_devs: int = 16) -> list[str]:
    """Discover Ethernet devices on the local network via mDNS/DNS-SD.

    Browses for "_uclog._tcp" and "_uclog._udp" for up to timeout_ms and returns
    a list of "tcp://<ip>:<port>" / "udp://<ip>:<port>" strings, each directly
    usable as PXXXX(port=...). The scheme reflects the service the device
    announced on - the P1150 family uses TCP, the P3260 uses UDP - so pass the
    string through unchanged rather than reconstructing it.

    Discovery queries every local IPv4 interface, so a device on a second NIC
    or a USB-Ethernet dongle is found too. Call set_log_callback() first (or
    set PXXXX_DEBUG=1) to see per-interface tracing when nothing turns up.
    Always returns [] in the WASM build, which is serial-only.
    """
    lib = _get_lib()
    buf = ctypes.create_string_buffer(max_devs * PXXXX_PORT_STR)
    n = lib.pxxxx_list_net_devices(buf, max_devs, int(timeout_ms))
    if n <= 0:
        return []
    raw = buf.raw
    return [raw[i * PXXXX_PORT_STR:(i + 1) * PXXXX_PORT_STR]
               .split(b"\0", 1)[0].decode(errors="replace")
            for i in range(n)]


# The C side keeps a bare function pointer, so the trampoline must outlive this
# call or ctypes will free it and the DLL will jump into freed memory.
_global_log_cb = None

def set_log_callback(logger=None) -> None:
    """Route the DLL's handle-less log output into a Python logger.

    Discovery (list_net_devices) runs before any PXXXX instance exists, so it
    cannot use the per-instance logger passed to PXXXX(logger=...). This
    installs a process-wide sink for it. Pass logger=None to detach.
    """
    global _global_log_cb
    lib = _get_lib()

    if logger is None:
        lib.pxxxx_set_cb_log_global(CB_LOG_T(0), None)
        _global_log_cb = None
        return

    def _on_log(level, msg_bytes, _user):
        if not msg_bytes:
            return
        msg = msg_bytes.decode("utf-8", errors="replace")
        if level == 0:   logger.info(msg)
        elif level == 1: logger.warning(msg)
        else:            logger.error(msg)

    _global_log_cb = CB_LOG_T(_on_log)
    lib.pxxxx_set_cb_log_global(_global_log_cb, None)


def version() -> str:
    """Return the DLL's own version string, e.g. "0.1-main"
    (git describe --tags + branch, baked in at build time)."""
    v = _get_lib().pxxxx_version()
    return v.decode() if isinstance(v, bytes) else v


PXXXX_USB_VID = 0x0483
PXXXX_USB_PID = 0xa430


# ------------------------------------------------------------------ #
# API constants dataclass (mirrors P1150API)                           #
# ------------------------------------------------------------------ #
@dataclass(frozen=True)
class PxxxxAPI:
    # Channel selection ("ch" field, integer). Single-channel hardware
    # (P1150/P1150D) ignores it; the multi-channel P3260 uses it to address a
    # specific channel. Channels are 1-based, so the API default is ch=1.
    # PROBE_ALL_CHANNELS tells probe() to act on every channel at once.
    PROBE_ALL_CHANNELS = 0

    ACQUIRE_MODE_RUN    = "ACQUIRE_MODE_RUN"
    ACQUIRE_MODE_SINGLE = "ACQUIRE_MODE_SINGLE"
    ACQUIRE_MODE_LOGGER = "ACQUIRE_MODE_LOGGER"
    ACQUIRE_MODE_LIST   = [ACQUIRE_MODE_RUN, ACQUIRE_MODE_SINGLE, ACQUIRE_MODE_LOGGER]

    TRIG_SRC_NONE = "TRIG_SRC_NONE"
    TRIG_SRC_CUR  = "TRIG_SRC_CUR"
    TRIG_SRC_D0   = "TRIG_SRC_D0"
    TRIG_SRC_D0S  = "TRIG_SRC_D0S"
    TRIG_SRC_D1   = "TRIG_SRC_D1"
    TRIG_SRC_A0A  = "TRIG_SRC_A0A"
    TRIG_SRC_CUR2 = "TRIG_SRC_CUR2"   # optional extra current stream i2
    TRIG_SRC_CUR3 = "TRIG_SRC_CUR3"   # optional extra current stream i3
    TRIG_SRC_LIST = [TRIG_SRC_NONE, TRIG_SRC_CUR, TRIG_SRC_D0, TRIG_SRC_D0S,
                     TRIG_SRC_D1, TRIG_SRC_A0A, TRIG_SRC_CUR2, TRIG_SRC_CUR3]

    TRIG_POS_CENTER = "TRIG_POS_CENTER"
    TRIG_POS_LEFT   = "TRIG_POS_LEFT"
    TRIG_POS_RIGHT  = "TRIG_POS_RIGHT"
    TRIG_POS_LIST   = [TRIG_POS_CENTER, TRIG_POS_LEFT, TRIG_POS_RIGHT]

    TRIG_SLOPE_RISE   = "TRIG_SLOPE_RISE"
    TRIG_SLOPE_FALL   = "TRIG_SLOPE_FALL"
    TRIG_SLOPE_EITHER = "TRIG_SLOPE_EITHER"
    TRIG_SLOPE_LIST   = [TRIG_SLOPE_RISE, TRIG_SLOPE_FALL, TRIG_SLOPE_EITHER]

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
    TBASE_SPAN_LIST  = [TBASE_SPAN_10MS, TBASE_SPAN_20MS, TBASE_SPAN_50MS,
                        TBASE_SPAN_100MS, TBASE_SPAN_200MS, TBASE_SPAN_500MS,
                        TBASE_SPAN_1S, TBASE_SPAN_2S, TBASE_SPAN_5S, TBASE_SPAN_10S]

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

    ERROR_NONE          = 0
    ERROR_I2C           = (1 << 0)
    ERROR_HAL           = (1 << 1)
    ERROR_INIT          = (1 << 2)
    ERROR_INIT_TMP      = (1 << 3)
    ERROR_INIT_VMAIN    = (1 << 4)
    ERROR_INIT_ADC      = (1 << 5)
    ERROR_INIT_USBPD    = (1 << 6)
    ERROR_TEMPERATURE   = (1 << 8)
    ERROR_VOUT_FAILURE  = (1 << 9)
    ERROR_CAL           = (1 << 10)
    ERROR_PROBE_CON     = (1 << 11)
    ERROR_SRC_CURRENT   = (1 << 12)
    ERROR_SNK_CURRENT   = (1 << 13)

    ERROR_ACT_DISCONNECT = (1 << 0)
    ERROR_ACT_RESET      = (1 << 1)
    ERROR_ACT_LOCKOUT    = (1 << 2)
    ERROR_ACT_SENDLOG    = (1 << 3)


# ------------------------------------------------------------------ #
# String ↔ integer mapping helpers                                     #
# ------------------------------------------------------------------ #
_TBASE_MAP = {
    "TBASE_SPAN_10MS":  0, "TBASE_SPAN_20MS":  1, "TBASE_SPAN_50MS":  2,
    "TBASE_SPAN_100MS": 3, "TBASE_SPAN_200MS": 4, "TBASE_SPAN_500MS": 5,
    "TBASE_SPAN_1S":    6, "TBASE_SPAN_2S":    7, "TBASE_SPAN_5S":    8,
    "TBASE_SPAN_10S":   9,
}
_TRIG_SRC_MAP = {
    "TRIG_SRC_NONE": 0, "TRIG_SRC_CUR": 1, "TRIG_SRC_D0": 2,
    "TRIG_SRC_D0S":  3, "TRIG_SRC_D1":  4, "TRIG_SRC_A0A": 5,
    "TRIG_SRC_CUR2": 6, "TRIG_SRC_CUR3": 7,
}
_TRIG_POS_MAP = {
    "TRIG_POS_CENTER": 0, "TRIG_POS_LEFT": 1, "TRIG_POS_RIGHT": 2,
}
_TRIG_SLOPE_MAP = {
    "TRIG_SLOPE_RISE": 0, "TRIG_SLOPE_FALL": 1, "TRIG_SLOPE_EITHER": 2,
}
_ACQUIRE_MAP = {
    "ACQUIRE_MODE_RUN": 0, "ACQUIRE_MODE_SINGLE": 1, "ACQUIRE_MODE_LOGGER": 2,
}
_LOAD_MAP = {
    "DEMO_CAL_LOAD_NONE":  0x000,
    "DEMO_CAL_LOAD_10_":   0x001,
    "DEMO_CAL_LOAD_20_":   0x002,
    "DEMO_CAL_LOAD_40_":   0x004,
    "DEMO_CAL_LOAD_100_":  0x100,
    "DEMO_CAL_LOAD_200_":  0x008,
    "DEMO_CAL_LOAD_400_":  0x200,
    "DEMO_CAL_LOAD_2K_":   0x010,
    "DEMO_CAL_LOAD_20K_":  0x020,
    "DEMO_CAL_LOAD_200K_": 0x040,
    "DEMO_CAL_LOAD_2M_":   0x080,
}


# ------------------------------------------------------------------ #
# Acquisition data return formats                                      #
# ------------------------------------------------------------------ #
# ACQ_FORMAT_LIST  - channels are Python lists of float (the default,
#                    matches the original P1150 contract, no numpy needed).
# ACQ_FORMAT_NUMPY - channels are float32 numpy arrays (zero per-element
#                    boxing; requires numpy to be installed).
ACQ_FORMAT_LIST  = "list"
ACQ_FORMAT_NUMPY = "numpy"
ACQ_FORMAT_LIST_ALL = [ACQ_FORMAT_LIST, ACQ_FORMAT_NUMPY]


# ------------------------------------------------------------------ #
# Helper: convert C acq struct → Python dict                           #
# ------------------------------------------------------------------ #
def _acq_to_dict(c_data: PxxxxAcqData, fmt: str = ACQ_FORMAT_LIST) -> dict:
    n = c_data.n_samples

    if fmt == ACQ_FORMAT_NUMPY:
        import numpy as np  # deferred: only numpy clients pay the dependency

        def arr(field):
            # Copy the first n samples straight out of the C buffer at C speed.
            # IMPORTANT: the channels are fixed-size ctypes arrays
            # (c_float * PXXXX_MAX_SAMPLES). np.ctypeslib.as_array(field,
            # shape=(n,)) IGNORES the shape arg for a fixed-size Array and
            # returns ALL PXXXX_MAX_SAMPLES, so the length must be bounded
            # explicitly - frombuffer(count=n) does that. copy() detaches from
            # the C buffer, which is reused after the callback returns. float32
            # matches the DLL's native c_float encoding (and the plot widgets).
            return np.frombuffer(field, dtype=np.float32, count=n).copy()

        out = {
            "t":    arr(c_data.t),
            "i":    arr(c_data.i),
            "isnk": arr(c_data.isnk),
            "a0":   arr(c_data.a0),
            "d0":   arr(c_data.d0),
            "d1":   arr(c_data.d1),
            # d0s: list of single-character strings, matching the P1150 contract
            "d0s":  list(np.frombuffer(c_data.d0s, dtype=np.uint8, count=n)
                         .tobytes().decode("latin-1")),
        }
        # Optional extra current streams: include only when the device actually
        # streamed them (has_i2/has_i3), so clients can distinguish "no i2" from
        # "i2 == 0". Absent -> key omitted entirely.
        if c_data.has_i2:
            out["i2"] = arr(c_data.i2)
        if c_data.has_i3:
            out["i3"] = arr(c_data.i3)
        return out

    out = {
        "t":    list(c_data.t[:n]),
        "i":    list(c_data.i[:n]),
        "isnk": list(c_data.isnk[:n]),
        "a0":   list(c_data.a0[:n]),
        "d0":   list(c_data.d0[:n]),
        "d1":   list(c_data.d1[:n]),
        "d0s":  [chr(c_data.d0s[k]) for k in range(n)],
    }
    if c_data.has_i2:
        out["i2"] = list(c_data.i2[:n])
    if c_data.has_i3:
        out["i3"] = list(c_data.i3[:n])
    return out


# ------------------------------------------------------------------ #
# PXXXX - main class                                                   #
# ------------------------------------------------------------------ #
class PXXXX:
    """
    Drop-in replacement for P1150.P1150, backed by the pxxxx C DLL.

    `port` selects the transport by its form:
      - a serial device path: "COM7", "/dev/ttyACM0"
      - an Ethernet URI: "tcp://192.168.1.50:9000" or "udp://192.168.1.16:9000"
        (default port 9000). Use list_net_devices() to get these; the scheme is
        the device's choice, not a preference.

    Usage example::

        from pxxxx import PXXXX, PxxxxAPI, get_port_from_sn

        port = get_port_from_sn(sn="XXXX")
        dev  = PXXXX(port=port,
                     cb_uclog_async=my_async_cb,
                     cb_acquisition_get_data=my_acq_cb)
        success, details = dev.ez_connect(calibrate=True)
    """

    @staticmethod
    def get_port_from_sn(sn=None, list_all=False):
        return get_port_from_sn(sn=sn, list_all=list_all)

    @staticmethod
    def list_ports(max_ports: int = 16) -> list[str]:
        """List every serial port with a PXXXX device attached."""
        return list_ports(max_ports=max_ports)

    @staticmethod
    def list_net_devices(timeout_ms: int = 1500, max_devs: int = 16) -> list[str]:
        """Discover Ethernet devices via mDNS; returns "tcp://" / "udp://" URIs."""
        return list_net_devices(timeout_ms=timeout_ms, max_devs=max_devs)

    @staticmethod
    def set_log_callback(logger=None) -> None:
        """Route the DLL's handle-less log output (discovery) into a logger."""
        set_log_callback(logger)

    @staticmethod
    def version() -> str:
        """Return the DLL's own version string, e.g. "0.1-main"."""
        return version()

    def __init__(self,
                 port: str = "COM1",
                 cb_uclog_async=None,
                 cb_acquisition_get_data=None,
                 logger=None,
                 acq_format: str = ACQ_FORMAT_LIST,
                 **_kw):
        # acq_format selects how acquisition channels are returned:
        #   ACQ_FORMAT_LIST  -> Python lists of float (default, no numpy)
        #   ACQ_FORMAT_NUMPY -> float32 numpy arrays (requires numpy)
        if acq_format not in ACQ_FORMAT_LIST_ALL:
            raise ValueError(
                f"acq_format must be one of {ACQ_FORMAT_LIST_ALL}, "
                f"got {acq_format!r}")

        self._lib  = _get_lib()
        self._lock = Lock()
        self._port = port
        self.connected = False
        self.logger    = logger
        self._acq_format = acq_format

        self._user_cb_async = cb_uclog_async
        self._user_cb_acq   = cb_acquisition_get_data

        # Create native device handle
        self._dev = self._lib.pxxxx_create()
        if not self._dev:
            raise MemoryError("pxxxx_create() returned NULL")

        # Wire up callbacks — keep references so GC doesn't collect them
        self._c_acq = CB_ACQ_T(self._on_acq)
        self._lib.pxxxx_set_cb_acq(self._dev, self._c_acq, None)

        self._c_async = CB_ASYNC_T(self._on_async)
        self._lib.pxxxx_set_cb_async(self._dev, self._c_async, None)

        if logger:
            self._c_log = CB_LOG_T(self._on_log)
            self._lib.pxxxx_set_cb_log(self._dev, self._c_log, None)
        else:
            self._c_log = None

        # Connect immediately if port provided
        rc = self._lib.pxxxx_connect(self._dev, port.encode())
        self.connected = (rc == 0)

    # ---- internal callbacks ---------------------------------------- #

    def _on_acq(self, c_data_ptr, _user):
        if c_data_ptr and self._user_cb_acq:
            d = _acq_to_dict(c_data_ptr.contents, self._acq_format)
            self._user_cb_acq(d)

    def _on_async(self, json_bytes, _user):
        if json_bytes and self._user_cb_async:
            try:
                data = json.loads(json_bytes.decode("utf-8", errors="replace"))
                self._user_cb_async(data)
            except Exception:
                pass

    def _on_log(self, level, msg_bytes, _user):
        if msg_bytes and self.logger:
            msg = msg_bytes.decode("utf-8", errors="replace")
            if level == 0:   self.logger.info(msg)
            elif level == 1: self.logger.warning(msg)
            else:            self.logger.error(msg)

    # ---- public API ------------------------------------------------ #

    def is_connected(self) -> bool:
        return bool(self._lib.pxxxx_is_connected(self._dev))

    def set_callbacks(self,
                      cb_acquisition_get_data=None,
                      cb_uclog_async=None,
                      acq_format: str = None) -> None:
        """Attach or replace the callbacks and data format after construction.

        This exists so a caller that opened a connection to identify a device
        can keep that same connection for the session instead of closing it and
        opening a new one. Over Ethernet the handover is not free: the device
        serves one client at a time and will not re-learn its peer for about a
        second, so reopening inside that window leaves it still answering the
        socket that just went away.

        The native callbacks are registered once in __init__ and dispatch
        through the attributes set here, so there is nothing to re-register.
        An argument left as None keeps its current value.
        """
        if acq_format is not None:
            if acq_format not in ACQ_FORMAT_LIST_ALL:
                raise ValueError(
                    f"acq_format must be one of {ACQ_FORMAT_LIST_ALL}, "
                    f"got {acq_format!r}")
            self._acq_format = acq_format
        if cb_acquisition_get_data is not None:
            self._user_cb_acq = cb_acquisition_get_data
        if cb_uclog_async is not None:
            self._user_cb_async = cb_uclog_async

    def close(self) -> None:
        with self._lock:
            if self._dev:
                self._lib.pxxxx_close(self._dev)
                self.connected = False

    def __del__(self):
        if hasattr(self, '_dev') and self._dev:
            self._lib.pxxxx_destroy(self._dev)
            self._dev = None

    # ---- ez_connect ------------------------------------------------ #

    def ez_connect(self, calibrate=True, progress_callback=None):
        """Connect, auto-download firmware if needed, calibrate.

        Signature intentionally mirrors P1150.ez_connect so p1150_hello.py
        needs only the 'calibrate' parameter (any truthy value = True).
        """
        result = PxxxxConnectResult()
        prog_c = None
        if progress_callback:
            def _prog(pct, msg_bytes, _u):
                progress_callback(pct, msg_bytes.decode() if msg_bytes else "")
            prog_c = CB_PROGRESS_T(_prog)

        with self._lock:
            rc = self._lib.pxxxx_ez_connect(
                self._dev,
                self._port.encode(),
                int(bool(calibrate)),
                prog_c or CB_PROGRESS_T(0),
                None,
                ctypes.byref(result)
            )

        if rc != 0:
            return False, {"ERROR": f"ez_connect failed (rc={rc})"}

        self.connected = True
        ping = result.ping
        st   = result.status
        details = {
            "app":         ping.app.decode(),
            "version":     ping.version.decode(),
            "serial":      ping.serial.decode(),
            "serial_hash": ping.serial_hash.decode(),
            "model":       ping.model.decode(),
            "hwver":       ping.hwver,
            "hs":          bool(ping.hs),
            "t_degc":      st.t_degc,
            "acquiring":   bool(st.acquiring),
            "vout":        st.vout,
            "cal_done":    bool(st.cal_done),
            "probe":       bool(st.probe),
            "ovc_ma":      st.ovc_ma,
            "err":         st.err,
            "err_act":     st.err_act,
            "uclog":       result.uclog.decode(),
        }
        return True, details

    # ---- ping ------------------------------------------------------ #

    def ping(self):
        with self._lock:
            p = PxxxxPing()
            rc = self._lib.pxxxx_ping(self._dev, ctypes.byref(p))
            if rc != 0: return False, None
            d = {
                "f":           "cmd_ping",
                "s":           True,
                "app":         p.app.decode(),
                "version":     p.version.decode(),
                "serial":      p.serial.decode(),
                "serial_hash": p.serial_hash.decode(),
                "model":       p.model.decode(),
                "hwver":       p.hwver,
                "hs":          bool(p.hs),
            }
            return True, [d]

    # ---- status ---------------------------------------------------- #

    def status(self):
        with self._lock:
            s = PxxxxStatus()
            rc = self._lib.pxxxx_status(self._dev, ctypes.byref(s))
            if rc != 0: return False, None
            d = {
                "f": "cmd_status", "s": True,
                "t_degc": s.t_degc, "acquiring": bool(s.acquiring),
                "vout": s.vout, "cal_done": bool(s.cal_done),
                "probe": bool(s.probe), "ovc_ma": s.ovc_ma,
                "err": s.err, "err_act": s.err_act,
            }
            return True, [d]

    # ---- vout_metrics ---------------------------------------------- #

    def vout_metrics(self):
        with self._lock:
            m = PxxxxVoutMetrics()
            rc = self._lib.pxxxx_vout_metrics(self._dev, ctypes.byref(m))
            if rc != 0: return False, None
            return True, [{"f": "cmd_vout_metrics", "s": True,
                           "max": m.max, "min": m.min, "step": m.step}]

    # ---- temperature_update ---------------------------------------- #

    def temperature_update(self):
        with self._lock:
            rc = self._lib.pxxxx_temperature_update(self._dev)
            return rc == 0, [{"f": "cmd_temp102_trigger", "s": rc == 0}]

    # ---- internal: channel-aware raw command send ------------------ #

    def _send_raw(self, payload: dict) -> int:
        """Send a flat command dict to the device via the raw pass-through
        (pxxxx_cmd) and return the DLL return code (0 == OK).

        Channel-aware commands use this so the integer "ch" field reaches the
        device unchanged: the DLL forwards the whole map and single-channel
        hardware (P1150/P1150D) simply ignores the extra field. This keeps
        multi-channel support entirely in the PXXXX class - no DLL change.
        """
        json_cmd = json.dumps(payload)
        rsp_buf = ctypes.create_string_buffer(4096)
        with self._lock:
            return self._lib.pxxxx_cmd(
                self._dev, json_cmd.encode(), rsp_buf, len(rsp_buf))

    # ---- set_vout -------------------------------------------------- #

    def set_vout(self, value_mv: int, ch: int = 1):
        # ch: 1-based channel (default 1); ignored by single-channel hardware
        rc = self._send_raw({"f": "cmd_vout", "mv": int(value_mv), "ch": int(ch)})
        return rc == 0, [{"f": "cmd_vout", "s": rc == 0}]

    # ---- set_vout_remote_sense ------------------------------------- #

    def set_vout_remote_sense(self, en: bool = False, ch: int = 1):
        # ch: 1-based channel (default 1); ignored by single-channel hardware
        rc = self._send_raw({"f": "cmd_vout_rs", "en": bool(en), "ch": int(ch)})
        return rc == 0, [{"f": "cmd_vout_rs", "s": rc == 0}]

    # ---- set_ovc --------------------------------------------------- #

    def set_ovc(self, value_ma: int, ch: int = 1):
        # ch: 1-based channel (default 1); ignored by single-channel hardware
        rc = self._send_raw({"f": "cmd_ovrcur", "ma": int(value_ma), "ch": int(ch)})
        return rc == 0, [{"f": "cmd_ovrcur", "s": rc == 0}]

    # ---- set_cal_load ---------------------------------------------- #

    def set_cal_load(self, loads: list = None):
        if loads is None:
            loads = [PxxxxAPI.DEMO_CAL_LOAD_NONE]
        mask = 0
        for load in loads:
            mask |= _LOAD_MAP.get(load, 0)
        with self._lock:
            rc = self._lib.pxxxx_set_cal_load(self._dev, ctypes.c_uint32(mask))
            return rc == 0, [{"f": "cmd_iload", "s": rc == 0}]

    # ---- set_cal_sweep --------------------------------------------- #

    def set_cal_sweep(self, sweep: bool = False):
        with self._lock:
            rc = self._lib.pxxxx_set_cal_sweep(self._dev, int(sweep))
            return rc == 0, [{"f": "cmd_iload_sweep", "s": rc == 0}]

    # ---- probe ----------------------------------------------------- #

    def probe(self, connect: bool = True, hard_connect: bool = False,
              rs_comp: bool = False, ch: int = 1):
        # ch: 1-based channel (default 1). Pass PxxxxAPI.PROBE_ALL_CHANNELS to
        # act on every channel at once. Ignored by single-channel hardware.
        rc = self._send_raw({"f":    "cmd_probe",
                             "v":    bool(connect),
                             "hard": bool(hard_connect),
                             "comp": bool(rs_comp),
                             "ch":   int(ch)})
        return rc == 0, [{"f": "cmd_probe", "s": rc == 0}]

    # ---- clear_error ----------------------------------------------- #

    def clear_error(self):
        with self._lock:
            rc = self._lib.pxxxx_clear_error(self._dev)
            return rc == 0, [{"f": "cmd_error_clear", "s": rc == 0}]

    # ---- led_blink ------------------------------------------------- #

    def led_blink(self):
        with self._lock:
            rc = self._lib.pxxxx_led_blink(self._dev)
            return rc == 0, [{"f": "cmd_led_blink", "s": rc == 0}]

    # ---- cal_status ------------------------------------------------ #

    def cal_status(self):
        with self._lock:
            cs = PxxxxCalStatus()
            rc = self._lib.pxxxx_cal_status(self._dev, ctypes.byref(cs))
            if rc != 0: return False, None
            d = {
                "f": "cmd_cal_status", "s": True,
                "cal_done": bool(cs.cal_done), "progress": cs.progress,
                "vout_set": cs.vout_set, "vout": cs.vout, "dacc": cs.dacc,
                "err": cs.err, "err_act": cs.err_act,
            }
            return True, [d]

    # ---- calibrate ------------------------------------------------- #

    def calibrate(self, force: bool = False, blocking: bool = True):
        with self._lock:
            rc = self._lib.pxxxx_calibrate(self._dev, int(force), int(blocking))
            return rc == 0, [{"f": "cmd_cal", "s": rc == 0}]

    # ---- set_timebase ---------------------------------------------- #

    def set_timebase(self, span: str):
        span_int = _TBASE_MAP.get(span)
        if span_int is None:
            return False, None
        with self._lock:
            rc = self._lib.pxxxx_set_timebase(self._dev, span_int)
            return rc == 0, None

    # ---- set_trigger ----------------------------------------------- #

    def set_trigger(self,
                    src: str = PxxxxAPI.TRIG_SRC_NONE,
                    pos: str = PxxxxAPI.TRIG_POS_LEFT,
                    slope: str = PxxxxAPI.TRIG_SLOPE_RISE,
                    level = 1,
                    ch: int = 1):
        # ch: 1-based channel (default 1). NOTE: the trigger is applied
        # client-side inside the DLL (pxxxx_set_trigger sends nothing to the
        # device), so ch is accepted for API uniformity/forward-compat but has
        # no effect yet. Per-channel triggering needs multi-channel ADC state
        # in the DLL once P3260 support lands.
        src_i   = _TRIG_SRC_MAP.get(src, 0)
        pos_i   = _TRIG_POS_MAP.get(pos, 1)
        slope_i = _TRIG_SLOPE_MAP.get(slope, 0)
        with self._lock:
            rc = self._lib.pxxxx_set_trigger(
                self._dev, src_i, pos_i, slope_i, ctypes.c_float(float(level)))
            return rc == 0, None

    # ---- acquisition_start ----------------------------------------- #

    def acquisition_start(self, mode: str):
        mode_i = _ACQUIRE_MAP.get(mode, 0)
        with self._lock:
            rc = self._lib.pxxxx_acquisition_start(self._dev, mode_i)
            return rc == 0, [{"f": "cmd_adc", "s": rc == 0}]

    # ---- acquisition_stop ------------------------------------------ #

    def acquisition_stop(self):
        with self._lock:
            rc = self._lib.pxxxx_acquisition_stop(self._dev)
            return rc == 0, [{"f": "cmd_adc", "s": rc == 0}]

    # ---- acquisition_complete -------------------------------------- #

    def acquisition_complete(self):
        trig = ctypes.c_int(0)
        rc = self._lib.pxxxx_acquisition_complete(self._dev, ctypes.byref(trig))
        return rc == 0, [{"triggered": bool(trig.value)}]

    # ---- acquisition_get_data -------------------------------------- #

    def acquisition_get_data(self):
        data = PxxxxAcqData()
        rc = self._lib.pxxxx_acquisition_get_data(self._dev, ctypes.byref(data))
        if rc != 0:
            return False, {"ERROR": "No data"}
        return True, _acq_to_dict(data, self._acq_format)

    # ---- bootloader ------------------------------------------------ #

    def bootloader_init(self):
        mtu = ctypes.c_int(256)
        rc = self._lib.pxxxx_bootloader_init(self._dev, ctypes.byref(mtu))
        return rc == 0, [{"f": "bl_init", "s": rc == 0, "mtu": mtu.value}]

    def bootloader_block(self, data: bytes):
        rc = self._lib.pxxxx_bootloader_block(self._dev, data, len(data))
        return rc == 0, [{"f": "bl_block", "s": rc == 0}]

    def bootloader_done(self):
        rc = self._lib.pxxxx_bootloader_done(self._dev)
        return rc == 0, [{"f": "bl_done", "s": rc == 0}]

    # ---- reset ----------------------------------------------------- #

    def reset(self, to_a70loader: bool = False) -> bool:
        """Reset the device; it reboots and re-enumerates over USB.

        to_a70loader=True reboots a P1150D into its a70 fallback loader.
        The connection is always closed (the device does not reply).
        """
        with self._lock:
            rc = self._lib.pxxxx_reset(self._dev, int(bool(to_a70loader)))
            self.connected = False
        return rc == 0

    # ---- cmd ------------------------------------------------------- #

    def cmd(self, cmd: dict) -> tuple[bool, list[dict] | None]:
        """Send raw command to target.

        :param cmd: {'f': 'cmd_*', 'key': value, ...}  (primitive values only)
        :return: success <True/False>, result <list[dict]|None>
        """
        json_cmd = json.dumps(cmd)
        rsp_buf = ctypes.create_string_buffer(4096)
        with self._lock:
            rc = self._lib.pxxxx_cmd(
                self._dev, json_cmd.encode(), rsp_buf, len(rsp_buf))
        if rc != 0:
            return False, None
        try:
            data = json.loads(rsp_buf.value.decode("utf-8"))
            return True, [data]
        except Exception:
            return False, None
