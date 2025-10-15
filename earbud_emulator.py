#!/usr/bin/env python3
import atexit
import sys

"""
Earbud Emulator: Terminal interface to simulate earbud button presses.
Displays current mode in prompt and calls corresponding actions.
"""

from earbud_input import (
    fn_mapping,
    getMode,
    set_ipc,
    shared_memory_cleanup
)
from common import IPC

def cleanup():
    global ipc;
    shared_memory_cleanup()
    ipc.cleanup()
def main():
    try:
        ipc = IPC("emulator")  # Create an IPC instance for the emulator
        set_ipc(ipc)  # Set the ipc instance in earbud_input module
        # we don't need to think about button codes here, just the actions
        print("Earbud Emulator Started. Type 'exit' to quit.")

        while True:
            # use the mode_mapping to get the actions, take input
            user_input = input(f"[{getMode()}] Enter action (cc, roc, h, m, tn, so, po): ").strip().lower()
            if user_input == "exit":
                print("Exiting Earbud Emulator.")
                break
            elif user_input in fn_mapping:
                fn_mapping[user_input]()  # Call the corresponding function
            else:
                print("Invalid input. Please try again.")
    except KeyboardInterrupt:
        
        print("\nExiting Earbud Emulator.")


if __name__ == "__main__":
    atexit.register(cleanup)
    main()