# Jarvis

Local-first desktop AI assistant: voice interface, Ollama-powered reasoning,
desktop automation, and a React control panel.

## Quick start

Run everything locally with one command:

```bash
cd jarvis
python launcher.py
```

This starts:

- the backend service (HTTP API + WebSocket event stream) on a free port
- the Vite + React UI dev server, pointed at that backend
- opens the UI in your browser automatically

The UI is available at the URL printed by the launcher, usually:

- `http://127.0.0.1:5173`

Stop everything with `Ctrl + C`.

First time only (after a fresh clone):

```bash
cd jarvis/ui
npm install
```

## Frontend-only development

If you only want to work on the UI:

```bash
cd jarvis/ui
npm run dev
```

The dev server proxies `/api` and `/ws` to the backend. If you run the UI
without the launcher, set the backend port with:

```bash
VITE_BACKEND_PORT=8000 npm run dev
```

## Backend endpoints

The backend serves everything on one localhost port (8000 by default):

| Method | Path              | Purpose                          |
| ------ | ----------------- | -------------------------------- |
| GET    | `/api/health`     | liveness check                   |
| GET    | `/api/state`      | current runtime state            |
| POST   | `/api/listen/start` | start the voice loop           |
| POST   | `/api/listen/stop`  | stop the voice loop            |
| POST   | `/api/command`    | send a text command              |
| WS     | `/ws`             | real-time status/reply/tool events |

HTTP errors use real status codes (`400`, `404`, `409`, `415`, `500`, `503`),
never `200`-with-an-error-payload.

## Runtime construction

The backend builds the runtime explicitly through
`backend.live_runtime.build_live_runtime()`. It never imports
`run_voice_test.py`, which is a standalone script with startup side effects
(model loading, `os._exit()` on validation failure).

If the live runtime dependencies are missing (audio hardware, Ollama, etc.),
the backend still starts: the UI works, and commands report a clean `503`
with the reason. `python backend/api.py` prints the reason at startup.

## Project structure

```
jarvis/
├── backend/
│   ├── api.py            # backend service entry point
│   ├── server.py         # HTTP + WebSocket service (single port)
│   ├── live_runtime.py   # explicit runtime factory (no import side effects)
│   ├── live_adapters.py  # live audio/tool/orchestrator adapters
│   ├── interfaces.py     # backend contracts
│   ├── tools.py          # tool registry (schemas, idempotency)
│   ├── observability.py  # structured event logging
│   ├── bus.py            # event bus
│   ├── models.py         # session/task model
│   ├── retry.py          # retry policy
│   ├── lifecycle.py      # startup/shutdown/interrupt handling
│   └── smoke.py          # backend test suite (no pytest required)
├── agents/               # brain + planner
├── audio/                # wake word, STT, TTS, VAD
├── core/                 # router, memory
├── tools/                # desktop/computer/vision automation
├── ui/                   # Vite + React control panel
├── server/               # JARVIS V2: Dell always-on server (FastAPI)
├── deploy/               # Dell deployment scripts + runbook
├── launcher.py           # starts backend + UI together
└── README.md
```

## Testing

The backend has a self-contained test suite:

```bash
cd jarvis
python backend/smoke.py
```

It covers contracts, lifecycle, retries, tool registry/safety, the HTTP API
(status codes), the WebSocket event stream, and runtime construction.

## Safety notes

- Desktop tools are validated against the tool registry before execution;
  unknown tools fail instead of silently succeeding.
- Retries only apply to idempotent tools, so typing/clicking/sending is
  never duplicated by a retry.
- Shell commands are built as argument lists, not shell strings, and
  app names are validated before use.
- The backend binds to `127.0.0.1` only.

## JARVIS V2 - roadmap (two-machine architecture)

JARVIS is evolving into one persistent AI identity reachable from laptop,
phone, dashboard and phone calls, with two machines:

| Machine | Role   | Responsibility |
| ------- | ------ | -------------- |
| Dell    | server | always-on FastAPI core: memory, scheduler, tasks, telephony, callbacks, WebSockets, auth |
| Lenovo  | brain  | heavy local LLM inference (Qwen/Ollama), agents, development |

### Phase status

| Phase | Deliverable | Status |
| ----- | ----------- | ------ |
| 0 | Infrastructure: `server/` FastAPI foundation, config validation, rotating logs, SQLite bootstrap, health endpoints, Dell auto-start scripts | **done** |
| 1 | JARVIS core: auth/tokens, device pairing, conversation + WebSocket API, LLM adapter (calls the Lenovo brain), basic memory | next |
| 2-12 | Voice, memory, tools, Android, telephony, reservations, callbacks, WhatsApp | planned |

Phase 0 details live in [`server/README.md`](server/README.md) and the Dell
runbook in [`deploy/README.md`](deploy/README.md).  Each phase must work
and be tested before the next begins.