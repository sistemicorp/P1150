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

Report what a P1150 is running without connecting to it fully.  Both the
bootloader (a51) and the application (a43) will respond to a 'ping', so this is
a quick way to check a P1150 is present and see whether the application has
been loaded yet.

Unlike the other examples this script does not call ez_connect(), so the P1150
is left exactly as it was found.

NOTE: Always identify a P1150 by its serial number, found on the back of the
      unit.  Connecting by port name is supported but will be deprecated.

"""
import argparse
from pxxxx import PXXXX

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--sn", required=True, help="Serial Number for the P1150")
    args = parser.parse_args()

    port = PXXXX.get_port_from_sn(args.sn)
    if port is None:
        print(f"No P1150 found with serial number {args.sn}")
        exit(1)

    p1150 = PXXXX(port=port)

    # both a51 (bootloader) and a43 (application) answer a ping
    success, result = p1150.ping()
    if not success:
        print(f"{port}: found, but did not respond to a ping")
        p1150.close()
        exit(1)

    # command responses are a list, and almost always have only one item,
    # however its possible more responses are present, always just take last
    details = result[-1]

    print(f"P1150      : {port}, serial number {details['serial_hash']}")
    print(f"model      : {details['model']}")
    print(f"application: {details['app']}", end="")
    print("  (bootloader, the application is not loaded yet)"
          if details["app"] == "a51" else "")
    print(f"version    : {details['version']}")

    p1150.close()  # ALWAYS close
    exit(0)
