"""
server/api_v1.py
----------------
Phase 1 API surface for the JARVIS V2 Dell server.

Endpoints:
- POST   /api/v1/auth/token           exchange a pair-code for a device token
    (kept minimal for now; real OAuth-style flows come later)
- POST   /api/v1/devices              register/pair a device
- GET    /api/v1/devices              list registered devices (authenticated)
- POST   /api/v1/conversations        start or continue a conversation
- GET    /api/v1/conversations/{id}   retrieve conversation metadata + messages
- WS     /api/v1/inbox               real-time inbox for a conversation

Design notes:
- authentication is modular: bearer / header / query are all supported
  via server.auth so clients choose the most convenient path
- conversation state lives in server.memory on the Dell
- the Lenovo brain is called through server.brain, never imported here
- errors are honest: 401, 400, 404, 409, 502/503 where appropriate
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse

from server.auth import (
    AuthError,
    bearer_depends,
    header_token_dep,
    query_token_dep,
    single_auth,
)
from server.brain import BrainAuthError, BrainBadResponse, BrainClient, BrainError, BrainTimeout, BrainUnavailable
from server.memory import ConversationStore, DeviceStore

router = APIRouter(prefix="/api/v1", tags=["v1"])


# ------------------------------------------------------------------ #
# auth helper
# ------------------------------------------------------------------ #


async def _auth(request: Request) -> str:
    return await single_auth(bearer_depends, header_token_dep, query_token_dep)()


# ------------------------------------------------------------------ #
# devices
# ------------------------------------------------------------------ #


@router.post("/auth/token", status_code=status.HTTP_200_OK, response_model=None)
async def exchange_pair_token(
    request: Request,
    body: dict,
    token: Annotated[str, Depends(_auth)],
) -> JSONResponse:
    """Minimal token exchange stub for device pairing.

    Today this endpoint is protected by server auth and returns a
    machine-readable message. Later phases can replace it with a real
    pair-code / device provisioning flow without changing callers that
    already expect a JSON body.
    """
    code = (body.get("code") or "").strip()
    device_name = (body.get("device_name") or "").strip()
    device_type = (body.get("device_type") or "unknown").strip()

    if not code:
        raise HTTPException(status_code=400, detail="pair code is required")

    # In Phase 1 we do not yet implement a real provisioning backend.
    # This endpoint exists so the pairing contract is in place and the
    # router tests can walk the authenticated path.
    return JSONResponse(
        {
            "ok": True,
            "message": "pairing stub",
            "device_name": device_name or "unknown",
            "device_type": device_type or "unknown",
        }
    )


@router.post("/devices", status_code=status.HTTP_201_CREATED, response_model=None)
async def register_device(
    request: Request,
    body: dict,
    token: Annotated[str, Depends(_auth)],
) -> JSONResponse:
    """Register or update a paired device on the server."""
    device_id = (body.get("device_id") or "").strip()
    name = (body.get("name") or "").strip()
    type_ = (body.get("type") or "unknown").strip()
    device_token = (body.get("token") or "").strip()
    metadata = body.get("metadata") or {}

    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")
    if not name:
        raise HTTPException(status_code=400, detail="device name is required")
    if not device_token:
        raise HTTPException(status_code=400, detail="device token is required")

    device_store = DeviceStore(request.app.state.config)
    try:
        device = device_store.register(
            device_id=device_id,
            name=name,
            type=type_,
            token=device_token,
            metadata=metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse(device, status_code=status.HTTP_201_CREATED)


@router.get("/devices", response_model=None)
async def list_devices(
    request: Request,
    token: Annotated[str, Depends(_auth)],
    limit: int = 100,
) -> JSONResponse:
    """List registered devices (authenticated)."""
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500
    device_store = DeviceStore(request.app.state.config)
    devices = device_store.list_all()[:limit]
    return JSONResponse(devices)


# ------------------------------------------------------------------ #
# conversations
# ------------------------------------------------------------------ #


@router.post("/conversations", status_code=status.HTTP_200_OK, response_model=None)
async def send_message(
    request: Request,
    body: dict,
    token: Annotated[str, Depends(_auth)],
) -> JSONResponse:
    """Send a user message and get a JARVIS response.

    Input:
        {
            "message": "Hello Jarvis",
            "device_id": "...",
            "conversation_id": "..."   # optional; created if missing
        }

    Output:
        {
            "response": "...",
            "conversation_id": "...",
            "model": "...",
            "usage": {...},
            "conversation": {...}
        }
    """
    message = (body.get("message") or "").strip()
    device_id = (body.get("device_id") or "").strip()
    conversation_id = (body.get("conversation_id") or "").strip() or uuid.uuid4().hex

    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    cfg = request.app.state.config
    brain = BrainClient(cfg)
    convo_store = ConversationStore(cfg)
    device_store = DeviceStore(cfg)

    # touch device presence
    device = device_store.get(device_id)
    if device is None:
        # auto-provision a device if one does not exist yet, so early
        # clients can still talk to the server during development.
        device = device_store.register(
            device_id=device_id,
            name=device_id,
            type="unknown",
            token=token,
        )

    device_store.touch(device_id)

    # persist request side
    convo_store.ensure_conversation(conversation_id, device_id)
    convo_store.save_message(conversation_id, "user", message)

    # build brain context from existing conversation history
    context_messages = _recent_messages(convo_store, conversation_id, limit=20)
    context = {"device_id": device_id, "conversation_id": conversation_id}

    try:
        brain_resp = brain.chat(
            messages=context_messages + [{"role": "user", "content": message}],
            conversation_id=conversation_id,
            context=context,
            token=token,
        )
    except BrainAuthError as exc:
        raise HTTPException(status_code=503, detail="AI brain rejected the request") from exc
    except BrainTimeout as exc:
        raise HTTPException(status_code=504, detail="AI brain timed out") from exc
    except BrainUnavailable as exc:
        raise HTTPException(status_code=503, detail="AI brain unavailable") from exc
    except BrainBadResponse as exc:
        raise HTTPException(status_code=502, detail="AI brain returned an invalid response") from exc
    except BrainError as exc:
        raise HTTPException(status_code=502, detail="AI brain error") from exc

    reply = brain_resp.get("response", "")
    convo_store.save_message(conversation_id, "assistant", reply)

    convo = convo_store.get(conversation_id)
    if convo is None:
        convo = {"id": conversation_id, "message_count": 0}

    return JSONResponse(
        {
            "response": reply,
            "conversation_id": conversation_id,
            "model": brain_resp.get("model"),
            "usage": brain_resp.get("usage"),
            "conversation": convo,
        }
    )


@router.get("/conversations/{conversation_id}", response_model=None)
async def get_conversation(
    request: Request,
    conversation_id: str,
    token: Annotated[str, Depends(_auth)],
    before_id: int | None = None,
    limit: int = 200,
) -> JSONResponse:
    """Retrieve conversation metadata + recent messages."""
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000

    cfg = request.app.state.config
    convo_store = ConversationStore(cfg)
    convo = convo_store.get(conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    messages = convo_store.list_messages(conversation_id, limit=limit, after_id=before_id)
    return JSONResponse({"conversation": convo, "messages": messages})


def _recent_messages(store: ConversationStore, conversation_id: str, limit: int) -> list[dict[str, str]]:
    rows = store.list_messages(conversation_id, limit=limit)
    out: list[dict[str, str]] = []
    for row in rows:
        content = row.get("content") or ""
        role = row.get("role") or "user"
        if not content.strip():
            continue
        out.append({"role": role, "content": content})
    return out


# ------------------------------------------------------------------ #
# WebSocket inbox
# ------------------------------------------------------------------ #


@router.websocket("/inbox")
async def inbox_ws(websocket: WebSocket) -> None:
    """Real-time inbox for a conversation.

    Client workflow:
        1. connect
        2. authenticate via query ?token= or header Authorization: Bearer ...
        3. send {"type": "join", "conversation_id": "..."}
        4. receive status + reply events
        5. send {"type": "message", "message": "...", "device_id": "..."}

    Authentication for WebSockets is handled on connect via query/header
    because WebSocket upgrades do not reliably carry Authorization headers
    across all clients.
    """
    await websocket.accept()

    token = _ws_auth(websocket)
    if token is None:
        await _ws_fail(websocket, "unauthenticated")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    cfg = websocket.app.state.config
    brain = BrainClient(cfg)
    convo_store = ConversationStore(cfg)
    device_store = DeviceStore(cfg)

    conversation_id: str | None = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                await _ws_send(websocket, {"type": "error", "message": "invalid json"})
                continue

            kind = data.get("type")
            if kind == "ping":
                await _ws_send(websocket, {"type": "pong"})
                continue

            if kind == "join":
                conversation_id = (data.get("conversation_id") or "").strip() or uuid.uuid4().hex
                device_id = (data.get("device_id") or conversation_id or "").strip() or uuid.uuid4().hex

                if not device_id:
                    await _ws_send(websocket, {"type": "error", "message": "device_id is required"})
                    continue

                device = device_store.get(device_id)
                if device is None:
                    device_store.register(
                        device_id=device_id,
                        name=device_id,
                        type="unknown",
                        token=token,
                    )

                device_store.touch(device_id)
                convo_store.ensure_conversation(conversation_id, device_id)

                await _ws_send(websocket, {
                    "type": "joined",
                    "conversation_id": conversation_id,
                    "conversation": convo_store.get(conversation_id),
                })
                continue

            if kind != "message":
                await _ws_send(websocket, {"type": "error", "message": "unknown event type"})
                continue

            if conversation_id is None:
                await _ws_send(websocket, {"type": "error", "message": "join a conversation first"})
                continue

            message = (data.get("message") or "").strip()
            device_id = (data.get("device_id") or "").strip() or conversation_id

            if not message:
                await _ws_send(websocket, {"type": "error", "message": "message is required"})
                continue

            device = device_store.get(device_id)
            if device is None:
                device_store.register(
                    device_id=device_id,
                    name=device_id,
                    type="unknown",
                    token=token,
                )

            device_store.touch(device_id)
            convo_store.ensure_conversation(conversation_id, device_id)
            convo_store.save_message(conversation_id, "user", message)

            await _ws_send(websocket, {
                "type": "status",
                "status": "thinking",
                "conversation_id": conversation_id,
            })

            try:
                context_messages = _recent_messages(convo_store, conversation_id, limit=20)
                context = {"device_id": device_id, "conversation_id": conversation_id}
                brain_resp = brain.chat(
                    messages=context_messages + [{"role": "user", "content": message}],
                    conversation_id=conversation_id,
                    context=context,
                    token=token,
                )
            except BrainAuthError:
                await _ws_send(websocket, {"type": "error", "message": "AI brain rejected the request"})
                continue
            except BrainTimeout:
                await _ws_send(websocket, {"type": "error", "message": "AI brain timed out"})
                continue
            except BrainUnavailable:
                await _ws_send(websocket, {"type": "error", "message": "AI brain unavailable"})
                continue
            except BrainBadResponse:
                await _ws_send(websocket, {"type": "error", "message": "AI brain returned an invalid response"})
                continue
            except BrainError:
                await _ws_send(websocket, {"type": "error", "message": "AI brain error"})
                continue

            reply = brain_resp.get("response", "")
            convo_store.save_message(conversation_id, "assistant", reply)

            await _ws_send(websocket, {
                "type": "reply",
                "conversation_id": conversation_id,
                "response": reply,
                "model": brain_resp.get("model"),
                "usage": brain_resp.get("usage"),
            })

    except Exception:
        await _ws_send(websocket, {"type": "error", "message": "connection error"})
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


def _ws_auth(websocket: WebSocket) -> str | None:
    token = websocket.query_params.get("token", "")
    if token:
        try:
            from server.auth import validate_token
            return validate_token(websocket.app.state.config, None, query=token)
        except AuthError:
            return None

    headers = dict(websocket.headers)
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            from server.auth import validate_token
            return validate_token(websocket.app.state.config, None, header=auth.split(" ", 1)[1])
        except AuthError:
            return None

    return None


async def _ws_send(websocket: WebSocket, payload: dict) -> None:
    await websocket.send_json(payload)


async def _ws_fail(websocket: WebSocket, message: str) -> None:
    await _ws_send(websocket, {"type": "error", "message": message})
