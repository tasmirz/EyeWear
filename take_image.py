from picamera2 import Picamera2
import time
import sys
picam2 = Picamera2()
camera_config = picam2.create_still_configuration(main={"size": (1920, 1080)})
picam2.configure(camera_config)


try:
    picam2.start()
    time.sleep(1)
    # Set autofocus mode to auto and wait for it to adjust
    picam2.set_controls({"AfMode": 0, "LensPosition": 425})
    time.sleep(2)
    # save as timestamped file
    # get timestamp from argv

    if len(sys.argv) > 1:
        timestamp = sys.argv[1]
    else:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
    picam2.capture_file(f"image_{timestamp}.jpg")
    picam2.stop()
    print(f"Image saved as image_{timestamp}.jpg")
except Exception as e:
    print(f"Error capturing image: {e}")
    exit(1)