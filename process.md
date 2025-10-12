Device used: Raspberry Pi Zero 2
A Pi Camera
A button is attached to it.

Button Functions:
Single tap : start processing image for OCR, on image capture send the data by shared memory
            and raise a signal. this signal will move the image from shared mem to a queue for processing to ocr_pipeline.
Double tap : will raise a signal to clear the queue and stop current processing and tts



