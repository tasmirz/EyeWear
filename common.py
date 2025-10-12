# create a pidfile named filename.pid in .pid directory, here filename is the name of the script
from fileinput import filename
import os
import atexit
import logging
import time
import signal
logging.basicConfig(level=logging.INFO)




class IPC:
    def __init__(self, filename):
        self.filename = filename
        self.pidfile = f".pid/{self.filename}.pid"
        if not os.path.exists(self.pidfile):
            self.create_pidfile(self.filename)
        else:
            logging.info(f"PID file {self.pidfile} already exists.")
            exit(1)

    def create_pidfile(self, filename):
        # Ensure the .pid directory exists
        if not os.path.exists(".pid"):
            os.makedirs(".pid")
        pid = os.getpid()
        pidfile = f".pid/{filename}.pid"
        with open(pidfile, "w") as f:
            f.write(str(pid))
        # Register cleanup function
        atexit.register(lambda: os.remove(pidfile))

    # WILL DO LATER:
# while sending signal, check if the pid exists in dictionary {
# if exists and is a python process, send signal}
# else run the process by appending .py to the filename (if not already exists)
# if does not exist in dictionary but the pidfile exists and is a python process, cache in a dictionary
# 
    def read_pid(self, filename):
        pidfile = f".pid/{filename}.pid"
        try:
            with open(pidfile, "r") as f:
                pid = int(f.read().strip())
            return pid
        except FileNotFoundError:
            logging.error(f"PID file {pidfile} not found.")
            # wait till the file is created.
            while not os.path.exists(pidfile):
                logging.info(f"Waiting for PID file {pidfile} to be created...")
                time.sleep(1)
            return self.read_pid(filename) 
        except ValueError:
            logging.error(f"Invalid PID in file {pidfile}.")
            return None

    def send_signal(self, process_name, signal):
        pid = self.read_pid(process_name)
        if pid:
            try:
                os.kill(pid, signal)
                logging.info(f"Sent signal {signal} to process {process_name} with PID {pid}.")
            except ProcessLookupError:
                logging.error(f"No process found with PID {pid}.")
            except PermissionError:
                logging.error(f"Permission denied to send signal to PID {pid}.")
        else:
            logging.error(f"Could not find PID for process {process_name}.")

class Logger:
    def __init__(self, tag):
        self.tag = tag
        logging.basicConfig(level=logging.INFO)

    def log(self, msg):
        print(f"[{self.tag}] {msg}")