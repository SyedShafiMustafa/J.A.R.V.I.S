# Lenovo AI Brain Service (Phase 1)

This service runs on the **Lenovo** and exposes the existing JARVIS brain over
the Tailscale network so the Dell JARVIS server can call it.

It intentionally reuses the existing brain/Ollama/Qwen path:
- chat logic: `agents.brain.JarvisBrain`
- model/provider configuration: `config.config` + `config.settings` (`.env`)

It is **not** a second independent LLM/chat implementation.

## Run (on the Lenovo)

From the `jarvis/` folder:

```bash
# first time only
python -m pip install -r requirements-server.txt

# start the brain service
python server/brain_service.py --host 0.0.0.0 --port 8001
```

Notes:
- `--host 0.0.0.0` allows the Dell to reach it over Tailscale.
- `--port 8001` is the default; change it if 8001 is already in use on the Lenovo.
- The running command prints the exact health/chat paths and the resolved brain model + provider URL.

Endpoints:
- `GET /healthz`
- `POST /v1/chat`
- `POST /v1/chat/completions`

## Required .env values on the Lenovo

The brain service uses the same `.env` as the rest of the repo for LLM settings.
The important keys:

```ini
OLLAMA_URL=http://127.0.0.1:11434/api/generate
OLLAMA_MODEL=llama3.1
LLM_PROVIDER=ollama
```

If your Ollama/Qwen provider runs on a non-default host, set `OLLAMA_URL` to that
address. The brain service reads it through `config.config` so you do not need to
edit Python files.

## Required .env value on the Dell

The Dell JARVIS server must know where the Lenovo brain lives. Set this in the
Dell `.env`:

```ini
JARVIS_BRAIN_URL=http://100.102.49.30:8001
```

Use the actual Lenovo Tailscale IP, not the Dell IP. Do not set the Dell brain URL
to the Dell itself.

## Test from the Dell

Health:
```bash
curl -s http://100.102.49.30:8001/healthz
```

Chat:
```bash
curl -s -X POST http://100.102.49.30:8001/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say hello in one short sentence."}]}'
```

Both commands should execute on the **Dell** (or from any machine that can reach the
Lenovo over Tailscale), not on the Lenovo itself.

## Test locally (without Tailscale)

On the Lenovo, the smoke suite starts the brain service on a free port and exercises
`/healthz` and `/v1/chat` locally:

```bash
python server/brain_service_smoke.py
```

Expected:
- `/healthz` returns `status: ok` plus brain model/provider metadata
- `/v1/chat` returns a non-empty `response` string and `model`/`server` metadata
- empty message requests are rejected with a 4xx status

## What this service is not

It does not implement, and is not a prerequisite for:
- authentication (that belongs on the Dell JARVIS server)
- device pairing
- conversation state
- WebSockets
- voice
- telephony
- WhatsApp
- reservations or callbacks
- advanced memory

## Replacement path

The brain service is modular by design: it talks HTTP/JSON only and delegates chat
to `agents.brain.JarvisBrain`. To replace Qwen/Ollama later, change the brain
implementation and/or `OLLAMA_URL`/`OLLAMA_MODEL` without changing the Dell JARVIS
Core contract (`/v1/chat` request/response shape stays the same).
