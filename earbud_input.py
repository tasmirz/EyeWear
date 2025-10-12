#!/usr/bin/env python3
"""
Bluetooth Headphone Button Event Reader for Raspberry Pi
Reads play/pause, previous, and next button events from connected BT headphones
"""

import evdev
from evdev import InputDevice, categorize, ecodes
import sys

def find_bluetooth_device():
    """Find the Bluetooth input device"""
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    
    print("Available input devices:")
    for i, device in enumerate(devices):
        print(f"{i}: {device.path} - {device.name}")
    
    # Look for devices with key capabilities (typical for BT headphones)
    for device in devices:
        caps = device.capabilities()
        if ecodes.EV_KEY in caps:
            keys = caps[ecodes.EV_KEY]
            # Check if device has media keys
            if any(k in keys for k in [ecodes.KEY_PLAYPAUSE, ecodes.KEY_NEXTSONG, 
                                       ecodes.KEY_PREVIOUSSONG, ecodes.KEY_PLAYCD]):
                print(f"\nFound potential BT device: {device.name}")
                print(f"Path: {device.path}")
                return device
    
    return None

def read_button_events(device):
    """Read and process button events"""
    print(f"\nListening for button events on {device.name}...")
    print("Press buttons on your headphones (Ctrl+C to exit)\n")
    
    try:
        for event in device.read_loop():
            if event.type == ecodes.EV_KEY:
                key_event = categorize(event)
                
                if key_event.keystate == key_event.key_down:
                    # Button pressed
                    if event.code == ecodes.KEY_PLAYPAUSE or event.code == ecodes.KEY_PLAYCD:
                        print("▶️  PLAY/PAUSE pressed")
                    elif event.code == ecodes.KEY_NEXTSONG:
                        print("⏭️  NEXT pressed")
                    elif event.code == ecodes.KEY_PREVIOUSSONG:
                        print("⏮️  PREVIOUS pressed")
                    elif event.code == ecodes.KEY_VOLUMEUP:
                        print("🔊 VOLUME UP pressed")
                    elif event.code == ecodes.KEY_VOLUMEDOWN:
                        print("🔉 VOLUME DOWN pressed")
                    else:
                        # Print other key codes for debugging
                        print(f"Key pressed: {event.code} ({key_event.keycode})")
                        
    except KeyboardInterrupt:
        print("\nStopped listening.")
    except PermissionError:
        print("\nPermission denied! Run with sudo:")
        print(f"sudo python3 {sys.argv[0]}")

def main():
    device = find_bluetooth_device()
    
    if device is None:
        print("\n⚠️  No Bluetooth device found with media keys!")
        print("\nManual device selection:")
        devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
        
        if not devices:
            print("No input devices found!")
            return
            
        try:
            idx = int(input("\nEnter device number: "))
            device = devices[idx]
        except (ValueError, IndexError):
            print("Invalid selection!")
            return
    
    # Grab exclusive access (prevents other apps from getting events)
    # Comment out if you want other apps to also receive events
    try:
        device.grab()
        print("(Grabbed exclusive access to device)")
    except:
        pass
    
    read_button_events(device)

if __name__ == "__main__":
    main()