#! /usr/bin/env python3

# © 2022 Unit Circle Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import threading
import queue
import struct
import time
import logging
import cbor2
import cobs

from logdata import LogData, TARGET_DIGIT_SHIFT, LOG_TYPE_PORT

logging.getLogger()

# Set the largest port/stream that is supported by python side
# This is driven by `ulimit -n`.  Default on macOS is 256 which prevents
# using 64.  Pratically the an application is not likely to use more than 8.
LOG_PORT_MAX = 8

class CobsEncode(object):
  def __init__(self):
    self.on_data = None

  def __call__(self, data):
    if self.on_data:
      self.on_data(b'\x00' + cobs.enc(data) +b'\x00')


class CborDecode(object):
  def __init__(self):
    self.on_data = None

  def __call__(self, data):
    try:
      dec = cbor2.loads(data)
    except cbor2.CBORDecodeError:
      return
    if self.on_data:
      self.on_data(dec)


class MuxDecode(object):
  def __init__(self, on_data):
    self.on_data = on_data

  def __call__(self, frame):
    # The instance check was added when cobs was changed to a c implementation
    # which exposed a problem where STM32 side was sending 4 extra bytes of
    # garbage data, and this instance check avoid choking on the garbage data.
    # BECAUSE the STM32 problem is in the bootloader (A51) and devices are
    # already in the field, this check is required.
    if not isinstance(frame, bytes) or len(frame) == 0:
      return
    p, t = divmod(int(frame[0]),4)
    if t == LOG_TYPE_PORT:
      if p in self.on_data:
        self.on_data[p](frame[1:])
    elif len(frame) >= 4:
      addr, frame = struct.unpack('<I', frame[:4])[0], frame[4:]
      target = (addr >> TARGET_DIGIT_SHIFT) & 0xf
      if 'log' in self.on_data:
        self.on_data['log']((target, addr, frame))
    else:
      if 'error' in self.on_data:
        self.on_data['error'](frame)


class MuxEncode(object):
  def __init__(self, port):
    self.port = port
    self.on_data = None

  def __call__(self, frame):
    if self.on_data:
      self.on_data(bytes(((self.port<<2)|LOG_TYPE_PORT,))+frame)

class LogDecode(object):
  def __init__(self, dec):
    self.dec = dec
    self.on_data = None

  def __call__(self, item):
    try:
      target, _, _ = item
      if target in self.dec:
        r = self.dec[target].decode(item)
      else:
        r = item
    except Exception as e:
      logging.error("exception ",exc_info=1)
      r = item
    if self.on_data:
      self.on_data(r)


from multiprocessing import Queue
from mp_serial import MySerialManager

class Serial(threading.Thread):
  def __init__(self, dev):
    threading.Thread.__init__(self)
    self.dev = dev
    self.on_data = None
    self.alive = True
    self.q_out = Queue()
    self.q_in = Queue()

    logging.info("MySerialManager creating instance")
    self.msm = MySerialManager(self.dev, self.q_in, self.q_out)
    logging.info("MySerialManager starting threads")
    self.msm.start()
    logging.info(f"MySerialManager is_running: {self.msm.is_running()}")

    self.lock = threading.Lock()
    self.last_send = time.time() - 1
    self.cnt = 0
    self.start()

  def shutdown(self):
    logging.info("shutting down")
    if self.alive:
      self.alive = False

      if self.msm and self.msm.is_running():
        self.msm.shutdown()
        self.msm = None
        logging.info("shutting down MySerialManager - joined")
      else:
        logging.info("shutting down MySerialManager - already done")

      self.join(timeout=1)
      logging.info("shutting down self - joined")

    self.q_out.close()
    self.q_in.close()

    logging.info("shutdown")

  def __call__(self, data):
    # Ensure each "packet" is fully sent before the next one
    with self.lock:
      self.last_send = time.time()

      # STM32 USB DMA CDC requires 4x multiple bytes, add padding
      sl = len(data) % 4
      if sl: data += (bytes([0] * (4 - sl)))

      self.q_in.put_nowait(data)

  def send_pulse(self):
    if time.time() >= self.last_send + .5:
      self(b'\x00\x00\x00\x00')

  def run(self):
      while self.alive and self.msm.is_running():
        try:
          frame = self.q_out.get(timeout=0.01)
          if self.on_data:
            self.on_data(frame)
        except queue.Empty:
          pass
        except Exception as e:
          logging.error(f"exception {e}", exc_info=1)

      # Ensure native shutdown if still present
      if self.msm and self.msm.is_running():
        logging.info("shutting down MySerialManager")
        self.msm.shutdown()
        self.msm = None

      logging.info("Serial run loop stopped")



# Utitlity to chain a list of processes together
def chain(items):
  for src, dst in zip(items[:-1], items[1:]):
    src.on_data = dst
  return items[0]


class Target(threading.Thread):
  def __init__(self, target):
    threading.Thread.__init__(self)
    self.threads = {}
    self.alive = True
    self.threads['serial'] = Serial(target)
    self.init()
    self.start()

  def init(self):
    pass

  def shutdown(self):
    logging.info("starting shutdown")
    self.alive = False;
    for _, thread in self.threads.items():
      thread.shutdown()
    logging.info("shutdown complete")

  def run(self):
    while self.alive:
      time.sleep(.1)
      if any([not thread.is_alive() for _, thread in self.threads.items()]):
        break

    logging.info("run loop stopped")


class LogClientServer(Target):
  def __init__(self, target, decoders, rx):
    self.decoders = decoders
    self.rx = rx
    Target.__init__(self, target)

  def start(self):
    try:
      self.tx = {
            i: chain([MuxEncode(i), CobsEncode(), self.threads['serial']])
            for i in range(LOG_PORT_MAX)
          }
      rx = self.rx.copy()
      if 'log' in rx:
        self.rx['log'] = chain([LogDecode(self.decoders), rx['log']])
      self.rx = chain([self.threads['serial'], MuxDecode(self.rx)])
    except Exception as e:
      self.shutdown()
      raise e

  def ready(self):
    return True

  def __getitem__(self, key):
    '''
    Returns a callable to allow sending data to a stream
    '''
    return self.tx[key]


def decoders(fnames):
  dec = [LogData(f) for f in fnames]
  return {d.target(): d for d in dec}

