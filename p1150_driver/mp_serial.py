# -*- coding: utf-8 -*-
"""
Sistemi Corporation, copyright, all rights reserved, 2024-2025
Martin Guthrie

"""
import mp_serial_ext


class MySerialManager:
    """
    Drop-in wrapper around mp_serial_ext.SerialManager

    Constructor (timeout removed; non-blocking reads are used internally):
      MySerialManager(serial_port, qin, qout, *, baud=115200)

    Notes:
      - Baud defaults to 115200 (can be adjusted via 'baud' kwarg).
      - Call start() to launch I/O threads.
      - To stop, call shutdown().
    """

    def __init__(self, serial_port: str, qin, qout, *, baud: int = 115200):
        self._qin = qin
        self._qout = qout
        self._impl = mp_serial_ext.SerialManager(serial_port, qin, qout, baud=baud)

    def start(self) -> None:
        self._impl.start()

    def is_running(self) -> bool:
        return self._impl.is_running()

    def shutdown(self) -> None:
        # Immediate native shutdown; no command queue signaling
        self._impl.shutdown()

