# JARVIS V2 - Dell deployment (Phase 0)

This folder turns the **Dell** (older Windows 10 laptop, 4 GB RAM) into the
**always-on JARVIS server**: the persistent nervous system that hosts the
core, memory, scheduler, tools, telephony and WebSockets.  The **Lenovo**
stays the heavy-AI brain.  The code in `jarvis/server/` is the foundation
this phase installs; it grows through Phases 1-12 without re-setup.

## Machine roles

| Machine     | Role          | Responsibilities                                        |
| ----------- | ------------- | ------------------------------------------------------- |
| Dell        | `server`      | FastAPI server, memory DB, scheduler, tasks, telephony,  |
|             | (always on)   | WebSockets, callbacks, auth/gateway - thin on AI         |
| Lenovo      | `brain`       | Qwen/Ollama inference, heavy agents, development         |
| Phone/others| clients       | Android app, dashboard, WhatsApp - talk to the Dell core |

The server never imports the legacy Lenovo voice runtime
(`run_voice_test.py`), so heavy audio/AI dependencies stay off the Dell.

## One-time setup (on the Dell)

1. Install **Python 3.10+** from python.org (tick *Add python.exe to PATH*).
   Verify: `python --version`
2. Install **Git** and clone the repository:
   ```powershell
   git clone <your-repo-url>
   cd <repo>\jarvis
   ```
3. Install the server:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\deploy\dell\setup.ps1
   ```
   This creates `.venv-server`, installs `requirements-server.txt`, copies
   `.env.example` to `.env`, makes `logs/` + `data/`, and runs the smoke suite.
4. Edit `.env` — at minimum set a real `JARVIS_SECRET_KEY` (>= 16 chars) and a
   `JARVIS_NODE_NAME`.  Leave `JARVIS_HOST=127.0.0.1` unless you have secured
   access (Tailscale) and intentionally want it reachable on the LAN.

## Run it

Foreground (first test run):

```powershell
.\deploy\dell\run_server.ps1
```

Check it:

- `http://127.0.0.1:8000/healthz`  -> `{"status":"ok", ...}`
- `http://127.0.0.1:8000/readyz`   -> `200` when the database is healthy
- `http://127.0.0.1:8000/docs`     -> interactive API docs (FastAPI/OpenAPI)

Logs: `logs/server.log` (rotating, 5 x 2 MB).  Database: `data/jarvis.db`
(SQLite, WAL mode).

## Always-on (auto-start)

```powershell
.\deploy\dell\install_service.ps1            # start at your logon
.\deploy\dell\install_service.ps1 -AtStartup # start at boot as SYSTEM
```

Notes:

- `-AtStartup` needs an **elevated** shell, and the repo must live somewhere
  SYSTEM can read — not under `C:\Users\...\Downloads`.
- Plain logon mode stops the server when you log off.  Use `-AtStartup` for
  24/7 operation, or configure Windows to keep the session alive.

Manage:

```powershell
schtasks /Run    /TN JarvisServer
schtasks /Query  /TN JarvisServer
.\deploy\dell\uninstall_service.ps1
```

## Reaching it from the Lenovo / phone

1. Install **Tailscale** on both the Dell and the Lenovo/phone
   (https://tailscale.com).  Both machines then have stable private IPs.
2. In `.env` set `JARVIS_HOST=0.0.0.0` and restrict
   `JARVIS_CORS_ORIGINS` to your UI origins.
3. Reach the server at `http://<dell-tailscale-ip>:8000`.
4. Do **not** port-forward the FastAPI port to the public internet.  Auth,
   HTTPS and device pairing arrive in Phase 1; until then loopback/Tailscale
   only.

## Updates

```powershell
git pull
.\deploy\dell\setup.ps1     # re-runs pip install + smoke suite, keeps .env
```

## Security posture (Phase 0)

- Binds to `127.0.0.1` by default.
- Production mode refuses to start without a `JARVIS_SECRET_KEY` >= 16 chars.
- Config fails fast with every problem listed (`ConfigError`).
- No secrets are committed; `.env` is git-ignored.

## Phase map (what comes next)

- **Phase 1** adds the JARVIS core on this foundation: auth/tokens, device
  pairing, conversation + WebSocket endpoints, the LLM adapter (calls the
  Lenovo brain at `JARVIS_BRAIN_URL`), and basic memory.
- Every later phase (voice, memory, tools, Android, telephony, reservations,
  callbacks, WhatsApp) layers onto `server/` without re-deploying the Dell
  from scratch.
