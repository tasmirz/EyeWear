import sys

#!/usr/bin/env python3
"""
Earbud Emulator: Terminal interface to simulate earbud button presses.
Displays current mode in prompt and calls corresponding actions.
"""

from earbud_input import (
    run_ocr_client,
    call_client,
    hangup,
    mute_unmute,
    take_new_photo_and_ocr_client_to_queue,
    stop_ocr,
    pause_ocr,
    mode_mapping,
    fn_mapping,
    getMode,
    setMode,
)

def main():
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
        
if __name__ == "__main__":
    main()