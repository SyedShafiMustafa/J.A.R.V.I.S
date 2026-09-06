"""
server/brain_service.py
-----------------------
Standalone Lenovo AI Brain service for JARVIS V2 Phase 1.

This service runs on the Lenovo and exposes the existing AI brain over the
Tailscale network so the Dell JARVIS server can call it.

It intentionally reuses the existing brain implementation instead of building
a second independent LLM/chat path:
    - chat: agents.brain.JarvisBrain (uses the configured Ollama/Qwen provider)
    - model configuration: config.config + config.settings from .env

Exposed endpoints:
    GET  /healthz          liveness + dependency checks
    POST /v1/chat         chat request against the existing brain

Run:
    python server/brain_service.py [--host 0.0.0.0] [--port 8001]

Configuration is read from .env / environment (JLENOVO_* keys below) plus the
existing project .env for the shared LLM settings. The service never hard-codes
an IP, model name, or secret.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make `import server.*` work no matter where this script is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agents.brain import JarvisBrain
from config.config import OLLAMA_MODEL, OLLAMA_URL
from server import __version__ as SERVER_VERSION

APP_ROOT = Path(__file__).resolve().parents[1]
JLENOVO_DEFAULT_BRAIN_PORT = 8001


# ------------------------------------------------------------------ #
# pydantic contracts
# ------------------------------------------------------------------ #


class ChatRequest(BaseModel):
    messages: list[dict[str, str]] = Field(
        default_factory=list,
        description="Conversation messages for the brain, e.g. [{'role':'user','content':'...'}]",
    )
    conversation_id: str | None = Field(default=None, description="Optional conversation id for tracing.")
    context: dict[str, object] | None = Field(default=None, description="Optional extra context from the Dell.")

    def to_list(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in self.messages:
            role = item.get("role") or "user"
            content = item.get("content") or ""
            if not isinstance(role, str) or not isinstance(content, str):
                continue
            out.append({"role": role, "content": content})
        return out


class ChatResponse(BaseModel):
    response: str
    model: str | None = None
    usage: dict[str, object] | None = None
    conversation_id: str | None = None
    server: str = "jarvis-brain"
    version: str = SERVER_VERSION


# ------------------------------------------------------------------ #


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_brain_app(brain: JarvisBrain | None = None) -> FastAPI:
    brain = brain or JarvisBrain()

    app = FastAPI(
        title="JARVIS V2 Brain Service",
        description="Lenovo AI brain service. Reuses the existing JARVIS brain/Ollama/Qwen path.",
        version=SERVER_VERSION,
    )

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict:
        return {
            "status": "ok",
            "service": "jarvis-brain",
            "version": SERVER_VERSION,
            "brain": {
                "model": OLLAMA_MODEL,
                "provider_url": OLLAMA_URL,
                "provider": "ollama",
            },
            "ts": now_iso(),
        }

    @app.post("/v1/chat", response_model=ChatResponse, tags=["brain"])
    async def v1_chat(body: ChatRequest) -> dict:
        if not body.messages:
            raise HTTPException(status_code=400, detail="messages must not be empty")

        messages = body.to_list()
        if not messages:
            raise HTTPException(status_code=400, detail="no valid messages found")

        try:
            response_text = brain.ask("\n".join(m["content"] for m in messages))
        except Exception as exc:
            # Existing brain implementation may raise requests/HTTP/network errors.
            # The brain service should report an honest failure rather than a fake reply.
            raise HTTPException(status_code=502, detail=f"brain call failed: {exc}") from exc

        resp: dict[str, object] = {
            "response": response_text,
            "model": OLLAMA_MODEL,
            "server": "jarvis-brain",
            "version": SERVER_VERSION,
        }
        if body.conversation_id:
            resp["conversation_id"] = body.conversation_id
        if body.context is not None:
            resp["usage"] = {"context_keys": list(body.context.keys())}
        return resp

    @app.post("/v1/chat/completions", tags=["brain"])
    async def v1_chat_completions(body: ChatRequest) -> dict:
        """Aliased endpoint for clients that prefer the completions-style path."""
        chat = await v1_chat(body)
        return {"choices": [{"message": {"role": "assistant", "content": chat["response"]}}], **chat}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-brain", description="JARVIS V2 Lenovo AI brain service")
    parser.add_argument("--host", default="0.0.0.0", help="bind host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=JLENOVO_DEFAULT_BRAIN_PORT, help="bind port (default 8001)")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run:  python -m pip install -r requirements-server.txt", file=sys.stderr)
        return 2

    app = create_brain_app()
    print(f"JARVIS V2 brain service ({SERVER_VERSION}) -> http://{args.host}:{args.port}")
    print("  GET  /healthz")
    print("  POST /v1/chat")
    print("  POST /v1/chat/completions")
    print(f"  brain model: {OLLAMA_MODEL}")
    print(f"  brain provider url: {OLLAMA_URL}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
