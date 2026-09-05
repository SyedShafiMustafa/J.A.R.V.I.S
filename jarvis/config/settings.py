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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

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
