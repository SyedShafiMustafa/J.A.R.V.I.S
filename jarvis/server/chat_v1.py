"""
server/chat_v1.py
------------------
Minimal Dell-side conversation proxy for Phase 1.

One endpoint:

    POST /api/v1/chat

It accepts a chat request, forwards it to the configured Lenovo brain via the
existing server.brain.BrainClient, and returns the brain response.

This is intentionally small and backend-neutral:
- no new LLM implementation
- no auth/pairing/memory/voice/telephony/HUD added here
- uses the existing JARVIS_BRAIN_URL config and BrainClient contract
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, status

from server.brain import BrainAuthError, BrainBadResponse, BrainClient, BrainError, BrainUnavailable, BrainTimeout
from server.config import ServerConfig


router = APIRouter(prefix="/api/v1", tags=["chat"])


@router.post("/chat", status_code=status.HTTP_200_OK, response_model=None)
async def chat_proxy(request: Request) -> dict:
    """Forward a chat request to the configured Lenovo brain and return its response.

    Expected request body:
        {
            "messages": [{"role": "user", "content": "..."}],
            "conversation_id": "...",     # optional
            "context": {...},             # optional
            "token": "..."                # optional, forwarded to brain if supported
        }

    The brain response must include at least a "response" string. The proxy
    returns that response as-is.
    """
    cfg: ServerConfig = request.app.state.config
    body: dict | None = None

    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be valid JSON")

    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")

    body = raw

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty list")

    conversation_id = body.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise HTTPException(status_code=400, detail="conversation_id must be a string if provided")

    context = body.get("context")
    if context is not None and not isinstance(context, dict):
        raise HTTPException(status_code=400, detail="context must be an object if provided")

    token = body.get("token")
    if token is not None and not isinstance(token, str):
        raise HTTPException(status_code=400, detail="token must be a string if provided")

    # Keep per-request client construction minimal and explicit.
    brain = BrainClient(cfg, timeout_s=30.0, retries=0)

    try:
        response = brain.chat(
            messages=[
                {
                    "role": str(item.get("role") or "user"),
                    "content": str(item.get("content") or ""),
                }
                for item in messages
                if isinstance(item, dict)
            ],
            conversation_id=conversation_id,
            context=context,
            token=token,
        )
    except BrainUnavailable as exc:
        raise HTTPException(status_code=503, detail="AI brain unavailable") from exc
    except BrainTimeout as exc:
        raise HTTPException(status_code=504, detail="AI brain timed out") from exc
    except BrainBadResponse as exc:
        raise HTTPException(status_code=502, detail="AI brain returned an invalid response") from exc
    except BrainAuthError as exc:
        raise HTTPException(status_code=503, detail="AI brain rejected the request") from exc
    except BrainError as exc:
        raise HTTPException(status_code=502, detail="AI brain error") from exc

    return response
