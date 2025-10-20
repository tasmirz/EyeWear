from asyncio import subprocess
import logging
from multiprocessing.shared_memory import SharedMemory
import multiprocessing
import subprocess
import threading
import time
import os
from dotenv import load_dotenv
load_dotenv()
from common import IPC, BluetoothProfileManager


TAGNAME = "eyewear"

logging.basicConfig(
    format=f"[{TAGNAME}] %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(TAGNAME)

# create shared memory
ocr_shm = None
call_shm = None
oqc_shm = None
oqi_shm = None

BT_MAC = os.getenv("BT_MAC")
bt_manager = BluetoothProfileManager(BT_MAC)
processes = []
ipc=None

def setup_shm():
    global ocr_shm, call_shm, oqc_shm, audio_feedback_shm
    ocr_shm = SharedMemory(create=True, size=4, name='ocr_signal')
    call_shm = SharedMemory(create=True, size=4, name='call_signal')
    oqc_shm = SharedMemory(create=True, size=4, name='ocr_queue_count')
    oqi_shm = SharedMemory(create=True, size=4, name='ocr_queue_images')

    ocr_shm.buf[0] = 0
    call_shm.buf[0] = 0
    oqc_shm.buf[0] = 0
    oqi_shm.buf[0] = 0

def run_program(command, stop_event):
    try:
        proc = subprocess.Popen(command)
        proc.wait()
    except Exception as e:
        logger.log(f"An error occurred while running {command}: {e}")
    finally:
        # Signal that a process has finished / failed
        stop_event.set()

def connect_bt():
    while not bt_manager.is_connected():
        logger.log("BT not connected. Trying to connect...")
        
        bt_manager.connect()
        time.sleep(2)

def observe_bt():
    while True:
        if not bt_manager.is_connected():
            time.sleep(40)
            if not bt_manager.is_connected():
                logger.log("BT disconnected. Restarting system to reconnect.")
                os.system("sudo bash restart_system.sh")
            else:
                continue
        time.sleep(20)

def main():
    #kill existing processes
    #read pid files and kill processes
    # os.system("kill $(cat /tmp/.pid/earbud_input_signal.pid) || true")
    # os.system("kill $(cat /tmp/.pid/call_client.pid) || true")
    # os.system("kill $(cat /tmp/.pid/ocr_process.pid) || true")
    # time.sleep(2)
    #remove pid files
    os.system("rm /tmp/.pid/earbud_input_signal.pid")
    os.system("rm /tmp/.pid/call_client.pid")
    os.system("rm /tmp/.pid/ocr_process.pid")
    setup_shm()
    #connect_bt()
    
    
    # run observer in a separate thread
    #observer_thread = threading.Thread(target=observe_bt)
    #observer_thread.start()
    
    stop_event = multiprocessing.Event()
    
    # main -> start ocr process,call client and input feeder
    #ocr_process = multiprocessing.Process(target=run_program, args=(["python3", "ocr_process.py"],stop_event))
    call_client_process = multiprocessing.Process(target=run_program, args=(["python3", "call_client.py"],stop_event))
    #input_feeder_process = multiprocessing.Process(target=run_program, args=(["python3", "earbud_input.py"],stop_event))

    
    # start processes
    #ocr_process.start()
    call_client_process.start()
    time.sleep(2)  # slight delay to ensure proper startup sequence
    #input_feeder_process.start()
    
    # if one process ends, terminate all, without while True loop
    stop_event.wait()
    
    print("One process ended or crashed. Terminating all...")

    # pause only for testing
    time.sleep(5000)


    # wait for signals
    #ocr_process.join()
    call_client_process.join()
    input_feeder_process.join()    
    
    # Terminate all processes
    # for p in processes:
    #     if p.is_alive():
    #         p.terminate()
    #         p.join() 
    # print("All processes terminated.")
    
    # # restart main
    # cleanup()

def cleanup():

    ocr_shm.close()
    ocr_shm.unlink()
    call_shm.close()
    call_shm.unlink()
    oqc_shm.close()
    oqc_shm.unlink()


# check if BT connected, if not try to connect, wait until connected
if __name__ == "__main__":
    try:
        ipc=IPC("main")
        main()
    except KeyboardInterrupt:
        print("\nExiting Earbud Emulator.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")
    finally:
        print("\nExiting Earbud Emulator.")
        cleanup()