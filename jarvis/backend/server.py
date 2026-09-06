"""
backend/server.py

Real Jarvis backend service layer.

This is the piece that makes the backend controllable from the UI
and streamable over WebSocket.

It wraps the existing Jarvis runtime pieces (built explicitly through
backend.live_runtime.build_live_runtime, never by importing a script
with startup side effects):
- audio (wake word, STT, TTS)
- router / planner / brain / executor
- tool orchestration
- session / lifecycle / bus

It exposes, all on a single localhost port:
- GET  /api/health
- GET  /api/state
- POST /api/listen/start
- POST /api/listen/stop
- POST /api/command
- WS   /ws for real-time events

Design notes:
- the voice loop stays on the backend
- the UI is a control panel that can start/stop listening and send
  text commands
- HTTP errors use real status codes (4xx/5xx), never masked as 200
- the runtime is constructed lazily on first use; if the live
  dependencies are missing the server reports a clean 503 instead
  of crashing
"""

from __future__ import annotations

import json
import logging
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.interfaces import ToolCall
from backend.live_runtime import RuntimeUnavailableError, build_live_runtime
from backend.bus import (
    task_started,
    task_completed,
    task_failed,
    transcription_ready,
    user_interrupt,
)

_log = logging.getLogger("jarvis.backend")
logger = logging.getLogger("jarvis.backend")

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

WAKE_RESPONSES = [
    "Yes?",
    "I'm here.",
    "Go ahead.",
    "Ready.",
    "At your service.",
    "How can I help?",
    "Listening.",
    "What's the mission?",
    "Tell me.",
    "What do you need?",
    "Always ready.",
]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


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


# ---------------------------------------------------------------------------
# Jarvis backend service
# ---------------------------------------------------------------------------

