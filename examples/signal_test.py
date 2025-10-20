import signal
import os
from multiprocessing.shared_memory import SharedMemory
import struct
shm = SharedMemory(name="signal_test", create=True, size=4)
from common import IPC

def signal_handler(a,b):
    print(f"Signal handler called with signal {a}, {b}")
    action_code = struct.unpack('i', shm.buf[:4])[0]
    print(f"Action code from shared memory: {action_code}")

signal.signal(signal.SIGUSR1, signal_handler)

if __name__ == "__main__":
    try:
        ipc = IPC("signal_test")
        # print the current process id
        print(f"Process ID: {os.getpid()}")
        print("Waiting for signals...")
        signal.pause()  # Wait for signals indefinitely
    except Exception as e:
        print(f"Error: {e}")
    finally:
        shm.close()
        ipc.cleanup()