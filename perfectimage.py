from flask import Flask, Response
from picamera2 import Picamera2
import io
import time

app = Flask(__name__)
camera = Picamera2()

# Use preview configuration which enables autofocus properly
config = camera.create_preview_configuration(
    main={"size": (1920, 1080)},  # Adjust resolution as needed
    controls={
        "AfMode": 2,  # Continuous autofocus
        "AfSpeed": 0   # Normal speed
    }
)
camera.configure(config)
camera.start()

# Give time for autofocus to initialize
time.sleep(2)

@app.route("/capture")
def capture():
    stream = io.BytesIO()
    
    # Wait a bit for focus to settle
    time.sleep(1)
    
    # Capture image
    camera.capture_file(stream, format="jpeg")
    stream.seek(0)
    
    return Response(stream.read(), mimetype='image/jpeg')

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
