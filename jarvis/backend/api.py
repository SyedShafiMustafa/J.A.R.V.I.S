"""
backend/api.py

Real Jarvis backend HTTP + WebSocket entry point.

This module starts the Jarvis backend service, which wraps the
existing runtime pieces and exposes, on one port:
- GET /api/health
- GET /api/state
- POST /api/listen/start
- POST /api/listen/stop
- POST /api/command
- WS /ws for real-time events

It is intentionally a service layer, not a stub.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("jarvis.api")


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis backend service")
    parser.add_argument("--port", type=int, default=8000, help="HTTP + WebSocket port")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="listen host")
    args = parser.parse_args()

    print(f"[api] starting Jarvis backend on http://{args.host}:{args.port}")
    print(f"[api] websocket on ws://{args.host}:{args.port}/ws")

    from backend.server import JarvisBackendService

    service = JarvisBackendService(host=args.host, port=args.port)
    service.start()

    runtime_msg = service.runtime_unavailable_message()
    if runtime_msg:
        print(f"[api] note: live runtime unavailable ({runtime_msg})")
        print("[api] note: the UI will still work; commands will report a clean 503")
    else:
        print("[api] live runtime ready")
        # Automatic wake listening: activate the existing voice loop as soon
        # as the runtime is healthy, without waiting for a UI button press.
        # ensure_listening is idempotent; failures are reported but do not
        # take the backend down (the UI can still start listening manually).
        ok, auto_error = service.ensure_listening()
        if ok:
            print("[api] wake listener started automatically; waiting for 'Hey Jarvis'")
        else:
            print(f"[api] note: automatic wake listener failed to start ({auto_error})")
            print("[api] note: use the central J control or Start Listening to retry")

    print("[api] ready")
    print("[api] endpoints:")
    print("  GET  /api/health")
    print("  GET  /api/state")
    print("  POST /api/listen/start")
    print("  POST /api/listen/stop")
    print("  POST /api/command")
    print("  WS   /ws")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[api] stopping")
    finally:
        service.stop()
        print("[api] stopped")


if __name__ == "__main__":
    main()
