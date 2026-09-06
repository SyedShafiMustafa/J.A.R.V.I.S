# server/ — JARVIS V2 Dell always-on server (Phase 0)

The foundation of the always-on JARVIS server that runs on the Dell.  Phase 0
is infrastructure only: configuration, logging, the database bootstrap, and a
FastAPI shell with honest health endpoints.  Later phases add the JARVIS core,
memory, voice, tools, telephony and callbacks **on top of this package** —
nothing here may import the legacy Lenovo voice runtime
(`run_voice_test.py` and friends).

## Layout

```
server/
├── config.py        environment-driven config, fail-fast validation (.env)
├── logging_setup.py rotating file + console logs (logs/server.log)
├── db.py            SQLite bootstrap: WAL, foreign keys, _meta schema version
├── app.py           FastAPI app factory: /healthz /readyz / /docs
├── run.py           python server/run.py [--host H] [--port P]
└── smoke.py         self-contained test suite (no pytest needed)
```

## Run

```bash
cd jarvis
python -m pip install -r requirements-server.txt   # once
python server/run.py                               # http://127.0.0.1:8000
```

Endpoints:

| Method | Path            | Meaning                                      |
| ------ | --------------- | -------------------------------------------- |
| GET    | `/`             | service identity (name/version/role/phase)   |
| GET    | `/healthz`      | liveness — the process is up (always 200)    |
| GET    | `/readyz`       | readiness — database probe, 503 when broken  |
| GET    | `/openapi.json` | API contract (auto-generated)                |
| GET    | `/docs`         | interactive API docs                         |

## Configuration

Every setting is an env var (`JARVIS_*`) or a key in `.env` at the repo's
`jarvis/` root.  `load_config()` validates everything at startup and raises
`ConfigError` listing **all** problems — the server never limps along
half-configured.

| Key                 | Default                       | Notes                                   |
| ------------------- | ----------------------------- | --------------------------------------- |
| `JARVIS_ENV`        | `development`                 | `production` enforces a real secret key |
| `JARVIS_ROLE`       | `server`                      | `server` (Dell) or `brain` (Lenovo)     |
| `JARVIS_HOST`       | `127.0.0.1`                   | loopback only unless secured            |
| `JARVIS_PORT`       | `8000`                        |                                         |
| `JARVIS_NODE_NAME`  | `<role>-<hostname>`           | shown in /healthz                       |
| `JARVIS_LOG_LEVEL`  | `INFO`                        | DEBUG..CRITICAL                         |
| `JARVIS_LOG_DIR`    | `<repo>/logs`                 | rotating 5 x 2 MB `server.log`          |
| `JARVIS_DATA_DIR`   | `<repo>/data`                 |                                         |
| `JARVIS_DB_PATH`    | `<data_dir>/jarvis.db`        | SQLite, WAL mode                        |
| `JARVIS_SECRET_KEY` | *(empty)*                     | required >= 16 chars when production    |
| `JARVIS_CORS_ORIGINS` | `*`                         | comma-separated UI origins              |
| `JARVIS_BRAIN_URL`  | *(empty)*                     | Lenovo LLM endpoint, used from Phase 1  |

## Test

```bash
cd jarvis
python server/smoke.py
```

Covers config validation (fail-fast cases), the rotating log writer, the
SQLite bootstrap (WAL, schema version, reopen), and the live app endpoints
(status codes, readiness, OpenAPI contract, clean shutdown).  The endpoint
section skips gracefully when fastapi/uvicorn are not installed.

## Dell deployment

See [`deploy/README.md`](../deploy/README.md) — one-time `setup.ps1`,
`run_server.ps1` for foreground runs, `install_service.ps1` for auto-start,
Tailscale notes for Lenovo/phone access.
