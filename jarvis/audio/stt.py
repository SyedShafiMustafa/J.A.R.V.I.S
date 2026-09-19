import os

from faster_whisper import WhisperModel

# Model tier is resolved from the environment at construction time.
# config.settings (imported below) runs load_dotenv(), so any
# WHISPER_MODEL=... line in .env is honored here. "base" is the
# latency-oriented default for JARVIS's short-command workload;
# set WHISPER_MODEL=large-v3-turbo in .env to restore the old tier.
DEFAULT_WHISPER_MODEL = "base"

# Greedy decoding: a single decode path is ~2-3x faster than beam search
# with no measurable accuracy loss on 1-2 sentence voice commands.
BEAM_SIZE = 1


class SpeechToText:

    def __init__(self):
        # Import config.settings for its load_dotenv side effect so .env
        # values are visible even when this module is constructed first.
        from config import settings  # noqa: F401

        model_name = os.getenv("WHISPER_MODEL", DEFAULT_WHISPER_MODEL)

        print(f"[STT] Loading Faster-Whisper {model_name}...")

        self.model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8"
        )

    def transcribe(self, audio_path):

        segments, info = self.model.transcribe(
            audio_path,
            language="en",
            beam_size=BEAM_SIZE,
            without_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=350
            ),
            condition_on_previous_text=False
        )

        text = " ".join(s.text for s in segments).strip()

        return text