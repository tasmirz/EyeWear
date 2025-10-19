from asyncio import subprocess
import multiprocessing.shared_memory as shm
import multiprocessing
import subprocess
import threading
import time
from time import time
import os
from dotenv import load_dotenv
load_dotenv()
from common import IPC, Logger, BluetoothManager

# create shared memory
ocr_shm = None
call_shm = None
oqc_shm = None
oqi_shm = None

BT_MAC = os.getenv("BT_MAC")
bt_manager = BluetoothManager(BT_MAC)
processes = []
ipc=None

def setup_shm():
    global ocr_shm, call_shm, oqc_shm, audio_feedback_shm
    ocr_shm = shm.SharedMemory(create=True, size=4, name='ocr_signal')
    call_shm = shm.SharedMemory(create=True, size=4, name='call_signal')
    oqc_shm = shm.SharedMemory(create=True, size=4, name='ocr_queue_count')
    oqi_shm = shm.SharedMemory(create=True, size=4, name='ocr_queue_images')

    ocr_shm.buf[0] = 0
    call_shm.buf[0] = 0
    oqc_shm.buf[0] = 0
    oqi_shm.buf[0] = 0

def run_program(command, stop_event):
    try:
        proc = subprocess.Popen(command)
        proc.wait()
    finally:
        # Signal that a process has finished / failed
        stop_event.set()

def connect_bt():
    while not bt_manager.is_connected():
        Logger.log("BT not connected. Trying to connect...")
        bt_manager.connect()
        time.sleep(2)

def observe_bt():
    while True:
        if not bt_manager.is_connected():
            time.sleep(40)
            if not bt_manager.is_connected():
                Logger.log("BT disconnected. Restarting system to reconnect.")
                os.system("sudo bash restart_system.sh")
            else:
                continue
        time.sleep(20)

def main():
    
    setup_shm()
    connect_bt()
    
    
    # run observer in a separate thread
    observer_thread = threading.Thread(target=observe_bt)
    observer_thread.start()
    
    # main -> start ocr process,call client and input feeder
    ocr_process = multiprocessing.Process(target=run_program, args=(["python3", "ocr_process.py"],))
    call_client_process = multiprocessing.Process(target=run_program, args=(["python3", "call_client.py"],))
    input_feeder_process = multiprocessing.Process(target=run_program, args=(["python3", "earbud_input.py"],))

    stop_event = multiprocessing.Event()
    
    # start processes
    ocr_process.start()
    call_client_process.start()
    time.sleep(5)  # slight delay to ensure proper startup sequence
    input_feeder_process.start()
    
    # if one process ends, terminate all, without while True loop
    stop_event.wait()
    
    print("One process ended or crashed. Terminating all...")

    # Terminate all processes
    for p in processes:
        if p.is_alive():
            p.terminate()
            p.join() 
    print("All processes terminated.")
    
    # restart main
    cleanup()
    main()

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
        print(f"An error occurred: {e}")
    finally:
        print("Exiting Earbud Emulator.")
        cleanup()