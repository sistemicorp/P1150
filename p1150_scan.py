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

A minimal program to scan for P1150 ports.  Note that non-P1150
devices using a serial port will also be subject to a 'ping' from this
program, which may cause some devices to error.

P1150 bootloader (a53) and application (a43) will respond to a 'ping'.
"""
import sys
from p1150_driver import P1150
import serial.tools.list_ports

# Must use __main__ due to multiprocessing within P1150 driver
if __name__ == '__main__':

    if sys.platform.startswith("linux"):
        ports_to_search = [p.device for p in serial.tools.list_ports.comports() if "ttyACM" in p.device]

    elif sys.platform == "darwin":
        ports_to_search = [p.device for p in serial.tools.list_ports.comports() if "cu.usb" in p.device]

    elif sys.platform == "win32":
        ports_to_search = [p.device for p in serial.tools.list_ports.comports()]

    else:
        print(f"Unknown platform {sys.platform}")
        ports_to_search = [p.device for p in serial.tools.list_ports.comports()]

    #print(f"ports_to_search {ports_to_search}")
    p1150s_found = {}

    for port in ports_to_search:

        try:
            p1150 = P1150.P1150(port=port)

        except Exception as e:
            print(e)
            print(f"{port} is not a P1150")
            p1150s_found[port] = {"s": False}  # s(uccess)
            continue

        # check if the P1125 is reachable
        success, result = p1150.ping()
        #print(f"Ping: {result}")
        if success:
            # attached device is a P1150, it responded to our PING, run some checks
            p1150s_found[port] = result[-1]

        else:
            #print(f"{port} is not a P1150")
            p1150s_found[port] = {"s": False}  # s(uccess)

        p1150.close()

    # print out found P1150s
    for port, p1150 in p1150s_found.items():
        if p1150["s"] is True:
            msg = f"P1150      : {port}, serial number {p1150['serial_hash']}"
            print(msg)
