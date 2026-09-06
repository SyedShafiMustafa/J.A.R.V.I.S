"""
server/smoke_phase0.py
----------------------
Phase 0 smoke suite for the JARVIS V2 Dell server.

Run it with:
    python server/smoke_phase0.py

It intentionally does not depend on the Phase 1 router or the brain client,
and it runs fastapi/uvicorn only when the package is installed.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.config import ConfigError, load_config  # noqa: E402
from server.db import connect, db_ok, ensure_db  # noqa: E402

_COUNTS = {"passed": 0, "failed": 0, "skipped": 0}


def ok(name: str, cond: bool, extra: str = "") -> None:
    tag = "ok " if cond else "FAIL"
    _COUNTS["passed" if cond else "failed"] += 1
    print(f"[{tag}] {name}" + (f"  ({extra})" if extra else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def skip(name: str, reason: str) -> None:
    _COUNTS["skipped"] += 1
    print(f"[skip] {name}  ({reason})")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_get(url: str, timeout: float = 5.0):
    """GET url -> (status_code, body_bytes). HTTP errors are returned too."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:  # non-2xx is a normal outcome here
        return err.code, err.read()


TMP = Path(tempfile.mkdtemp(prefix="jarvis_server_smoke_"))


def mkcfg(**overrides) -> "ServerConfig":
    base = {
        "DATA_DIR": TMP / "data",
        "LOG_DIR": TMP / "logs",
        "DB_PATH": TMP / "data" / "jarvis.db",
        "NODE_NAME": "smoke-node",
        "HOST": "127.0.0.1",
        "PORT": _free_port(),
    }
    base.update(overrides)
    # environ={} keeps the real process env / .env out of the test entirely.
    return load_config(overrides=base, environ={})


# --------------------------------------------------------------------------- #
# 1. config
# --------------------------------------------------------------------------- #
def test_config() -> None:
    section("config: defaults, overrides, fail-fast validation")

    cfg = mkcfg()
    ok("defaults: loopback host + integer port", cfg.host == "127.0.0.1" and isinstance(cfg.port, int))
    ok("overridden db path is honored", str(cfg.db_path).startswith(str(TMP)))

    try:
        load_config(overrides={"ENV": "staging"}, environ={})
        ok("invalid JARVIS_ENV rejected", False)
    except ConfigError:
        ok("invalid JARVIS_ENV rejected", True)

    try:
        load_config(overrides={"ENV": "production"}, environ={})
        ok("production without secret rejected", False)
    except ConfigError:
        ok("production without secret rejected", True)

    try:
        load_config(overrides={"ENV": "production", "SECRET_KEY": "short"}, environ={})
        ok("production with short secret rejected", False)
    except ConfigError:
        ok("production with short secret rejected", True)

    prod = load_config(
        overrides={"ENV": "production", "SECRET_KEY": "x" * 24, "CORS_ORIGINS": "http://a.local,http://b.local"},
        environ={},
    )
    ok("production with valid secret accepted", prod.env == "production" and len(prod.secret_key) >= 16)
    ok("cors origins parsed to a list", prod.cors_origins == ["http://a.local", "http://b.local"])

    try:
        load_config(overrides={"PORT": "not-a-number"}, environ={})
        ok("non-integer port rejected", False)
    except ConfigError:
        ok("non-integer port rejected", True)


# --------------------------------------------------------------------------- #
# 2. logging
# --------------------------------------------------------------------------- #
def test_logging() -> None:
    section("logging: rotating file handler writes")

    from server.logging_setup import get_logger, setup_logging

    cfg = mkcfg()
    logger = setup_logging(cfg)
    get_logger("smoke").info("phase0-smoke-marker-%s", "12345")
    for handler in logger.handlers:
        handler.flush()

    log_file = cfg.log_dir / "server.log"
    ok("log file created", log_file.is_file(), str(log_file))
    if log_file.is_file():
        ok("marker line written", "phase0-smoke-marker-12345" in log_file.read_text(encoding="utf-8"))

    logging.shutdown()  # release file handles before tmp cleanup (Windows)


# --------------------------------------------------------------------------- #
# 3. db
# --------------------------------------------------------------------------- #
def test_db() -> None:
    section("db: sqlite bootstrap (WAL + schema version)")

    from server.db import SCHEMA_VERSION

    cfg = mkcfg()
    ensure_db(cfg)
    ok("db file created", cfg.db_path.is_file())
    ok("readiness probe passes", db_ok(cfg))

    conn = connect(cfg.db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        version = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0]
    finally:
        conn.close()
    ok("journal_mode is WAL", mode == "wal", mode)
    ok(f"schema version is {SCHEMA_VERSION}", version == str(SCHEMA_VERSION), str(version))

    # Opening a second connection on the same file must work (multi-threaded server).
    conn2 = connect(cfg.db_path)
    conn2.close()
    ok("second connection opens cleanly", True)


# --------------------------------------------------------------------------- #
# 4. live endpoints (needs fastapi + uvicorn)
# --------------------------------------------------------------------------- #
def test_endpoints() -> None:
    section("endpoints: live FastAPI app (honest status codes)")

    try:
        import uvicorn  # noqa: F401
        from server.app import create_app
    except ImportError as exc:
        skip("live endpoint checks", f"missing dependency: {exc.__class__.__name__}: {exc}")
        skip("install for full suite", "python -m pip install -r requirements-server.txt")
        return

    cfg = mkcfg()
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://{cfg.host}:{cfg.port}"
    deadline = time.time() + 15.0
    started = False
    while time.time() < deadline and not started:
        status, _ = _http_get(f"{base}/healthz")
        started = status == 200
        if not started:
            time.sleep(0.2)
    ok("server starts and answers /healthz", started)

    if started:
        status, body = _http_get(f"{base}/healthz")
        data = json.loads(body)
        ok("/healthz -> 200 + status ok", status == 200 and data.get("status") == "ok")

        status, body = _http_get(f"{base}/readyz")
        data = json.loads(body)
        ok("/readyz returns a valid payload", status in {200, 503} and data.get("status") in {"ready", "degraded"})

        status, body = _http_get(f"{base}/")
        data = json.loads(body)
        ok("identity payload (service/version/role)", status == 200 and data.get("service") == "jarvis-server" and data.get("role") == "server")

        status, body = _http_get(f"{base}/openapi.json")
        data = json.loads(body)
        ok("/openapi.json documents the API", status == 200 and data.get("openapi"))

        status, _ = _http_get(f"{base}/does-not-exist")
        ok("unknown route -> honest 404", status == 404, f"got {status}")

        ready, body = _http_get(f"{base}/readyz")
        data = json.loads(body)
        ok("readiness payload stable after traffic", ready in {200, 503} and data.get("status") in {"ready", "degraded"})

    server.should_exit = True
    thread.join(timeout=10)
    ok("server stops cleanly on shutdown", not thread.is_alive())

    logging.shutdown()  # close the lifespan's file handler before tmp cleanup


def main() -> int:
    print(f"JARVIS V2 server smoke suite (phase 0) - {len(list(Path(__file__).parent.glob('*.py')))} modules in server/")
    try:
        test_config()
        test_logging()
        test_db()
        test_endpoints()
    finally:
        logging.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)

    passed, failed, skipped = _COUNTS["passed"], _COUNTS["failed"], _COUNTS["skipped"]
    print(f"\nresults: {passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
