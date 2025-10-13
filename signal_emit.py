import os
import signal
from multiprocessing.shared_memory import SharedMemory
import struct
from common import IPC


def send_signal(pid: int, action: int) -> None:
    os.kill(pid, signal.SIGUSR1)

if __name__ == "__main__":
    try:
        ipc = IPC("signal_emit")
        #target_pid = int(input("Enter the target process ID: "))
        action_code = int(input("Enter the action code (1-4): "))
        shm = SharedMemory(name="signal_test", create=False)
        shm.buf[:4] = struct.pack('i', action_code)
        ipc.send_signal("signal_test", signal.SIGUSR1)
        print(f"Sent signal to process with action code {action_code}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        shm.close()
        ipc.cleanup()