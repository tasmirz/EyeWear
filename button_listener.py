"""
interrupt button listener
    falling edge detection on GPIO pin 17
    rising edge detection on GPIO pin 17
"""
import RPi.GPIO as GPIO
import time
import sys
import argparse
import common.logger as Logger
import common.create_pidfile as create_pidfile
import common.ipc as IPC


TAG="button_listener"
button_handler_tag = "button_handler"
logger=None
ipc=None


def button_rising_callback():
    logger.log(f"Button released (rising edge detected)")
    ipc.send_signal(button_handler_tag, "SIGUSR1")

def button_falling_callback():
    logger.log(f"Button pressed (falling edge detected)")
    ipc.send_signal(button_handler_tag, "SIGUSR2")

def setup_gpio(pin=17):
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.add_event_detect(pin, GPIO.FALLING, callback=button_falling_callback, bouncetime=200)
    GPIO.add_event_detect(pin, GPIO.RISING, callback=button_rising_callback, bouncetime=200)

def emulate_signal():
    DOUBLE_PRESS_INTERVAL = 0.5  # seconds
    LONG_PRESS_THRESHOLD = 1
    VERY_LONG_PRESS_THRESHOLD = 3

    parser = argparse.ArgumentParser(description="Button Listener")
    parser.add_argument("--send-single", action="store_true", help="Send single press signal")
    parser.add_argument("--send-double", action="store_true", help="Send double press signal")
    parser.add_argument("--send-long", action="store_true", help="Send long press signal")
    parser.add_argument("--send-very-long", action="store_true", help="Send very long press signal")
    args = parser.parse_args()


    if args.send_single:
        logger.log("Emulating single press")
        button_rising_callback()
        time.sleep(0.1)  # simulate short press duration
        button_falling_callback()

    elif args.send_double:
        logger.log("Emulating double press")
        button_rising_callback()
        time.sleep(0.1)  # simulate short press duration
        button_falling_callback()
        time.sleep(DOUBLE_PRESS_INTERVAL / 2)  # within double press interval
        button_rising_callback()
        time.sleep(0.1)  # simulate short press duration
        button_falling_callback()
    elif args.send_long:
        logger.log("Emulating long press")
        button_rising_callback()
        time.sleep(LONG_PRESS_THRESHOLD + 0.1)  # simulate long press duration
        button_falling_callback()
    elif args.send_very_long:
        logger.log("Emulating very long press")
        button_rising_callback()
        time.sleep(VERY_LONG_PRESS_THRESHOLD + 0.1)  # simulate very long press duration
        button_falling_callback()
    else:
        return False
    return True
    

def main():
    global logger, ipc
    if emulate_signal():
        sys.exit(0)
    logger = Logger.Logger(TAG)
    setup_gpio()
    
    logger.log("Button listener started. Press Ctrl+C to exit.")
    ipc = IPC.IPC(TAG)


if __name__ == "__main__":
    # allow args -> --send-single, --send-double, --send-long, --send-very-long
    try:
        main()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.log("Exiting button listener.")
    finally:
        GPIO.cleanup()
        sys.exit(0)