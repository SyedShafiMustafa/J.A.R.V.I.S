"""
server/auth.py
--------------
Lightweight token authentication for the JARVIS V2 server.

Phase 1 keeps this intentionally simple and modular:
- exactly one server-wide secret lives in config (JARVIS_SECRET_KEY)
- clients authenticate with an Authorization bearer token
- in development the server can fall back to a default token when the
  secret is empty, so you can start testing before a real secret exists
- production rejects requests when the secret is missing instead of
  silently allowing them through
- the same token is used for HTTP and WebSocket auth
- token values are never logged, only validate/invalidate

This is deliberately a thin dependency-neutral auth layer so the server
stays easy to reason about and the auth policy can be switched out later.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Awaitable, Callable, Union

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from server.config import ServerConfig


class AuthError(Exception):
    """Raised by the validator when a request lacks a valid token."""


def validate_token(
    cfg: ServerConfig,
    bearer: HTTPAuthorizationCredentials | None,
    header: str | None = None,
) -> str:
    """Validate a bearer token and return the token value on success.

    Raises:
        AuthError: when the request is unauthenticated or the token is wrong.
    """
    token = None
    if bearer is not None and bearer.credentials is not None:
        token = bearer.credentials
    elif header:
        token = header

    token = token or ""

    if cfg.env == "production":
        if not cfg.secret_key:
            raise AuthError("server is not configured with a secret")
        if token != cfg.secret_key:
            raise AuthError("invalid token")
        return token

    # Development: allow a sensible default so the project can be tested
    # before a real secret has been chosen, but still prefer the configured
    # secret when one is present.
    expected = cfg.secret_key or "dev-token"
    if token != expected:
        raise AuthError("invalid token")
    return token


def require_auth(request: Request) -> str:
    """Dependency that authenticates a request and returns the token used."""
    cfg = request.app.state.config
    bearer: HTTPAuthorizationCredentials | None = getattr(request.state, "_auth_bearer", None)
    if bearer is None:
        raise AuthError("missing bearer token")
    return validate_token(cfg, bearer)


# --------------------------------------------------------------------------- #
# Bearer scheme + helper dependency
# --------------------------------------------------------------------------- #


async def bearer_depends(request: Request) -> str:
    """FastAPI dependency: authenticate via Authorization: Bearer <token>."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        creds = auth_header.split(" ", 1)[1]
        request.state._auth_bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials=creds)
    else:
        request.state._auth_bearer = None
    cfg = request.app.state.config
    return validate_token(cfg, request.state._auth_bearer)


# --------------------------------------------------------------------------- #
# Cooking-safe header token (used by simple clients that cannot send headers)
# --------------------------------------------------------------------------- #


def header_token_dep(request: Request) -> str:
    """Auth via X-Jarvis-Token header. Convenience for non-browser clients."""
    cfg = request.app.state.config
    token = request.headers.get("X-Jarvis-Token", "")
    return validate_token(cfg, None, header=token)


def single_auth(*deps: Callable[..., Union[str, Awaitable[str]]]) -> Callable[..., Awaitable[str]]:
    """Pick the first dependency that returns a token; fall back through them.

    Keeps the router readable: you can say
        auth: str = Depends(single_auth(bearer_depends, header_token_dep))
    without wiring body/type logic in every endpoint.
    """
    async def _pick(request: Request) -> str:
        for dep in deps:
            try:
                result = dep(request)
                if inspect.iscoroutine(result):
                    value = await result
                else:
                    value = result
                if value:
                    return value
            except AuthError:
                continue
        raise AuthError("authentication required")
    return _pick
