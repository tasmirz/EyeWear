# plays audio feedback  based on shared memory signals
import os
#os.environ['SDL_AUDIODRIVER'] = 'dummy'

import multiprocessing.shared_memory as shm
from time import time
from common import SoundType
import threading

# load audio files, dict
audio_files = {
    SoundType.CALL_START: 'assets/call_client.wav',
    SoundType.CALL_END: 'assets/hang_up.wav',
    SoundType.MUTED: 'assets/muted.wav',
    SoundType.UNMUTED: 'assets/unmuted.wav',
    SoundType.ZERO: 'assets/zero.wav',
    SoundType.ONE: 'assets/one.wav',
    SoundType.TWO: 'assets/two.wav',
    SoundType.THREE: 'assets/three.wav',
    SoundType.FOUR: 'assets/four.wav',
    SoundType.FIVE: 'assets/five.wav',
    SoundType.MANY: 'assets/many.wav',
    SoundType.PHOTOS_ARE_PROCESSING: 'assets/photos_are_processing.wav',
    SoundType.GENERATED_AUDIO: 'assets/generated-audio.wav',
    SoundType.RUN_OCR_CLIENT: 'assets/run_ocr_client.wav',
    SoundType.TAKE_NEW_PHOTO_AND_ADD_TO_OCR: 'assets/take_new_photo_and_add_to_ocr.wav',
    SoundType.PLEASE_TRY_AGAIN_LATER: 'assets/please_try_again_later.wav',
    SoundType.STOP_OCR: 'assets/stop_ocr.wav',
    SoundType.CALLING: 'assets/call.wav',
    SoundType.PROCESSING: 'assets/processing.wav',
    }

import subprocess
import threading
import time


# class AudioFeedback:
#     def __init__(self, key):
#         self.key = key
#         self.file_path = audio_files[key]
#         self.process = None
#         self.is_playing = False
#         self._paused = False

#     def _play(self, loop=False):
#         cmd = [
#             "ffplay",
#             "-nodisp",  # no video
#             "-autoexit",  # exit after file finishes
#             "-hide_banner",
#             "-loglevel", "quiet",
#             self.file_path
#         ]
#         while self.is_playing:
#             self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
#             self.process.wait()
#             if not loop:
#                 break

#     def play(self, loop=False, threaded=True):
#         if self.is_playing:
#             return  # already playing
#         self.is_playing = True
#         self._paused = False
#         if threaded:
#             self._thread = threading.Thread(target=self._play, args=(loop,), daemon=True)
#             self._thread.start()
#         else:
#             self._play(loop)

#     def pause(self):
#         if self.process and self.is_playing and not self._paused:
#             # Sending 'p' to ffplay toggles pause
#             self.process.stdin.write(b"p")
#             self.process.stdin.flush()
#             self._paused = True

#     def resume(self):
#         if self.process and self.is_playing and self._paused:
#             self.process.stdin.write(b"p")
#             self.process.stdin.flush()
#             self._paused = False

#     def stop(self):
#         if self.process and self.is_playing:
#             self.is_playing = False
#             if self.process.poll() is None:
#                 self.process.terminate()
#             if hasattr(self, "_thread") and self._thread.is_alive():
#                 self._thread.join(timeout=0.1)
#             self._paused = False


# class AudioFeedbackManager:
#     def __init__(self, arr):
#         self.audio_feedbacks = {key: AudioFeedback(key) for key in arr}

#     def play(self, key, loop=False, threaded=True):
#         self.audio_feedbacks[key].play(loop=loop, threaded=threaded)

#     def pause(self, key):
#         self.audio_feedbacks[key].pause()

#     def resume(self, key):
#         self.audio_feedbacks[key].resume()

#     def stop(self, key):
#         self.audio_feedbacks[key].stop()

#     def sequential_play(self, keys, threaded=False):
#         def _sequential_play_threaded(keys_list):
#             for key in keys_list:
#                 self.play(key, threaded=False)

#         if not threaded:
#             for key in keys:
#                 self.play(key, threaded=False)
#         else:
#             threading.Thread(target=_sequential_play_threaded, args=(keys,), daemon=True).start()


# # Example usage:
# # manager = AudioFeedbackManager(["beep", "alert"])
# # manager.play("beep", loop=True, threaded=True)
# # time.sleep(2)
# # manager.pause("beep")
# # time.sleep(2)
# # manager.resume("beep")
# # time.sleep(2)
# # manager.stop("beep")
