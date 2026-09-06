"""
server/brain.py
---------------
HTTP adapter for the JARVIS V2 Lenovo AI Brain.

The Dell server calls the Lenovo brain over Tailscale using the
configured JARVIS_BRAIN_URL. The brain is the heavy-AI side of the
system: reasoning, planning, and LLM inference.

This adapter is deliberately backend-neutral and replaceable:
- it talks HTTP/JSON only
- timeouts, retries, and errors are explicit
- responses from the brain are parsed into a small stable contract
- healthz is used to probe whether the brain is reachable

The interface is designed so the Lenovo brain can later be swapped
for another provider without changing server-side callers.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from server.config import ServerConfig


class BrainError(Exception):
    """Base class for brain client failures."""


class BrainUnavailable(BrainError):
    """The brain could not be reached or is refusing connections."""


class BrainTimeout(BrainError):
    """The brain did not respond within the configured timeout."""


class BrainBadResponse(BrainError):
    """The brain responded, but not with the expected contract."""


class BrainAuthError(BrainError):
    """The brain rejected the request (401/403)."""


class BrainClient:
    """Client for the Lenovo AI Brain HTTP API."""

    def __init__(
        self,
        cfg: ServerConfig | None = None,
        *,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        retries: int = 0,
    ) -> None:
        if cfg is not None:
            base_url = cfg.brain_url
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = timeout_s
        self._retries = max(0, int(retries))

    @property
    def base_url(self) -> str:
        return self._base_url

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout, connect=self._timeout),
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ #
    # health
    # ------------------------------------------------------------------ #

    def healthz(self) -> dict[str, Any]:
        """Ping the brain's liveness endpoint.

        Returns the parsed JSON body. Raises BrainUnavailable if the
        brain is not reachable.
        """
        with self._client() as client:
            try:
                response = client.get("/healthz")
            except httpx.ConnectError as exc:
                raise BrainUnavailable("brain unreachable") from exc
            except httpx.ConnectTimeout as exc:
                raise BrainTimeout("brain connection timed out") from exc
            except httpx.RequestError as exc:
                raise BrainUnavailable(f"brain request failed: {exc}") from exc

            if response.status_code != 200:
                raise BrainUnavailable(f"brain returned {response.status_code}")

            try:
                return response.json()
            except Exception as exc:
                raise BrainBadResponse("brain healthz was not JSON") from exc

    # ------------------------------------------------------------------ #
    # chat
    # ------------------------------------------------------------------ #

    def chat(
        self,
        *,
        messages: list[dict[str, str]],
        conversation_id: str | None = None,
        context: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Send a chat request to the brain and return the parsed response.

        Expected brain response shape:
            {
                "response": "...",           # required string
                "model": "...",             # optional
                "usage": {...}              # optional
            }

        Raises:
            BrainUnavailable / BrainTimeout / BrainBadResponse / BrainAuthError
        """
        payload: dict[str, Any] = {"messages": messages}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if context:
            payload["context"] = context

        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        last_error: BrainError | None = None
        for attempt in range(1 + self._retries):
            try:
                with self._client() as client:
                    response = client.post(
                        "/v1/chat",
                        json=payload,
                        headers=headers,
                    )
            except httpx.ConnectError as exc:
                last_error = BrainUnavailable("brain unreachable")
                raise BrainUnavailable("brain unreachable") from exc
            except httpx.ConnectTimeout as exc:
                last_error = BrainTimeout("brain connection timed out")
                raise BrainTimeout("brain connection timed out") from exc
            except httpx.ReadTimeout as exc:
                last_error = BrainTimeout("brain read timed out")
                raise BrainTimeout("brain read timed out") from exc
            except httpx.RequestError as exc:
                last_error = BrainUnavailable(f"brain request failed: {exc}")
                raise BrainUnavailable(f"brain request failed: {exc}") from exc

            if response.status_code == 401 or response.status_code == 403:
                raise BrainAuthError("brain rejected the request")

            if response.status_code != 200:
                raise BrainUnavailable(f"brain returned {response.status_code}")

            try:
                body = response.json()
            except Exception as exc:
                raise BrainBadResponse("brain response was not JSON") from exc

            if not isinstance(body, dict):
                raise BrainBadResponse("brain response was not a JSON object")

            if "response" not in body or not isinstance(body["response"], str):
                raise BrainBadResponse("brain response missing 'response' string")

            return body

        if last_error is not None:
            raise last_error
        raise BrainBadResponse("brain request failed without a usable error")

    def chat_response(
        self,
        *,
        messages: list[dict[str, str]],
        conversation_id: str | None = None,
        context: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> str:
        """Convenience wrapper: chat() -> response text."""
        return self.chat(
            messages=messages,
            conversation_id=conversation_id,
            context=context,
            token=token,
        )["response"]

    # ------------------------------------------------------------------ #
    # readiness
    # ------------------------------------------------------------------ #

    def is_healthy(self) -> bool:
        """True when the brain is reachable and responding to healthz."""
        try:
            self.healthz()
            return True
        except BrainError:
            return False
