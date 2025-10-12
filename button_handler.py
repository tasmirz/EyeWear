import time
import Logger
import IPC
import signal

ipc=None
logger=None

DOUBLE_PRESS_INTERVAL = 0.5  # seconds
LONG_PRESS_THRESHOLD = 1
VERY_LONG_PRESS_THRESHOLD = 3

start = time.time()
last_press = time.time()


#handle press srtart
def button_press():
    global start
    start = time.time()

def button_release():
    global start
    length = time.time() - start
    handle_press(length)

# on sigusr1: start time
start = time.time()
signal.signal(signal.SIGUSR1, lambda signum, frame: setattr(globals(), 'start', time.time()))

def handle_press(length):
    global last_press
    current_time = time.time()
    # three conclusions: short press, long press, double press 

    if length >= LONG_PRESS_THRESHOLD:
        logger.log("Long press detected")
    elif current_time - last_press > DOUBLE_PRESS_INTERVAL:  # Debounce time
        logger.log("Double press detected")
    elif length < LONG_PRESS_THRESHOLD:
        logger.log("Short press detected")
    elif length >= VERY_LONG_PRESS_THRESHOLD:
        logger.log("Very long press detected")
    else:
        pass
    last_press = current_time


def main():
    global logger, ipc
    logger = Logger.Logger("button_handler")
    ipc = IPC.IPC("button_handler")
    logger.log("Button handler started.")