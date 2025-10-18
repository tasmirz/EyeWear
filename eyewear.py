from asyncio import subprocess
import multiprocessing.shared_memory as shm
from time import time

# create shared memory
ocr_shm = shm.SharedMemory(create=True, size=10**6, name='ocr_signal')
call_shm = shm.SharedMemory(create=True, size=4, name='call_signal')

import os
from dotenv import load_dotenv
load_dotenv()

BT_MAC = os.getenv("BT_MAC")

def is_bt_connected(mac):
    try:
        output = subprocess.check_output(["bluetoothctl", "info", mac]).decode()
        return "Connected: yes" in output
    except subprocess.CalledProcessError:
        return False
def cleanup():
    pass
def main():
    pass



# check if BT connected, if not try to connect, wait until connected
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting Earbud Emulator.")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        print("Exiting Earbud Emulator.")
        cleanup()