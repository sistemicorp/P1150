# -*- coding: utf-8 -*-
"""
MIT License

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

P1150 bootloader (a51) and application (a43) will respond to a 'ping'.

NOTE: This script is deprecated since support to connect via serial number
      is implemented.  This script will be removed in a future release.

"""
import sys
from p1150_driver import P1150
import serial.tools.list_ports

ports_to_search = P1150.get_port_from_sn(None, list_all=True)

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
