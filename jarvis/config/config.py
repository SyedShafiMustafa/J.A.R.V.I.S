"""
config.py
---------
Single place every module reads configuration from.

Everything that can be overridden lives in settings.py (which reads .env).
This file just re-exports those values plus a few fixed audio constants,
so existing imports like `from config.config import OLLAMA_MODEL` keep working.
"""

from pathlib import Path

from config.settings import (
    LLM_PROVIDER,          # "ollama" or "openai"
    OLLAMA_MODEL,          # from .env, defaults to llama3.1
    OLLAMA_URL,            # Ollama/Qwen provider endpoint (from .env)
    OPENAI_API_KEY,
    TTS_PROVIDER,
    MEMORY_DB_PATH,        # absolute — no more CWD-dependent paths
    VECTOR_STORE_DIR,
    LOGS_DIR,
    PROJECT_ROOT as ROOT,
)

# --- Audio ---
SAMPLE_RATE = 16000
CHANNELS = 1
WAKEWORD = "hey_jarvis"    # openwakeword's built-in model name

# --- Whisper ---
WHISPER_MODEL = "large-v3-turbo"   # override with WHISPER_MODEL= in .env

# --- Piper (Jarvis Voice) ---
PIPER_MODEL = ROOT / "models" / "piper" / "jarvis.onnx"
PIPER_CONFIG = ROOT / "models" / "piper" / "jarvis.onnx.json"

# --- Ollama ---
OLLAMA_URL: str  # resolved from settings.py at runtime using .env