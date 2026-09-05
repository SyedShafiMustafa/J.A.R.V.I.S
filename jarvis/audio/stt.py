from faster_whisper import WhisperModel


class SpeechToText:

    def __init__(self):
        print("🧠 Loading Faster-Whisper Large-v3 Turbo...")

        self.model = WhisperModel(
            "large-v3-turbo",
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