class JarvisBackendService:
    """Runs the Jarvis runtime and exposes HTTP + WebSocket control APIs."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        runtime_builder: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._runtime_builder = runtime_builder or build_live_runtime
        self.state = JarvisState()
        self._http_server: ThreadingHTTPServer | None = None
        self._http_server_thread: threading.Thread | None = None
        self._voice_mode = False
        self._voice_thread: threading.Thread | None = None
        self._voice_future: threading.Event | None = None
        self._runtime: dict[str, Any] | None = None
        self._started = False

        # WebSocket clients: handler instances currently upgraded.
        self._ws_clients: list[Any] = []
        self._ws_clients_lock = threading.Lock()
        self._bus_bridge_attached = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.state.mark_started()
        self._http_server = ThreadingHTTPServer((self.host, self.port), _make_handler(self))
        self._http_server.daemon_threads = True
        self._http_server_thread = threading.Thread(
            target=self._http_server.serve_forever,
            daemon=True,
        )
        self._http_server_thread.start()
        _log.info("backend api + ws listening on http://%s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._voice_mode:
            self.stop_listening()
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
        with self._ws_clients_lock:
            self._ws_clients.clear()
        self.state.set_status(STATUS_IDLE)

    # ------------------------------------------------------------------
    # runtime (lazy, explicit, no import side effects)
    # ------------------------------------------------------------------

    def _get_runtime(self) -> dict[str, Any]:
        if self._runtime is None:
            self._runtime = self._runtime_builder(session_id="voice-session")
        return self._runtime

    def runtime_unavailable_message(self) -> str | None:
        try:
            self._get_runtime()
            return None
        except RuntimeUnavailableError as exc:
            return str(exc)

    # ------------------------------------------------------------------
    # HTTP API (all methods return (status_code, payload))
    # ------------------------------------------------------------------

    def health(self) -> tuple[int, dict[str, Any]]:
        return 200, {"ok": True, "service": "jarvis-backend"}

    def _state_response(self) -> tuple[int, dict[str, Any]]:
        return 200, self.state.snapshot()

    def start_listening(self) -> tuple[int, dict[str, Any]]:
        if self._voice_mode:
            return 409, {"error": "listening already active"}
        self._voice_mode = True
        self._voice_future = threading.Event()
        self._voice_thread = threading.Thread(target=self._run_voice_loop, daemon=True)
        self._voice_thread.start()
        return 200, {"started": True}

    def stop_listening(self) -> tuple[int, dict[str, Any]]:
        if not self._voice_mode:
            return 409, {"error": "not listening"}
        self._voice_mode = False
        if self._voice_future is not None:
            self._voice_future.set()
        return 200, {"stopped": True}

    def handle_command(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        text = (payload.get("text") or "").strip()
        if not text:
            return 400, {"error": "missing text"}
        try:
            self._dispatch_command(text)
        except RuntimeUnavailableError as exc:
            self.state.set_error(str(exc))
            self.emit({"type": "error", "message": str(exc)})
            return 503, {"error": str(exc)}
        except Exception as exc:
            _log.exception("command dispatch failed")
            self.state.set_error(str(exc))
            self.emit({"type": "error", "message": str(exc)})
            return 500, {"error": str(exc)}
        return 200, {"received": text}

    # ------------------------------------------------------------------
    # event stream
    # ------------------------------------------------------------------

    def emit(self, payload: Any) -> None:
        if self._http_server is None:
            return
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with self._ws_clients_lock:
            for client in list(self._ws_clients):
                try:
                    client._ws_send_frame(0x1, data)
                except Exception:
                    pass

    def _register_ws_client(self, client: Any) -> None:
        with self._ws_clients_lock:
            self._ws_clients.append(client)

    def _unregister_ws_client(self, client: Any) -> None:
        with self._ws_clients_lock:
            try:
                self._ws_clients.remove(client)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # command dispatch
    # ------------------------------------------------------------------

    def _dispatch_command(self, text: str) -> None:
        self.state.set_status(STATUS_THINKING)
        self.state.note_user_text(text)
        self.emit({"type": "user_text", "text": text})

        runtime = self._get_runtime()
        session = runtime["session"]
        audio = runtime["audio"]
        orchestrator = runtime["orchestrator"]
        bus = runtime["bus"]

        self._attach_bus_bridge(bus)
        self.state.note_session(session.id)
        self.state.set_transcript(text)

        session.note_user(text)
        decision = orchestrator.decide(text)
        session.note_decision(decision.metadata or {})

        if decision.kind == "reply":
            reply = decision.reply or ""
            self._speak(audio, reply)
            return

        if decision.kind == "action":
            self.state.set_status(STATUS_EXECUTING)
            self.emit({"type": "status", "status": STATUS_EXECUTING})
            self._run_action(runtime, text)
            return

        self._run_chat(runtime, text)

    def _run_action(self, runtime: dict[str, Any], text: str) -> None:
        audio = runtime["audio"]
        session = runtime["session"]
        lifecycle = runtime["lifecycle"]
        tool_runner = runtime["tool_runner"]
        orchestrator = runtime["orchestrator"]
        bus = runtime["bus"]

        session.note_user(text)

        try:
            task = orchestrator.plan_action(text)
        except Exception as exc:
            _log.exception("planning failed")
            message = "I couldn't plan that task. Please check that Ollama is running."
            self.state.set_error(f"planning failed: {exc}")
            self.emit({"type": "error", "message": f"planning failed: {exc}"})
            self._speak(audio, message)
            return

        session.active_task = task
        bus.publish(task_started(task, session_id=session.id))
        task.start()

        try:
            for step in task.steps:
                if lifecycle.shutdown_requested:
                    session.cancel_active_task()
                    return

                call = ToolCall(
                    tool=step["tool"],
                    payload={k: v for k, v in step.items() if k != "tool"},
                )
                result = tool_runner.run(call, task=task)

                if not result.success:
                    task.fail(result.message or "tool failed")
                    bus.publish(task_failed(task, session_id=session.id))
                    self._speak(audio, "I couldn't complete that task.")
                    return

            task.complete("done")
            bus.publish(task_completed(task, session_id=session.id))
            self._speak(audio, "Done.")
        except Exception as exc:
            _log.exception("action failed")
            task.fail(f"unexpected error: {exc}")
            bus.publish(task_failed(task, session_id=session.id))
            self._speak(audio, "Something went wrong.")
        finally:
            session.active_task = None

    def _run_chat(self, runtime: dict[str, Any], text: str) -> None:
        audio = runtime["audio"]
        brain = runtime["brain"]
        memory = runtime["memory"]
        session = runtime["session"]

        memories = memory.search_memories(text)
        context = f"Relevant memories:\n{memories}\n\nUser: {text}" if memories else text

        full_reply = ""
        for sentence in brain.stream(context):
            full_reply += sentence + " "
            self._speak(audio, sentence)

        reply = full_reply.strip()
        if reply:
            memory.save_memory(text, reply)
            session.note_reply(reply)
        else:
            self.state.set_status(STATUS_IDLE)
            self.emit({"type": "status", "status": STATUS_IDLE})

    def _speak(self, audio: Any, text: str) -> None:
        self.state.set_status(STATUS_SPEAKING)
        self.emit({"type": "status", "status": STATUS_SPEAKING})
        self.emit({"type": "reply", "text": text})
        self.state.set_reply(text)
        try:
            audio.speak(text)
            audio.wait()
        except Exception as exc:
            _log.exception("speak failed")
            self.state.set_error(f"speak failed: {exc}")
            self.emit({"type": "error", "message": f"speak failed: {exc}"})
        finally:
            if self.state.status not in {STATUS_ERROR}:
                self.state.set_status(STATUS_IDLE)
                self.emit({"type": "status", "status": STATUS_IDLE})

    # ------------------------------------------------------------------
    # voice loop (wake-driven, server-side)
    # ------------------------------------------------------------------

    def _run_voice_loop(self) -> None:
        self.state.set_status(STATUS_LISTENING)
        self.emit({"type": "status", "status": STATUS_LISTENING})
        try:
            runtime = self._get_runtime()
            bus = runtime["bus"]
            self._attach_bus_bridge(bus)
            self._voice_conversation(runtime)
        except RuntimeUnavailableError as exc:
            _log.error("voice loop unavailable: %s", exc)
            self.state.set_error(str(exc))
            self.emit({"type": "error", "message": str(exc)})
        except Exception as exc:
            _log.exception("voice loop failed")
            self.state.set_error(str(exc))
            self.emit({"type": "error", "message": str(exc)})
        finally:
            self._voice_mode = False
            self._voice_future = None
            self._voice_thread = None
            self.state.set_status(STATUS_IDLE)
            self.emit({"type": "status", "status": STATUS_IDLE})

    def _voice_conversation(self, runtime: dict[str, Any]) -> None:
        audio = runtime["audio"]
        session = runtime["session"]
        lifecycle = runtime["lifecycle"]
        orchestrator = runtime["orchestrator"]
        bus = runtime["bus"]

        self.state.note_session(session.id)

        wake = random.choice(WAKE_RESPONSES)
        self.state.note_wake_response(wake)
        self._speak(audio, wake)

        if lifecycle.shutdown_requested:
            return

        timeout = time.time() + 30

        while time.time() < timeout:
            if self._voice_future is not None and self._voice_future.is_set():
                return

            try:
                audio_path = audio.record_audio()
            except Exception as exc:
                _log.exception("record failed")
                self.state.set_error(f"record failed: {exc}")
                self.emit({"type": "error", "message": f"record failed: {exc}"})
                return

            user = (audio.transcribe(audio_path) or "").strip()

            if not user:
                continue

            self.state.set_transcript(user)
            self.emit({"type": "user_text", "text": user})
            bus.publish(transcription_ready(session_id=session.id, user_text=user))

            if _is_shutdown_phrase(user):
                reply = "Shutting down. Goodbye, Shafi."
                self._speak(audio, reply)
                lifecycle.request_shutdown()
                lifecycle.shutdown()
                return

            if _is_sleep_phrase(user):
                reply = "Going back to sleep."
                self._speak(audio, reply)
                lifecycle.request_shutdown()
                return

            if lifecycle.shutdown_requested:
                bus.publish(user_interrupt(session_id=session.id))
                return

            session.note_user(user)
            decision = orchestrator.decide(user)
            session.note_decision(decision.metadata or {})

            if decision.kind == "reply":
                self._speak(audio, decision.reply or "")
            elif decision.kind == "action":
                self._run_action(runtime, user)
            else:
                self._run_chat(runtime, user)

            timeout = time.time() + 30

    # ------------------------------------------------------------------
    # bus -> websocket bridge
    # ------------------------------------------------------------------

    def _attach_bus_bridge(self, bus: Any) -> None:
        if self._bus_bridge_attached:
            return
        self._bus_bridge_attached = True

        def _bridge(event: Any) -> None:
            kind = getattr(event, "kind", "")
            payload: dict[str, Any] = {
                "type": "bus_event",
                "event": kind,
            }
            if getattr(event, "session_id", None):
                payload["session_id"] = event.session_id
            if getattr(event, "task_id", None):
                payload["task_id"] = event.task_id
            meta = getattr(event, "meta", None)
            if meta:
                payload["meta"] = dict(meta)
            self.emit(payload)

        try:
            bus.subscribe(_bridge)
        except Exception:
            _log.exception("bus bridge subscribe failed")


def _is_shutdown_phrase(text: str) -> bool:
    import re
    cleaned = re.sub(r"[^a-zA-Z\s]", "", text.lower()).strip()
    phrases = [
        "jarvis shutdown",
        "shutdown jarvis",
        "shut down",
        "goodbye",
        "bye",
        "see you later",
        "exit",
        "quit",
        "im done",
        "i am done",
        "thats all",
    ]
    return any(p in cleaned for p in phrases)


def _is_sleep_phrase(text: str) -> bool:
    cleaned = text.lower()
    return any(p in cleaned for p in ["go to sleep", "sleep mode", "go idle"])


# ---------------------------------------------------------------------------
# HTTP handler (also serves WebSocket upgrades on the same port)
# ---------------------------------------------------------------------------

def _make_handler(service: JarvisBackendService):
    class Handler(BaseHTTPRequestHandler):
        _service = service
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            logger.info(fmt, *args)

        # ---- routing ----

        def do_GET(self):
            if self._want_websocket():
                self._upgrade_to_websocket()
                return

            if self.path == "/api/health":
                self._respond(*self._service.health())
                return
            if self.path == "/api/state":
                self._respond(*self._service._state_response())
                return

            self._respond(404, {"error": "not found"})

        def do_POST(self):
            ct = self.headers.get("Content-Type", "")
            if "json" not in ct:
                self._respond(415, {"error": "content-type must be application/json"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0

            body = self.rfile.read(length) if length else b"{}"

            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._respond(400, {"error": "invalid json"})
                return

            if self.path == "/api/listen/start":
                self._respond(*self._service.start_listening())
                return
            if self.path == "/api/listen/stop":
                self._respond(*self._service.stop_listening())
                return
            if self.path == "/api/command":
                self._respond(*self._service.handle_command(payload))
                return

            self._respond(404, {"error": "not found"})

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _respond(self, code: int, payload: dict[str, Any]) -> None:
            try:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                _log.exception("http response write failed")

        # ---- WebSocket upgrade (single implementation, same port) ----

        def _want_websocket(self) -> bool:
            upgrade = self.headers.get("Upgrade", "").lower()
            return upgrade == "websocket"

        def _upgrade_to_websocket(self) -> None:
            try:
                self._ws_handshake()
            except Exception:
                _log.exception("websocket handshake failed")
                try:
                    self.connection.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                except Exception:
                    pass
                return

            self.close_connection = True
            self._service._register_ws_client(self)
            try:
                self._ws_read_loop()
            finally:
                self._service._unregister_ws_client(self)

        def _ws_handshake(self) -> None:
            import base64
            import hashlib

            sec_key = self.headers.get("Sec-WebSocket-Key")
            if not sec_key:
                raise RuntimeError("missing sec-websocket-key")

            accept = base64.b64encode(
                hashlib.sha1(
                    (sec_key + "258EAFA5-E914-47DA-95CA-5AB5AC402890").encode()
                ).digest()
            ).decode("ascii")

            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("utf-8")
            self.connection.sendall(response)

        def _ws_read_loop(self) -> None:
            rfile = self.rfile
            while True:
                hdr = rfile.read(2)
                if len(hdr) < 2:
                    break

                first, second = hdr[0], hdr[1]
                opcode = first & 0x0F
                masked = (second & 0x80) != 0
                length = second & 0x7F

                if length == 126:
                    ext = rfile.read(2)
                    if len(ext) < 2:
                        break
                    length = int.from_bytes(ext, "big")
                elif length == 127:
                    ext = rfile.read(8)
                    if len(ext) < 8:
                        break
                    length = int.from_bytes(ext, "big")

                mask = rfile.read(4) if masked else b""
                if masked and len(mask) < 4:
                    break

                payload = rfile.read(length) if length else b""
                if len(payload) < length:
                    break

                if masked:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

                if opcode == 0x8:  # close
                    self._ws_send_frame(0x8, payload)
                    break
                elif opcode == 0x9:  # ping
                    self._ws_send_frame(0xA, payload)
                elif opcode == 0xA:  # pong
                    continue
                elif opcode == 0x1:  # text
                    try:
                        text = payload.decode("utf-8")
                        self._ws_handle_text(text)
                    except Exception:
                        pass

        def _ws_handle_text(self, text: str) -> None:
            try:
                data = json.loads(text)
            except Exception:
                return

            cmd = data.get("type")
            if cmd == "ping":
                self._ws_send_json({"type": "pong"})
            elif cmd == "subscribe":
                self._ws_send_json({"type": "subscribed"})
                self._ws_send_json({"type": "state", "state": self._service.state.snapshot()})

        def _ws_send_json(self, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._ws_send_frame(0x1, data)

        def _ws_send_frame(self, opcode: int, payload: bytes) -> None:
            header = bytearray([0x80 | opcode])
            n = len(payload)
            if n < 126:
                header.append(n)
            elif n < 65536:
                header.append(126)
                header += n.to_bytes(2, "big")
            else:
                header.append(127)
                header += n.to_bytes(8, "big")
            try:
                self.connection.sendall(bytes(header) + payload)
            except Exception:
                pass

    return Handler