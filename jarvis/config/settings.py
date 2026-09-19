"""
settings.py
------------
This is the ONE place every other file reads configuration from.
Nothing else in the project should call os.getenv() directly —
that way, if you rename or add a setting, you only change it here.

How this works, if you're new to it:
- "load_dotenv()" reads your .env file and copies its values into the
  environment (like a temporary set of labeled boxes your code can read).
- "os.getenv('KEY', 'default')" reads one of those boxes, or uses the
  default if the box is empty/missing.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Find the project root (the "jarvis" folder) no matter where this file
# is imported from — avoids "file not found" bugs later.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# --- LLM brain ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")      # "ollama" or "openai"
# OpenAI-compatible provider settings (only used when LLM_PROVIDER=openai).
# Keys are never hardcoded — set them in .env. Ollama stays the local fallback.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")            # e.g. https://api.openai.com/v1
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")                 # cloud model name
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
# When the cloud provider is the primary brain, automatically fall back to the
# local Ollama model if it is unreachable / rate-limited (never for a rejected
# credential). Set LLM_FALLBACK=0 to disable.
LLM_FALLBACK = os.getenv("LLM_FALLBACK", "1").strip().lower() not in {"0", "false", "no", "off"}
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "30"))
# How long Ollama keeps the model loaded between requests. Longer = the first
# reply after an idle gap stays warm, at the cost of holding the model in RAM.
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
OLLAMA_MAX_RETRIES = int(os.getenv("OLLAMA_MAX_RETRIES", "2"))
OLLAMA_RETRY_DELAY = float(os.getenv("OLLAMA_RETRY_DELAY", "0.25"))

# --- Speech-to-text ---
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")   # tiny / base / medium / large-v3-turbo

# --- Conversation mode ---
# How long J.A.R.V.I.S. keeps listening for follow-up sentences after a wake
# word before dropping back to wake-word-only listening. 0 = stay until the
# user says the exit phrase ("bye-bye Jarvis").
CONVERSATION_IDLE_TIMEOUT = float(os.getenv("CONVERSATION_IDLE_TIMEOUT", "60"))

# --- Wake word ---
PICOVOICE_ACCESS_KEY = os.getenv("PICOVOICE_ACCESS_KEY", "")
WAKE_WORD = "jarvis"          # Porcupine's free built-in keyword we'll start with

# --- Text-to-speech ---
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "edge")        # "edge" or "elevenlabs"
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
TTS_VOICE = "en-US-GuyNeural"   # a good default JARVIS-ish edge-tts voice

# --- File paths (created automatically, don't edit) ---
DATA_DIR = PROJECT_ROOT / "data"
MEMORY_DB_PATH = DATA_DIR / "memory.db"
VECTOR_STORE_DIR = DATA_DIR / "vector_store"
LOGS_DIR = PROJECT_ROOT / "logs"

DATA_DIR.mkdir(exist_ok=True)
VECTOR_STORE_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
