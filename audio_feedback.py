# plays audio feedback  based on shared memory signals
import os
#os.environ['SDL_AUDIODRIVER'] = 'dummy'

import multiprocessing.shared_memory as shm
import time
from common import SoundType
import threading
import vlc

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
    SoundType.IM_TAKING_DONE: 'assets/pcpw.mp3',
    
    }
class AudioFeedback:
    def __init__(self, key):
        self.key = key
        self.file_path = audio_files[key]
        self.player = None
        self.is_playing = False
        self._loop = False

    def _play(self):
        print(f"🔊 Playing sound: {self.key}")
        instance = vlc.Instance("--no-xlib", "--quiet", "--no-video", "--intf", "dummy")
        self.player = instance.media_player_new()
        media = instance.media_new(self.file_path)
        self.player.set_media(media)

        self.player.play()
        self.is_playing = True

        # Wait for playback to start
        time.sleep(0.1)

        # Handle looping
        while self.is_playing:
            state = self.player.get_state()
            if state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
                if self._loop:
                    self.player.stop()
                    self.player.play()
                else:
                    break
            time.sleep(0.1)
        self.is_playing = False

    def play(self, loop=False, threaded=True):
        if self.is_playing:
            return
        self._loop = loop
        if threaded:
            self._thread = threading.Thread(target=self._play, daemon=True)
            self._thread.start()
        else:
            self._play()

    def stop(self):
        if self.player and self.is_playing:
            self.is_playing = False
            self.player.stop()
            if hasattr(self, "_thread") and self._thread.is_alive():
                self._thread.join(timeout=0.1)


class AudioFeedbackManager:
    def __init__(self, arr):
        self.audio_feedbacks = {key: AudioFeedback(key) for key in arr}

    def play(self, key, loop=False, threaded=True):
        self.audio_feedbacks[key].play(loop=loop, threaded=threaded)

    def stop(self, key):
        self.audio_feedbacks[key].stop()

    def sequential_play(self, keys, threaded=False):
        def _sequential_play_threaded(keys_list):
            for key in keys_list:
                self.play(key, threaded=False)

        if not threaded:
            for key in keys:
                self.play(key, threaded=False)
        else:
            threading.Thread(target=_sequential_play_threaded, args=(keys,), daemon=True).start()