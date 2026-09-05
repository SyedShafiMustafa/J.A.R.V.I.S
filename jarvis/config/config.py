from pathlib import Path

# Project root
ROOT = Path(__file__).resolve().parent.parent

# Audio
SAMPLE_RATE = 16000
CHANNELS = 1
WAKEWORD = "hey_jarvis"

# Whisper
WHISPER_MODEL = "tiny"

# Piper (Jarvis Voice)
PIPER_MODEL = ROOT / "models" / "piper" / "jarvis.onnx"
PIPER_CONFIG = ROOT / "models" / "piper" / "jarvis.onnx.json"

# Ollama
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:1.5b"