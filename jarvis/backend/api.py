"""
backend/api.py

Very small API surface the frontend can call during development.

This is intentionally minimal right now. It exists so the Vite dev
server can proxy requests to something real instead of failing on
every frontend request.

Later, this can grow into the actual backend HTTP layer without
changing the frontend contract.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

# Allow backend imports when this module is executed directly from the
# launcher or from a separate runner process.
ROOT = Path(__file__).resolve().parent.parent
# This module is meant to be executed directly by the launcher, so keep
# import side effects minimal and run self-contained.


def _ensure_root_on_path() -> None:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


class BackendAPIHandler(BaseHTTPRequestHandler):
    """Stub handler for frontend health/transcript/command calls."""

    def log_message(self, fmt, *args):
        # Keep console noise manageable during launcher runs.
        print(f"[api] {fmt % args}")

    def _send_json(self, code: int, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True, "service": "jarvis-backend"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}

        if self.path == "/transcript":
            text = payload.get("text", "")
            self._send_json(200, {"received": text, "handled": True})
            return

        if self.path == "/command":
            command = payload.get("command", "")
            self._send_json(200, {"received": command, "handled": True})
            return

        self._send_json(404, {"error": "not found"})


def run_api_server(port: int) -> None:
    _ensure_root_on_path()
    server = HTTPServer(("127.0.0.1", port), BackendAPIHandler)
    print(f"[api] backend API stub listening on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[api] stopping")
    finally:
        server.server_close()





def main() -> None:
    port = 8000
    args = sys.argv[1:]
    if args and args[0] == "--port" and len(args) > 1:
        try:
            port = int(args[1])
        except ValueError:
            pass
    run_api_server(port=port)


if __name__ == "__main__":
    _ensure_root_on_path()
    main()
