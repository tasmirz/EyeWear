# plays audio feedback  based on shared memory signals
import multiprocessing.shared_memory as shm
from time import time
audio_feedback_shm = shm.SharedMemory(name='audio_feedback_signal')
import simpleaudio as sa
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

    }

class AudioFeedback:
    def __init__(self, key):
        self.key = key
        self.wave_obj = sa.WaveObject.from_wave_file(audio_files[key])
        self.play_obj = None
        self.is_playing = False
    
    def play(self,loop=False,threaded=True):
        # if loop, then must be threaded
        self.is_playing = True
        if loop:
            while True:
                self.play_obj = self.wave_obj.play()
                self.play_obj.wait_done()
        else:
            self.play_obj = self.wave_obj.play()
            if not threaded:
                self.play_obj.wait_done()
    def stop(self):
        if self.is_playing:
            self.play_obj.stop()
            self.is_playing = False

            
class AudioFeedbackManager:
    # dict of AudioFeedback objects
    def __init__(self,arr):
        self.audio_feedbacks = {}
        for key in arr:
            self.audio_feedbacks[key] = AudioFeedback(key)
    def play(self, key, loop=False, threaded=True):
        self.audio_feedbacks[key].play(loop=loop, threaded=threaded)
    def stop(self, key):
        self.audio_feedbacks[key].stop()
    def sequential_play(self, keys, threaded=False):
        if not threaded:
            for key in keys:
                self.play(key, threaded=False)
        else:
            threading.Thread(target=self._sequential_play_threaded, args=(keys,)).start()