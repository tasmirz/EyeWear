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


import logging


TAGNAME = "earbud_input"

logging.basicConfig(
    format=f"[{TAGNAME}] %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(TAGNAME)

import signal
import evdev
from evdev import categorize, ecodes
import sys
import time
import argparse
from multiprocessing.shared_memory import SharedMemory
import struct
from common import IPC, CallSignal, OCRSignal,SoundType 
#from audio_feedback import AudioFeedbackManager


ipc = None
muted=False
ocr_shm = SharedMemory(name="ocr_signal", create=False, size=4)
call_shm = SharedMemory(name="call_signal", create=False, size=4)
oqc_shm = SharedMemory(name="ocr_queue_count", create=False, size=4)
oqi_shm = SharedMemory(name="ocr_queue_images", create=False, size=4)

def set_ipc(ipc_instance):
    global ipc
    ipc = ipc_instance
def get_ipc():
    global ipc
    return ipc
button_map = {
    200: "DOUBLE_TAP",   # playcd
    201: "DOUBLE_TAP",   # pausecd
    163: "LONG_PRESS",   # nextsong
    165: "SINGLE_TAP"    # previoussong
}

mode_of_operation = 'IDLE' # can be IDLE, CALL, OCR
def  getMode():
    return globals().get('mode_of_operation', 'IDLE')
setMode = lambda mode: globals().update(mode_of_operation=mode)

setMode("IDLE")


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
    #afm.play(SoundType.RUN_OCR_CLIENT, threaded=False)
    ocr_shm.buf[:4] = struct.pack('i', OCRSignal.START_OCR.value)  # signal OCR client
    # send signal to OCR client process
    ipc.send_signal("ocr_process",signal.SIGUSR1)
    logger.info("Running OCR client...")

def call_client():
    setMode("CALL")
    global muted
    muted=False
    #afm.play(SoundType.CALL_START, threaded=False)
    call_shm.buf[:4] = struct.pack('i', CallSignal.START_CALL.value)  # signal call client
    ipc.send_signal("call_client",signal.SIGUSR1)
    logger.info("Calling client...")

def hangup():
    logger.info("Hanging up call...")
    #afm.play(SoundType.CALL_END, threaded=True)
    call_shm.buf[:4] = struct.pack('i', CallSignal.END_CALL.value)  # signal hangup
    ipc.send_signal("call_client",signal.SIGUSR1)
    setMode("IDLE")

def mute_unmute():
    global muted
    muted = not muted
    if muted:
        #afm.play(SoundType.MUTED, threaded=True)
        pass
    else:
        #afm.play(SoundType.UNMUTED, threaded=True)
        pass
    call_shm.buf[:4] = struct.pack('i', CallSignal.MUTE_CALL.value)  # signal mute/unmute
    ipc.send_signal("call_client",signal.SIGUSR1)
    logger.info("Toggling mute/unmute...")

def take_new_photo_and_ocr_client_to_queue():
    #afm.play(SoundType.TAKE_NEW_PHOTO_AND_ADD_TO_OCR, threaded=True)
    ocr_shm.buf[:4] = struct.pack('i', OCRSignal.NEW_PICTURE.value)  # signal take new photo and OCR
    ipc.send_signal("ocr_process",signal.SIGUSR1)
    logger.info("Taking new photo and sending to OCR client queue...")

def stop_ocr():
    ocr_shm.buf[:4] = struct.pack('i', OCRSignal.STOP_OCR.value)  # signal stop OCR
    ipc.send_signal("ocr_process",signal.SIGUSR1)
        

def pause_ocr():
   # afm.play(SoundType.PAUSE_OCR, threaded=True)
    ocr_shm.buf[:4] = struct.pack('i', OCRSignal.PAUSE_OCR.value)  # signal pause OCR
    ipc.send_signal("ocr_process",signal.SIGUSR1)
    logger.info("Pausing OCR...")


def ocr_queue_feedback(): # must be registered as a signal handler
    count = struct.unpack('i', oqc_shm.buf[:4])[0]
    logger.info(f"OCR Queue Count: {count}")
    if count == 0:
        #afm.play(SoundType.STOP_OCR, threaded=True)
        setMode("IDLE")
        logger.info("Stopping OCR...")
        ocr_shm.buf[:4] = struct.pack('i', OCRSignal.STOP_OCR_NOW.value)  # signal stop OCR now
        ipc.send_signal("ocr_process",signal.SIGUSR1)
    else:
        # audio feedback -> how many audio in queue
        audio_count = struct.unpack('i', oqi_shm.buf[:4])[0]
        logger.info(f"OCR Queue Images: {audio_count}")
        

def ocr_mute_feedback(): # must be registered as a signal handler
    logger.info("OCR Mute/Unmute feedback received.")
    queue_count = struct.unpack('i', oqc_shm.buf[:4])[0]
    queue_count_images = struct.unpack('i', oqi_shm.buf[:4])[0]
    logger.info(f"Playing sound for OCR Queue Count: {queue_count}")
    logger.info(f"OCR Queue Images for Mute/Unmute: {queue_count_images}")

    if (queue_count>=6):
        # afm.sequential_play([
        #     SoundType.MANY,
        #     SoundType.GENERATED_AUDIO], 
        #                     threaded=False)
        pass
    elif (queue_count==0):
        pass
    else:
        # afm.sequential_play([
        #     SoundType(queue_count),
        #     SoundType.GENERATED_AUDIO                                                                                   
        #     ], threaded=False)
        pass                                                                                                                       
    if (queue_count_images >=6):
        # afm.sequential_play([
        #     SoundType.MANY,
        #     SoundType.PHOTOS_ARE_PROCESSING], 
        #                     threaded=True)
        pass
    elif (queue_count_images==0):
        pass
    else:
        # afm.sequential_play([
        #     SoundType(queue_count_images),
        #     SoundType.PHOTOS_ARE_PROCESSING                                                                                   
        #     ], threaded=True)
        pass
fn_mapping = {
    "roc": run_ocr_client,
    "cc": call_client,
    "so": stop_ocr,
    "h": hangup,
    "m": mute_unmute,
    "tn": take_new_photo_and_ocr_client_to_queue,
    "po": pause_ocr,
    "IGNORE": None
}



def find_bluetooth_device():
    """Find the Bluetooth input device"""
    while True:
        devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
        logger.info("Available input devices:")
        for i, device in enumerate(devices):
            logger.info(f"{i}: {device.path} - {device.name}")

        for device in devices:
            caps = device.capabilities()
            if ecodes.EV_KEY in caps:
                keys = caps[ecodes.EV_KEY]
                # device name does not have hdmi
                if "hdmi" in device.name.lower():
                    continue
                if any(k in keys for k in [ecodes.KEY_PLAYPAUSE, ecodes.KEY_NEXTSONG,
                                           ecodes.KEY_PREVIOUSSONG, ecodes.KEY_PLAYCD]):
                    logger.info(f"\nFound potential BT device: {device.name}")
                    logger.info(f"Path: {device.path}")
                    return device

        logger.warning("No Bluetooth input device found. Retrying in 10 seconds...")
        time.sleep(10)
    
def read_button_events(device):
    """Read and process button events"""
    logger.info(f"\nListening for button events on {device.name}...")
    logger.info("Press buttons on your headphones (Ctrl+C to exit)\n")
    
    try:
        for event in device.read_loop():
            if event.type == ecodes.EV_KEY:
                key_event = categorize(event)
                if key_event.keystate == key_event.key_down:
                    logger.info(f"{event.code} pressed")
                    button = button_map.get(event.code, None)
                    logger.info(f"Button mapped to: {button}")
                    if button:
                        action = fn_mapping[mode_mapping[getMode()].get(button, None)]
                        if action:
                            if action == "IGNORE":
                                pass
                            else:
                                action()
                    else:
                        logger.warning(f"Unmapped button code: {event.code}")
    # handle if device is disconnected.
    except OSError as e:
        logger.exception(f"Device disconnected: {e}")
        find_bluetooth_device()

def shared_memory_cleanup():
    global ocr_shm, call_shm
    logger.info("Cleaning up shared memory...")
    #ocr_shm.close()
    #self.create_pipeline_rpicamocr_shm.close()
    call_shm.close()

if __name__ == "__main__":
    try:
        ipc = IPC("earbud_input_signal")
        signal.signal(signal.SIGUSR1, ocr_queue_feedback)
        signal.signal(signal.SIGUSR2, ocr_mute_feedback)
        device = find_bluetooth_device()
        if device:
            read_button_events(device)
        else:
            logger.error("No Bluetooth input device found. Exiting.")
            shared_memory_cleanup()
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\nExiting Earbud Input Listener.")
    except Exception as e:
        logger.exception(f"Error: {e}")
    finally:
        shared_memory_cleanup()
        ipc.cleanup()