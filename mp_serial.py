# -*- coding: utf-8 -*-
"""
Sistemi Corporation, copyright, all rights reserved, 2024
Martin Guthrie

"""
from multiprocessing import Process, Event, get_logger
import queue
import serial
import logging
import logging.handlers as handlers
from threading import Thread
import timeit
import sys
import cobs
import os
logger = logging.getLogger()


class MySerialManager(Process):

  # see https://stackoverflow.com/questions/45977209/mixing-multiprocessing-and-serial-ports

  def __init__(self, serial_port, qin, qout, qcmd, timeout=0.01):
    super(MySerialManager, self).__init__(target=self.loop_iterator, args=(serial_port, timeout))
    # As soon as you uncomment this, you'll get an error.  TODO: Why?
    # self.ser = serial.Serial(serial_port, baudrate=baudrate, timeout=timeout)

    self._running = Event()
    self.logger = None

    self.q_in = qin
    self.q_out = qout
    self.q_cmd = qcmd

    self.alive = Event()
    self.alive.set()

    self._write_thread = None
    self._cmd_thread = None
    self._serial_port = None

    logger.info("starting")

  def is_running(self):
    return self._running.is_set()

  def loop_iterator(self, serial_port, timeout):

    self.logger = get_logger()

    if sys.platform.startswith("linux"):  # could be "linux", "linux2", "linux3", ...
        app_data_path = os.path.join(os.path.expanduser("~"), ".local/share/p1150")

    elif sys.platform == "darwin":
        app_data_path = os.path.join(os.path.expanduser("~"), "Library/Application Support/p1150")

    elif sys.platform == "win32":
        app_data_path = os.path.join(os.path.expanduser("~"), "AppData/Roaming/p1150")

    else:
        app_data_path = "./log"

    if not os.path.exists(app_data_path):
        os.makedirs(app_data_path)

    app_data_log_path = os.path.join(app_data_path, "log")
    logfile = os.path.join(app_data_log_path, "a48mp.log")

    FORMAT = "%(asctime)s: %(filename)22s %(funcName)25s %(levelname)-5.5s :%(lineno)4s: %(message)s"
    formatter = logging.Formatter(FORMAT)
    allLogHandler = handlers.RotatingFileHandler(logfile, maxBytes=1024 * 512, backupCount=4)
    allLogHandler.setLevel(logging.INFO)
    allLogHandler.setFormatter(formatter)
    self.logger.addHandler(allLogHandler)
    self.logger.setLevel(logging.INFO)

    self.logger.info(f"opening serial {serial_port}")
    try:
      ser = serial.Serial(serial_port, timeout=timeout)
      if sys.platform == 'win32':
        ser.set_buffer_size(1024 * 64, 1024 * 4)
        ser.reset_input_buffer()

    except Exception as e:
      self.logger.error(e)
      return

    self._serial_port = serial_port
    self._write_thread = Thread(None, self.writer, args=(ser,))
    self._write_thread.start()

    self._cmd_thread = Thread(None, self.cmdr)
    self._cmd_thread.start()

    self.loop(ser)

    self.logger.info("joining _write_thread")
    self._write_thread.join()
    self.logger.info("joining _cmd_thread")
    self._cmd_thread.join()

    self.logger.info("closing queues")
    self.q_in.close()
    self.q_out.close()
    self.q_cmd.close()

    self.logger.info(f"shutdown")

  def loop(self, ser):
    self.logger.info("starting serial read loop")

    self._running.set()

    indata = bytes()

    #start = timeit.default_timer()
    while self.alive.is_set():
      try:
        inw = ser.inWaiting()
        if inw > 0:
          #last_ser_read = timeit.default_timer()
          c = ser.read(inw)

        else:
          #last_ser_read = timeit.default_timer()
          c = ser.read(20)
          inw = ser.inWaiting()
          if inw > 0:
            #last_ser_read = timeit.default_timer()
            c += ser.read(inw)

        # self.send_pulse()

        # Process any new bytes received in `data`
        # indata is state (represents all currently unprocessed bytes
        indata = indata + c
        while b'\x00' in indata:
          frame, indata = indata.split(b'\x00', 1)
          try:
            if len(frame) > 0:
              self.q_out.put_nowait(cobs.dec(frame))

          except Exception as e:
            self.logger.error(e)

      except serial.serialutil.SerialException:
        self.logger.error("connection lost")
        # TODO: reconnect to serial?  See original ucLog Serial class
        #       But it may not make sense for P1150 to "reconnect"

      except Exception as e:
        self.logger.error(e)

    self.logger.info(f"closing serial port {self._serial_port}")
    ser.close()
    self.logger.info("Ended serial read loop")

  def writer(self, args):
    self.logger.info("Starting Writer thread")
    ser = args
    while self.alive.is_set():

      try:
        data = self.q_in.get(timeout=0.04)

        if data:
          #self.logger.info(f"serial writing {len(data)} bytes")
          ser.write(data)

      except queue.Empty:
        pass

      except Exception as e:
        self.logger.error(e)

    self.logger.info("Ended Writer thread")

  def cmdr(self):
    self.logger.info("Starting Cmdr thread")
    while self.alive.is_set():

      try:
        cmd = self.q_cmd.get(timeout=0.1)
        self.logger.info(cmd)

        if cmd == "SHUTDOWN":
          self.alive.clear()
          self._running.clear()

        else:
          self.logger.error(cmd)

      except queue.Empty:
        pass

    self.logger.info("Ended Cmdr thread")
