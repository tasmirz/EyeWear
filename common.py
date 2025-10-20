# create a pidfile named filename.pid in .pid directory, here filename is the name of the script
from fileinput import filename
import os
import atexit
import subprocess
import logging
import time
import signal
logging.basicConfig(level=logging.INFO)
from enum import Enum,auto

class SoundType(Enum):
    ZERO = 0
    ONE =1
    TWO = 2
    THREE = 3
    FOUR = 4
    FIVE = 5
    MANY = 6
    CALL_START = 7
    CALL_END = 8
    MUTED = 9
    UNMUTED = 10
    PHOTOS_ARE_PROCESSING = 11
    GENERATED_AUDIO = 12
    RUN_OCR_CLIENT = 13
    TAKE_NEW_PHOTO_AND_ADD_TO_OCR = 14
    PLEASE_TRY_AGAIN_LATER = 15
    STOP_OCR = 16
    PROCESSING = 17
    CALLING = 18
    IM_TAKING_DONE=19

class CallSignal(Enum):
    START_CALL = auto()
    END_CALL = auto()
    MUTE_CALL = auto()

class OCRSignal(Enum):
    START_OCR = auto()
    STOP_OCR = auto()
    PAUSE_OCR = auto()
    NEW_PICTURE = auto()
    STOP_OCR_NOW = auto()
class IPC:
    pid_file_location = "/tmp/.pid/"
    def __init__(self, filename):
        self.filename = filename
        self.pidfile = f"{self.pid_file_location}/{self.filename}.pid"
        if not os.path.exists(self.pidfile):
            self.create_pidfile(self.filename)
        else:
            logging.info(f"PID file {self.pidfile} already exists.")
            exit(1)

    def create_pidfile(self, filename):
        # Ensure the .pid directory exists
        if not os.path.exists(self.pid_file_location):
            os.makedirs(self.pid_file_location)
        pid = os.getpid()
        pidfile = f"{self.pid_file_location}/{filename}.pid"
        with open(pidfile, "w") as f:
            f.write(str(pid))
        # Register cleanup function
        atexit.register(self.cleanup)

    # WILL DO LATER:
# while sending signal, check if the pid exists in dictionary {
# if exists and is a python process, send signal}
# else run the process by appending .py to the filename (if not already exists)
# if does not exist in dictionary but the pidfile exists and is a python process, cache in a dictionary
# 
    def read_pid(self, filename):
        pidfile = f"{self.pid_file_location}/{filename}.pid"
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
    def cleanup(self): 
        if os.path.exists(self.pidfile):
            os.remove(self.pidfile)
            logging.info(f"Removed PID file {self.pidfile}.")

        
        
class BluetoothProfileManager:
    """Minimal manager for bluealsa + BlueZ flows"""
    def __init__(self, bt_mac, settle=1.0):
        self.bt_mac = bt_mac
        self.current_profile = None
        self.settle = settle

    def _run(self, cmd, timeout=5):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except subprocess.TimeoutExpired:
            return 1, "", "timeout"
    def connect(self):
        """Connect to device via bluetoothctl"""
        rc, out, err = self._run(["bluetoothctl", "connect", self.bt_mac], timeout=10)
        if rc == 0:
            time.sleep(self.settle)
            return True
        return False
    def disconnect(self):
        """Disconnect device via bluetoothctl"""
        rc, out, err = self._run(["bluetoothctl", "disconnect", self.bt_mac], timeout=5)
        if rc == 0:
            time.sleep(self.settle)
            return True
        return False
    def is_connected(self):
        """Check if device is connected via bluetoothctl"""
        rc, out, err = self._run(["bluetoothctl", "info", self.bt_mac])
        return rc == 0 and "Connected: yes" in out
    
    def ensure_connected(self):
        """Ensure device is connected via bluetoothctl"""
        rc, out, err = self._run(["bluetoothctl", "info", self.bt_mac])
        if rc != 0 or "Connected: yes" not in out:
            rc, out, err = self._run(["bluetoothctl", "connect", self.bt_mac], timeout=10)
            if rc == 0:
                time.sleep(self.settle)
                return True
            return False
        return True

    def _reconnect(self):
        """Disconnect then connect to force profile negotiation"""
        self._run(["bluetoothctl", "disconnect", self.bt_mac], timeout=5)
        time.sleep(0.5)
        rc, out, err = self._run(["bluetoothctl", "connect", self.bt_mac], timeout=10)
        time.sleep(self.settle)
        return rc == 0

    def switch_to_sco(self):
        """Request reconnect for SCO/HFP"""
        try:
            print("🔄 Requesting reconnect for SCO/HFP...")
            if not self.ensure_connected():
                print("⚠ Device not connected; attempting connect")
            ok = self._reconnect()
            if ok:
                self.current_profile = "sco"
                print("✅ Reconnected; expect SCO devices available")
                return True
            else:
                print("⚠ Failed to reconnect Bluetooth device")
                return False
        except Exception as e:
            print(f"❌ Error switching to SCO: {e}")
            return False

    def switch_to_a2dp(self):
        """Request reconnect for A2DP"""
        try:
            print("🔄 Requesting reconnect for A2DP...")
            ok = self._reconnect()
            if ok:
                self.current_profile = "a2dp"
                print("✅ Reconnected; expect A2DP devices available")
                return True
            else:
                print("⚠ Failed to reconnect Bluetooth device")
                return False
        except Exception as e:
            print(f"❌ Error switching to A2DP: {e}")
            return False
        
def  getMode():
    # read file /tmp/.mode_of_operation/mode.txt
    mode_file = "/tmp/.mode_of_operation/mode.txt"
    try:
        with open(mode_file, "r") as f:
            mode = f.read().strip()
            return mode
    except FileNotFoundError:
        return "IDLE"
def setMode(mode):
    mode_file = "/tmp/.mode_of_operation/mode.txt"
    # create directory if not exists
    os.makedirs(os.path.dirname(mode_file), exist_ok=True)
    with open(mode_file, "w") as f:
        f.write(mode)



