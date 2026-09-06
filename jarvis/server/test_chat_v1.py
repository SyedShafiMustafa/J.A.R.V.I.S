"""
server/test_chat_v1.py
----------------------
Minimal tests for the new Dell conversation proxy.

Run with:
    python server/test_chat_v1.py

These tests only cover:
- the endpoint exists on the app
- invalid request bodies are rejected
- a reachable brain returns the brain response
- an unavailable brain returns an honest error

They do not add auth/pairing/memory/voice/telephony/HUD.
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path

# Make imports work no matter where this is run from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _http_json(method, base, path, payload=None, headers=None, timeout=10.0):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            body = {"raw": exc.read().decode("utf-8", "replace")}
        return exc.code, body
    except Exception as exc:
        return None, str(exc)


def _free_port(start=9000, stop=9999):
    import socket
    for port in range(start, stop):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("No free port found")


def _make_brain_app():
    # Import the existing brain service factory without starting a full server
    # for these proxy tests. We only need its ASGI app.
    from server.brain_service import create_brain_app
    return create_brain_app()


def _make_server_app(config, port):
    from server.app import create_app
    app = create_app(config=config)
    return app


def run():
    print("server/test_chat_v1.py - minimal Dell conversation proxy tests\n")

    all_ok: bool = True

    # ---------------------------------------------------------------------------
    # 1. Build a real Dell server + a local Lenovo brain on the same host.
    # ---------------------------------------------------------------------------
    import uvicorn

    brain_port = _free_port()
    server_port = _free_port(start=brain_port + 1)

    cfg = {
        "JARVIS_ENV": "development",
        "JARVIS_ROLE": "server",
        "JARVIS_HOST": "127.0.0.1",
        "JARVIS_PORT": str(server_port),
        "JARVIS_NODE_NAME": "test-dell",
        "JARVIS_LOG_LEVEL": "warning",
        "JARVIS_LOG_DIR": str(Path(".").resolve() / "tmp_test_logs"),
        "JARVIS_DATA_DIR": str(Path(".").resolve() / "tmp_test_data"),
        "JARVIS_DB_PATH": "",
        "JARVIS_SECRET_KEY": "test-secret-1234",
        "JARVIS_CORS_ORIGINS": "*",
        "JARVIS_BRAIN_URL": f"http://127.0.0.1:{brain_port}",
    }

    from server.config import load_config
    from server.db import ensure_db

    p = Path(cfg["JARVIS_LOG_DIR"])
    p.mkdir(parents=True, exist_ok=True)
    d = Path(cfg["JARVIS_DATA_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    db_path = p.parent / "tmp_test_db.sqlite3"
    cfg["JARVIS_DB_PATH"] = str(db_path)
    if db_path.exists():
        db_path.unlink()
    os.environ.clear()
    os.environ.update(cfg)
    config = load_config()

    ensure_db(config)

    brain_app = _make_brain_app()
    server_app = _make_server_app(config, server_port)

    brain_thread = threading.Thread(
        target=lambda: uvicorn.run(
            brain_app, host="127.0.0.1", port=brain_port, log_level="warning"
        ),
        daemon=True,
    )
    server_thread = threading.Thread(
        target=lambda: uvicorn.run(
            server_app, host="127.0.0.1", port=server_port, log_level="warning"
        ),
        daemon=True,
    )

    brain_thread.start()
    server_thread.start()

    base_server = f"http://127.0.0.1:{server_port}"
    base_brain = f"http://127.0.0.1:{brain_port}"

    deadline = time.time() + 12
    brain_ok = False
    server_ok = False
    while time.time() < deadline and not (brain_ok and server_ok):
        try:
            if not brain_ok:
                status, _ = _http_json("GET", base_brain, "/healthz", timeout=1)
                brain_ok = status == 200
        except Exception:
            pass
        try:
            if not server_ok:
                status, _ = _http_json("GET", base_server, "/healthz", timeout=1)
                server_ok = status == 200
        except Exception:
            pass
        time.sleep(0.2)

    print(f"brain started on {base_brain}: {brain_ok}")
    print(f"server started on {base_server}: {server_ok}")

    if not (brain_ok and server_ok):
        print("[FAIL] required services did not start")
        all_ok = False
        server_thread.join(timeout=2)
        brain_thread.join(timeout=2)
        sys.exit(1)

    def _mark(ok: bool, msg: str) -> None:
        nonlocal all_ok
        all_ok = all_ok and ok
        tag = "ok" if ok else "FAIL"
        print(f"[{tag}] {msg}")

    def _mark_with_status(status, body, ok: bool, msg: str) -> None:
        nonlocal all_ok
        all_ok = all_ok and ok
        tag = "ok" if ok else "FAIL"
        print(f"[{tag}] {msg}: {status} {body}")

    secret = config.secret_key or "test-secret-1234"

    # ---------------------------------------------------------------------------
    # 2. Endpoint exists
    # ---------------------------------------------------------------------------
    # ---------------------------------------------------------------------------
    # 2. Endpoint exists
    # ---------------------------------------------------------------------------
    status, body = _http_json("POST", base_server, "/api/v1/chat", payload={"messages": [{"role": "user", "content": "hi"}]})
    _mark_with_status(status, body, status == 200 and isinstance(body, dict) and "response" in body, "/api/v1/chat exists and returns a response")

    status, body = _http_json("POST", base_server, "/api/v1/chat", payload={"messages": []})
    _mark_with_status(status, body, status == 400 and isinstance(body, dict), "empty messages rejected")

    status, body = _http_json("POST", base_server, "/api/v1/chat", payload={})
    _mark_with_status(status, body, status == 400 and isinstance(body, dict), "missing messages rejected")

    status, body = _http_json("POST", base_server, "/api/v1/chat", payload="not-a-dict")
    _mark_with_status(status, body, status == 400 and isinstance(body, dict), "invalid JSON body rejected")

    # ---------------------------------------------------------------------------
    # 3. Valid conversation round trip
    # ---------------------------------------------------------------------------
    status, body = _http_json(
        "POST",
        base_server,
        "/api/v1/chat",
        payload={
            "messages": [
                {"role": "user", "content": "Say hello in one short sentence."},
            ],
            "conversation_id": "chat-test-1",
            "context": {"source": "dell-proxy-test"},
        },
    )
    _mark_with_status(
        status,
        body,
        status == 200 and isinstance(body, dict) and isinstance(body.get("response"), str) and body["response"],
        "valid chat request returns a non-empty response",
    )

    # ---------------------------------------------------------------------------
    # 5. Unavailable brain produces honest error
    # ---------------------------------------------------------------------------
    import server.config as cfg_mod

    degraded_cfg = {
        **cfg,
        "JARVIS_BRAIN_URL": "http://127.0.0.1:1/healthz",
    }
    os.environ.clear()
    os.environ.update(degraded_cfg)
    degraded_config = cfg_mod.load_config()
    degraded_port = _free_port(start=server_port + 1)
    degraded_app = _make_server_app(degraded_config, degraded_port)

    degraded_base = f"http://127.0.0.1:{degraded_port}"
    degraded_thread = threading.Thread(
        target=lambda: uvicorn.run(
            degraded_app, host="127.0.0.1", port=degraded_port, log_level="warning"
        ),
        daemon=True,
    )
    degraded_thread.start()
    time.sleep(0.5)

    status, body = _http_json(
        "POST",
        degraded_base,
        "/api/v1/chat",
        payload={"messages": [{"role": "user", "content": "hi"}]},
        timeout=10.0,
    )
    ok = isinstance(status, int) and status in (502, 503, 504)
    tag = "ok" if ok else "FAIL"
    print(f"[{tag}] unavailable brain returns honest error: {status} {body}")
    all_ok &= ok

    degraded_thread.join(timeout=3)

    print()
    if all_ok:
        print("ALL TESTS PASSED")
        sys.exit(0)
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    raise SystemExit(run())
