from faster_whisper import WhisperModel

from config.config import WHISPER_MODEL


class SpeechToText:

    def __init__(self):
        print(f"🧠 Loading Faster-Whisper {WHISPER_MODEL}...")

        self.model = WhisperModel(
            WHISPER_MODEL,
            device="cpu",
            compute_type="int8"
        )

    def transcribe(self, audio_path):

        segments, info = self.model.transcribe(
            audio_path,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=350
            ),
            condition_on_previous_text=False
        )

        text = " ".join(s.text for s in segments).strip()

        return text