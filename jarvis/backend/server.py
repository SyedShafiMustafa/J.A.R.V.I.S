"""
backend/server.py

Real Jarvis backend service layer.

This is the piece that makes the backend controllable from the UI
and streamable over WebSocket.

It wraps the existing Jarvis runtime pieces:
- audio (wake word, STT, TTS)
- router / planner / brain / executor
- tool orchestration
- session / lifecycle / bus

It exposes:
- GET /api/health
- GET /api/state
- POST /api/listen/start
- POST /api/listen/stop
- POST /api/command
- WS /ws for real-time events

Design note:
- the voice loop stays on the backend
- the UI is a control panel that can start/stop listening and send
  text commands
- nothing AI-related is duplicated in the frontend
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_log = logging.getLogger("jarvis.backend")
logger = logging.getLogger("jarvis.backend")

# ---------------------------------------------------------------------------
# Simple JSON helpers
# ---------------------------------------------------------------------------


def _json_response(code: int, payload: Any) -> bytes:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return (
        f"HTTP/1.1 {code} OK\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "\r\n"
    ).encode("utf-8") + data


def _json_error(code: int, message: str) -> bytes:
    return _json_response(code, {"error": message})


# ---------------------------------------------------------------------------
# Runtime state model
# ---------------------------------------------------------------------------

STATUS_IDLE = "idle"
STATUS_LISTENING = "listening"
STATUS_THINKING = "thinking"
STATUS_EXECUTING = "executing"
STATUS_SPEAKING = "speaking"
STATUS_ERROR = "error"

VALID_STATUSES = {
    STATUS_IDLE,
    STATUS_LISTENING,
    STATUS_THINKING,
    STATUS_EXECUTING,
    STATUS_SPEAKING,
    STATUS_ERROR,
}


class JarvisState:
    """Shared runtime state that the UI can query and observe."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = STATUS_IDLE
        self.transcript: str | None = None
        self.reply: str | None = None
        self.tool_events: list[dict[str, Any]] = []
        self.error_message: str | None = None
        self.last_user_text: str | None = None
        self.session_id: str | None = None
        self.wake_response: str | None = None
        self.started_at: str | None = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "transcript": self.transcript,
                "reply": self.reply,
                "tool_events": list(self.tool_events),
                "error_message": self.error_message,
                "last_user_text": self.last_user_text,
                "session_id": self.session_id,
                "wake_response": self.wake_response,
                "started_at": self.started_at,
            }

    # ---- thread-safe mutators ----
    def set_status(self, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        with self.lock:
            self.status = status

    def set_transcript(self, text: str) -> None:
        with self.lock:
            self.transcript = text

    def set_reply(self, text: str) -> None:
        with self.lock:
            self.reply = text

    def set_error(self, message: str) -> None:
        with self.lock:
            self.error_message = message
            self.status = STATUS_ERROR

    def note_user_text(self, text: str) -> None:
        with self.lock:
            self.last_user_text = text

    def note_session(self, session_id: str) -> None:
        with self.lock:
            self.session_id = session_id

    def note_wake_response(self, text: str) -> None:
        with self.lock:
            self.wake_response = text

    def mark_started(self) -> None:
        with self.lock:
            if self.started_at is None:
                self.started_at = _iso_now()

    def push_tool_event(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.tool_events.append(event)
            if len(self.tool_events) > 200:
                self.tool_events = self.tool_events[-200:]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


# ---------------------------------------------------------------------------
# WebSocket support (simple manual implementation, no extra deps)
# ---------------------------------------------------------------------------

MSG_JSON = "json"
MSG_WS = "ws"


class SimpleWebSocketHandler:
    """Very small WebSocket handler for the Jarvis event stream.

    This implementation is intentionally minimal. It supports:
    - HTTP upgrade to WebSocket
    - text frames only
    - ping/pong keep-alive
    - close frames

    It is enough for the UI to receive JSON events without adding
    a third-party WebSocket server dependency.
    """

    def __init__(self, send_callback: callable) -> None:
        self._send = send_callback
        self._conn = None
        self._closed = False

    # ---- frame helpers ----
    @staticmethod
    def _mask_payload(payload: bytes) -> bytes:
        mask = bytes([0, 0, 0, 0])
        out = bytearray(len(payload))
        for i in range(len(payload)):
            out[i] = payload[i] ^ mask[i % 4]
        return bytes(out)

    @staticmethod
    def _unmask_payload(payload: bytes, mask: bytes) -> bytes:
        out = bytearray(len(payload))
        for i in range(len(payload)):
            out[i] = payload[i] ^ mask[i % 4]
        return bytes(out)

    def send_text(self, text: str) -> None:
        if self._closed:
            return
        payload = text.encode("utf-8")
        frame = (
            b"\x81"
            + bytes([len(payload)])
            + payload
        )
        self._send(frame)

    def send_json(self, payload: Any) -> None:
        self.send_text(json.dumps(payload, ensure_ascii=False))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send(b"\x88\x02\x03\xe8")
        except Exception:
            pass


class WebSocketUpgradeRequest:
    def __init__(self, path: str, headers: dict[str, str], peer: str) -> None:
        self.path = path
        self.headers = headers
        self.peer = peer


class WebSocketServer:
    """Minimal WebSocket server built on top of a thread and a socket.

    This is used only for the Jarvis event stream.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._ws_acceptor: threading.Thread | None = None
        self._running = False
        self._server_socket = None
        self._clients: list[SimpleWebSocketHandler] = []
        self._clients_lock = threading.Lock()
        self._acceptor_stop = threading.Event()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._server_socket = _create_server_socket(self.host, self.port)
        self._ws_acceptor = threading.Thread(target=self._accept_loop, daemon=True)
        self._ws_acceptor.start()
        logger.info("ws listening on ws://%s:%s", self.host, self.port)

    def stop(self) -> None:
        self._running = False
        self._acceptor_stop.set()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except Exception:
                pass
        if self._ws_acceptor is not None:
            self._ws_acceptor.join(timeout=3)
        with self._clients_lock:
            for client in list(self._clients):
                try:
                    client.close()
                except Exception:
                    pass
            self._clients.clear()

    def broadcast_json(self, payload: Any) -> None:
        frame = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        data = b"\x81" + bytes([len(frame)]) + frame
        with self._clients_lock:
            for client in list(self._clients):
                try:
                    client._send(data)
                except Exception:
                    pass

    # ---- internal ----
    def _accept_loop(self) -> None:
        while self._running and not self._acceptor_stop.is_set():
            try:
                self._server_socket.settimeout(1.0)
                conn, _ = self._server_socket.accept()
            except OSError:
                continue
            except Exception:
                break
            t = threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                daemon=True,
            )
            t.start()

    def _handle_connection(self, conn) -> None:
        try:
            client = self._handshake(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return

        if client is None:
            try:
                conn.close()
            except Exception:
                pass
            return

        with self._clients_lock:
            self._clients.append(client)

        try:
            self._read_frames(conn, client)
        finally:
            with self._clients_lock:
                try:
                    self._clients.remove(client)
                except ValueError:
                    pass
            try:
                client.close()
            except Exception:
                pass

    def _handshake(self, conn) -> SimpleWebSocketHandler | None:
        conn.settimeout(5.0)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data += chunk
        header_block = data.split(b"\r\n\r\n", 1)[0]
        headers = self._parse_headers(header_block)
        sec_key = headers.get("sec-websocket-key", "")
        if not sec_key:
            return None
        import base64
        import hashlib

        accept = base64.b64encode(
            hashlib.sha1((sec_key + "258EAFA5-E914-47DA-95CA-5AB5AC402890").encode()).digest()
        ).decode("ascii")

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("utf-8")
        conn.sendall(response)
        return SimpleWebSocketHandler(lambda frame: conn.sendall(frame))

    def _read_frames(self, conn, client: SimpleWebSocketHandler) -> None:
        conn.settimeout(30.0)
        buf = b""
        while self._running:
            try:
                chunk = conn.recv(4096)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                if len(buf) < 2:
                    break
                first = buf[0]
                masked = (first & 0x80) != 0
                length = first & 0x7F
                idx = 1
                if length == 126:
                    if len(buf) < idx + 2:
                        break
                    length = int.from_bytes(buf[idx:idx + 2], "big")
                    idx += 2
                elif length == 127:
                    if len(buf) < idx + 8:
                        break
                    length = int.from_bytes(buf[idx:idx + 8], "big")
                    idx += 8
                if masked:
                    if len(buf) < idx + 4:
                        break
                    mask = buf[idx:idx + 4]
                    idx += 4
                if len(buf) < idx + length:
                    break
                payload = buf[idx:idx + length]
                buf = buf[idx + length:]
                if masked:
                    payload = client._unmask_payload(payload, mask)
                opcode = first & 0x0F
                if opcode == 0x08:
                    return
                if opcode == 0x09:
                    pong = bytes([0x8A, len(payload)]) + payload
                    try:
                        conn.sendall(pong)
                    except Exception:
                        return
                    continue
                if opcode == 0x01:
                    try:
                        text = payload.decode("utf-8")
                        self._handle_text(client, text)
                    except Exception:
                        pass
                continue

    def _handle_text(self, client: SimpleWebSocketHandler, text: str) -> None:
        try:
            data = json.loads(text)
        except Exception:
            return
        cmd = data.get("type")
        if cmd == "ping":
            client.send_json({"type": "pong"})


def _create_server_socket(host: str, port: int):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(5)
    return s


# ---------------------------------------------------------------------------
# Jarvis backend service
# ---------------------------------------------------------------------------

class JarvisBackendService:
    """Runs the Jarvis runtime and exposes HTTP + WebSocket control APIs."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        ws_port: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._explicit_ws_port = ws_port
        self.state = JarvisState()
        self._http_server: HTTPServer | None = None
        self._ws_server: WebSocketServer | None = None
        self._http_server_thread: threading.Thread | None = None
        self._voice_mode = False
        self._voice_thread: threading.Thread | None = None
        self._voice_future: threading.Event | None = None
        self._started = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.state.mark_started()
        self._ws_port = self._resolve_ws_port()
        self._ws_server = WebSocketServer(self.host, self._ws_port)
        self._ws_server.start()
        self._http_server = HTTPServer((self.host, self.port), _make_handler(self))
        self._http_server_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_server_thread.start()
        _log.info("backend api listening on http://%s:%s", self.host, self.port)
        _log.info("backend ws  listening on ws://%s:%s", self.host, self._ws_port)

    def stop(self) -> None:
        if self._voice_mode:
            self.stop_listening()
        if self._ws_server is not None:
            self._ws_server.stop()
            self._ws_server = None
        if self._http_server is not None:
            try:
                self._http_server.shutdown()
            except Exception:
                pass
            self._http_server = None
        if self._http_server_thread is not None:
            self._http_server_thread.join(timeout=5)
            self._http_server_thread = None
        if self._voice_thread is not None:
            self._voice_thread.join(timeout=5)
            self._voice_thread = None
        self.state.set_status(STATUS_IDLE)

    _ws_port: int

    def _resolve_ws_port(self) -> int:
        return self._explicit_ws_port if self._explicit_ws_port is not None else self.port + 1

    @property
    def ws_port(self) -> int:
        return self._resolve_ws_port()

    # ------------------------------------------------------------------
    # HTTP API
    # ------------------------------------------------------------------

    def health(self) -> bytes:
        return json.dumps({"ok": True, "service": "jarvis-backend"}, ensure_ascii=False).encode("utf-8")

    def _state_response(self) -> bytes:
        return json.dumps(self.state.snapshot(), ensure_ascii=False).encode("utf-8")

    def _respond(self, data: bytes) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            _log.exception("http response write failed")

    def start_listening(self) -> bytes:
        if self._voice_mode:
            return _json_error(409, "listening already active")
        self._voice_mode = True
        self._voice_future = threading.Event()
        self._voice_thread = threading.Thread(target=self._run_voice_loop, daemon=True)
        self._voice_thread.start()
        return json.dumps({"started": True}, ensure_ascii=False).encode("utf-8")

    def stop_listening(self) -> bytes:
        if not self._voice_mode:
            return _json_error(409, "not listening")
        self._voice_mode = False
        if self._voice_future is not None:
            self._voice_future.set()
        return json.dumps({"stopped": True}, ensure_ascii=False).encode("utf-8")

    def handle_command(self, payload: dict[str, Any]) -> bytes:
        text = (payload.get("text") or "").strip()
        if not text:
            return _json_error(400, "missing text")
        try:
            self._dispatch_command(text)
        except Exception as e:
            _log.exception("command dispatch failed")
            self.state.set_error(str(e))
            self.emit({"type": "error", "message": str(e)})
            return _json_error(500, str(e))
        return json.dumps({"received": text}, ensure_ascii=False).encode("utf-8")

    # ------------------------------------------------------------------
    # event stream
    # ------------------------------------------------------------------

    def emit(self, payload: Any) -> None:
        if self._ws_server is not None:
            self._ws_server.broadcast_json(payload)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _dispatch_command(self, text: str) -> None:
        self.state.set_status(STATUS_THINKING)
        self.state.note_user_text(text)
        self.emit({"type": "user_text", "text": text})

        reply = None
        status_seen = set()

        def update_status(status: str, transient: bool = False) -> None:
            self.state.set_status(status)
            status_seen.add(status)
            self.emit({"type": "status", "status": status})

        try:
            # Keep compatibility with the existing runtime helpers by
            # importing them lazily here.
            try:
                from run_voice_test import (
                    handle_turn,
                    handle_action,
                    handle_chat,
                    is_shutdown_command,
                    is_sleep_command,
                    orchestrator,
                    tool_runner,
                    audio,
                    session as active_session,
                    lifecycle,
                    bus,
                )
            except Exception as exc:
                msg = f"runtime unavailable: {exc}"
                _log.exception(msg)
                self.state.set_error(msg)
                self.emit({"type": "error", "message": msg})
                return

            self._attach_bus_bridge()

            decision = orchestrator.decide(text)
            self.state.note_session(active_session.id)
            self.state.set_transcript(text)

            if decision.kind == "reply":
                reply = decision.reply or ""
                update_status(STATUS_SPEAKING)
                self.emit({"type": "reply", "text": reply})
                self.state.set_reply(reply)
                audio.speak(reply)
                audio.wait()
                self._http_respond(json.dumps({"received": text}, ensure_ascii=False).encode("utf-8"))
                return

            if decision.kind == "action":
                update_status(STATUS_EXECUTING)
                self.emit({"type": "status", "status": STATUS_EXECUTING})
                result = self._run_action(text)
                reply = result.get("reply") or "Done."
                update_status(STATUS_SPEAKING)
                self.emit({"type": "reply", "text": reply})
                self.state.set_reply(reply)
                audio.speak(reply)
                audio.wait()
                self._http_respond(json.dumps({"received": text}, ensure_ascii=False).encode("utf-8"))
                return

            update_status(STATUS_THINKING)
            result = self._run_chat(text)
            reply = result.get("reply") or ""
            update_status(STATUS_SPEAKING)
            self.emit({"type": "reply", "text": reply})
            self.state.set_reply(reply)
            audio.speak(reply)
            audio.wait()
            self._http_respond(json.dumps({"received": text}, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            _log.exception("command failed")
            self.state.set_error(str(e))
            self.emit({"type": "error", "message": str(e)})
        finally:
            if self.state.status not in {STATUS_SPEAKING, STATUS_ERROR}:
                self.state.set_status(STATUS_IDLE)

    def _attach_bus_bridge(self) -> None:
        def _bridge(event):
            kind = getattr(event, "kind", "")
            payload = {
                "type": "bus_event",
                "event": kind,
            }
            if event.session_id:
                payload["session_id"] = event.session_id
            if event.task_id:
                payload["task_id"] = event.task_id
            if getattr(event, "meta", None):
                payload["meta"] = dict(event.meta)
            self.emit(payload)

        try:
            bus.subscribe(_bridge)
        except Exception:
            pass

    def _run_action(self, text: str) -> dict[str, Any]:
        from run_voice_test import (
            handle_action as _handle_action,
            session as active_session,
        )

        active_session.note_user_text(text)
        ok = _handle_action(text)
        reply = active_session.last_assistant_reply or "Done."
        return {"ok": ok, "reply": reply}

    def _run_chat(self, text: str) -> dict[str, Any]:
        from run_voice_test import (
            handle_chat as _handle_chat,
            session as active_session,
        )

        active_session.note_user_text(text)
        ok = _handle_chat(text)
        reply = active_session.last_assistant_reply or ""
        return {"ok": ok, "reply": reply}

    def _run_voice_loop(self) -> None:
        self.state.set_status(STATUS_LISTENING)
        self.emit({"type": "status", "status": STATUS_LISTENING})
        try:
            from run_voice_test import (
                conversation as _conversation,
                session as active_session,
                lifecycle,
                bus,
            )
            self._attach_bus_bridge()
            self.state.note_session(active_session.id)
            _conversation()
        except Exception as e:
            logger.exception("voice loop failed")
            self.state.set_error(str(e))
            self.emit({"type": "error", "message": str(e)})
        finally:
            self._voice_mode = False
            self._voice_future = None
            self._voice_thread = None
            self.state.set_status(STATUS_IDLE)
            self.emit({"type": "status", "status": STATUS_IDLE})


def _make_handler(service: JarvisBackendService):
    class Handler(BaseHTTPRequestHandler):
        _service = service

        def log_message(self, fmt, *args):
            logger.info(fmt, *args)

        def do_GET(self):
            if self.path == "/api/health":
                self._respond(self._service.health())
                return
            if self.path == "/api/state":
                self._respond(self._service._state_response())
                return
            if self._want_websocket():
                self._upgrade_to_websocket()
                return
            self._respond(_json_error(404, "not found"))

        def do_POST(self):
            ct = self.headers.get("Content-Type", "")
            if "json" not in ct:
                self._respond(_json_error(415, "content-type must be application/json"))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._respond(_json_error(400, "invalid json"))
                return

            if self.path == "/api/listen/start":
                self._respond(self._service.start_listening())
                return
            if self.path == "/api/listen/stop":
                self._respond(self._service.stop_listening())
                return
            if self.path == "/api/command":
                self._respond(self._service.handle_command(payload))
                return
            self._respond(_json_error(404, "not found"))

        def _want_websocket(self):
            upgrade = self.headers.get("Upgrade", "").lower()
            return upgrade == "websocket"

        def _upgrade_to_websocket(self):
            try:
                ws = SimpleWebSocketHandler(lambda frame: self._ws_send(frame))
                ws._conn = self.request
                # perform a minimal WebSocket handshake here and close the
                # connection cleanly if the browser does not handshake further
                try:
                    self._ws_handshake(ws)
                    t = threading.Thread(target=self._ws_run, args=(ws,), daemon=True)
                    t.start()
                except Exception:
                    try:
                        ws._send(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    except Exception:
                        pass
                    try:
                        ws._conn.close()
                    except Exception:
                        pass
                    return
                self._respond(b"")
            except Exception as e:
                _log.exception("websocket upgrade failed")
                try:
                    self._respond(_json_error(500, "websocket upgrade failed"))
                except Exception:
                    pass

        def _ws_handshake(self, ws: SimpleWebSocketHandler) -> None:
            conn = ws._conn
            conn.settimeout(5.0)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    raise RuntimeError("websocket handshake incomplete")
                data += chunk
            header_block = data.split(b"\r\n\r\n", 1)[0]
            headers = self._parse_headers(header_block)
            sec_key = headers.get("sec-websocket-key")
            if not sec_key:
                raise RuntimeError("missing sec-websocket-key")
            import base64
            import hashlib
            accept = base64.b64encode(
                hashlib.sha1((sec_key + "258EAFA5-E914-47DA-95CA-5AB5AC402890").encode()).digest()
            ).decode("ascii")
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("utf-8")
            conn.sendall(response)

        @staticmethod
        def _parse_headers(block: bytes) -> dict[str, str]:
            headers = {}
            for line in block.split(b"\r\n"):
                if b":" not in line:
                    continue
                key, value = line.split(b":", 1)
                headers[key.decode("utf-8").strip().lower()] = value.decode("utf-8").strip()
            return headers

        def _ws_send(self, frame):
            try:
                self.request.sendall(frame)
            except Exception:
                pass

        def _ws_run(self, ws):
            try:
                conn = self.request
                conn.settimeout(30.0)
                buf = b""
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except Exception:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        if len(buf) < 2:
                            break
                        first = buf[0]
                        masked = (first & 0x80) != 0
                        length = first & 0x7F
                        idx = 1
                        if length == 126:
                            if len(buf) < idx + 2:
                                break
                            length = int.from_bytes(buf[idx:idx + 2], "big")
                            idx += 2
                        elif length == 127:
                            if len(buf) < idx + 8:
                                break
                            length = int.from_bytes(buf[idx:idx + 8], "big")
                            idx += 8
                        if masked:
                            if len(buf) < idx + 4:
                                break
                            mask = buf[idx:idx + 4]
                            idx += 4
                        if len(buf) < idx + length:
                            break
                        payload = buf[idx:idx + length]
                        buf = buf[idx + length:]
                        if masked:
                            payload = ws._unmask_payload(payload, mask)
                        opcode = first & 0x0F
                        if opcode == 0x08:
                            return
                        if opcode == 0x09:
                            pong = bytes([0x8A, len(payload)]) + payload
                            try:
                                conn.sendall(pong)
                            except Exception:
                                return
                            continue
                        if opcode == 0x01:
                            try:
                                text = payload.decode("utf-8")
                                data = json.loads(text)
                            except Exception:
                                continue
                            cmd = data.get("type")
                            if cmd == "ping":
                                ws.send_json({"type": "pong"})
                            elif cmd == "subscribe":
                                ws.send_json({"type": "subscribed"})
            except Exception:
                pass

        def _respond(self, data: bytes) -> None:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                pass

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

    return Handler
