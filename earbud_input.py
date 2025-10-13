#!/usr/bin/env python3
"""
Checks for Bluetooth headphones input device and reads button events.
If not found, check periodically until found. (use bluetooth hooks to trigger)
Currently only supports play/pause, next, previous buttons.


mapping:
double tap -> play/pause
long press -> next
single tap -> previous

mode of operation:
|mode|button|operation|
|----|------|---------|
|idle  |double tap|ocr_client|
|idle  |long press|call_client|
|idle  |single tap|ignore|
|call  |double tap|hangup|
|call  |long press|ignore|
|call  |single tap|mute/unmute|
|ocr   |double tap|take new photo and ocr_client to queue|
|ocr   |long press|stop ocr|
|ocr   |single tap|pause ocr|


"""

import signal
import evdev
from evdev import categorize, ecodes
import sys
import time
import argparse
from multiprocessing.shared_memory import SharedMemory
import struct
from common import IPC

ipc = None
ocr_shm = SharedMemory(name="ocr_signal", size=4)


button_map = {
    200: "DOUBLE_TAP",   # playcd
    201: "DOUBLE_TAP",   # pausecd
    163: "LONG_PRESS",   # nextsong
    165: "SINGLE_TAP"    # previoussong
}

mode_of_operation = "IDLE" , # can be IDLE, CALL, OCR
getMode = lambda: mode_of_operation
setMode = lambda mode: globals().update(mode_of_operation=mode)


mode_mapping = {
    "IDLE": {
        "DOUBLE_TAP": "roc",
        "LONG_PRESS": "cc",
        "SINGLE_TAP": "IGNORE"
    },
    "CALL": {
        "DOUBLE_TAP": "h",
        "LONG_PRESS": "IGNORE",
        "SINGLE_TAP": "m"
    },
    "OCR": {
        "DOUBLE_TAP": "tn",
        "LONG_PRESS": "so",
        "SINGLE_TAP": "po"
    }
}



def run_ocr_client():
    setMode("OCR")
    ocr_shm.buf[:4] = struct.pack('i', 1)  # signal OCR client
    # send signal to OCR client process
    ipc.send_signal("ocr_client",signal.SIGUSR1)
    print("Running OCR client...")

def call_client():
    setMode("CALL")
    print("Calling client...")

def hangup():
    print("Hanging up call...")
    setMode("IDLE")

def mute_unmute():
    print("Toggling mute/unmute...")

def take_new_photo_and_ocr_client_to_queue():
    ocr_shm.buf[:4] = struct.pack('i', 3)  # signal take new photo and OCR
    ipc.send_signal("ocr_client",signal.SIGUSR1)
    print("Taking new photo and sending to OCR client queue...")

def stop_ocr():
    ocr_shm.buf[:4] = struct.pack('i', 2)  # signal stop OCR
    ipc.send_signal("ocr_client",signal.SIGUSR1)
    print("Stopping OCR...")
    setMode("IDLE")

def pause_ocr():
    ocr_shm.buf[:4] = struct.pack('i', 4)  # signal pause OCR
    ipc.send_signal("ocr_client",signal.SIGUSR1)
    print("Pausing OCR...")



fn_mapping = {
    "roc": run_ocr_client,
    "cc": call_client,
    "so": stop_ocr,
    "h": hangup,
    "m": mute_unmute,
    "tn": take_new_photo_and_ocr_client_to_queue,
    "po": pause_ocr
}



def find_bluetooth_device():
    """Find the Bluetooth input device"""
    while True:
        devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
        print("Available input devices:")
        for i, device in enumerate(devices):
            print(f"{i}: {device.path} - {device.name}")

        for device in devices:
            caps = device.capabilities()
            if ecodes.EV_KEY in caps:
                keys = caps[ecodes.EV_KEY]
                # device name does not have hdmi
                if "hdmi" in device.name.lower():
                    continue
                if any(k in keys for k in [ecodes.KEY_PLAYPAUSE, ecodes.KEY_NEXTSONG, 
                                           ecodes.KEY_PREVIOUSSONG, ecodes.KEY_PLAYCD]):
                    print(f"\nFound potential BT device: {device.name}")
                    print(f"Path: {device.path}")
                    return device
        print("No Bluetooth input device found. Retrying in 10 seconds...")
        time.sleep(10)
    return None
    
def read_button_events(device):
    """Read and process button events"""
    print(f"\nListening for button events on {device.name}...")
    print("Press buttons on your headphones (Ctrl+C to exit)\n")
    
    try:
        for event in device.read_loop():
            if event.type == ecodes.EV_KEY:
                key_event = categorize(event)
                if key_event.keystate == key_event.key_down:
                    print(event.code, 'pressed')
                    button = button_map.get(event.code, None)
                    if button:
                        action = fn_mapping[mode_mapping[getMode()].get(button, None)]
                        if action:
                            if action == "IGNORE":
                                pass
                            else:
                                action()
                    else:
                        print(f"Unmapped button code: {event.code}")
                        
    except KeyboardInterrupt:
        print("\nStopped listening.")   
    # handle if device is disconnected.
    except OSError as e:
        print(f"\nDevice disconnected: {e}")
        find_bluetooth_device()



if __name__ == "__main__":
    try:
        ipc = IPC("earbud_input_signal")
        device = find_bluetooth_device()
        if device:
            read_button_events(device)
        else:
            print("No Bluetooth input device found. Exiting.")
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        ocr_shm.close()
        ocr_shm.unlink()
        ipc.cleanup()