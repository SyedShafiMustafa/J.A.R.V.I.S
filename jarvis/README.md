# JARVIS — Personal Desktop AI Assistant

## One-time setup (do this before Phase 3)

1. **Install Python 3.11+** from python.org. During install, check "Add Python to PATH".
2. **Install Ollama** (your free local AI brain): download from ollama.com, install it,
   then open Command Prompt and run:
   ```
   ollama pull llama3.1
   ```
   This downloads the model once (a few GB). Ollama then runs quietly in the background
   and JARVIS talks to it on your own machine — no internet, no API key, no cost.
3. **Get a free Porcupine access key** (for the wake word) at console.picovoice.ai —
   sign up free, copy your key.
4. **Open this folder in VS Code.**
5. **Create a virtual environment** (keeps this project's packages separate from
   everything else on your PC). In VS Code's terminal:
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
6. **Copy `.env.example` to `.env`** and paste in your Porcupine key.

You're now ready for Phase 3 — the voice engine.
