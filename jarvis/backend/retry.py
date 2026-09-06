"""
backend/retry.py

Retry helper for transient backend failures.

It is intentionally small and explicit:
- only retry on TransientError by default
- configurable attempts, base delay, backoff, and jitter
- total timeout cap available for long-running operations
- all delays are real sleeps, so this is meant for backend
  operations where a short pause and retry is acceptable

This is not a generic retry framework. It exists so the backend
can handle temporary failures like brief network blips, audio
device contention, or short-lived service unavailability without
immediately giving up or retrying forever.
"""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

from backend.interfaces import TransientError, ToolResult

T = TypeVar("T")


class RetryConfig:
    """Configuration for a retry attempt policy."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay_s: float = 0.5,
        backoff: float = 2.0,
        max_delay_s: float = 10.0,
        jitter: bool = True,
        timeout_s: float | None = None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.backoff = backoff
        self.max_delay_s = max_delay_s
        self.jitter = jitter
        self.timeout_s = timeout_s


def retry(
    func: Callable[[], T],
    config: RetryConfig | None = None,
    retryable: type[Exception] = TransientError,
) -> T:
    """
    Run `func` and retry on transient failures.

    If `func` succeeds, its return value is returned.
    If it keeps failing after the configured attempts, the last
    exception is re-raised.
    """
    config = config or RetryConfig()

    last_error: Exception | None = None
    attempt = 0
    start = time.monotonic()
    retry_count = 0

    while True:
        attempt += 1

        try:
            result = func()
            return _attach_meta(result, attempt, retry_count)
        except retryable as e:
            last_error = e
            retry_count += 1

            if attempt >= config.max_attempts:
                break

            delay = _next_delay(config, attempt)

            if config.timeout_s is not None:
                elapsed = time.monotonic() - start
                if elapsed + delay > config.timeout_s:
                    break

            time.sleep(delay)
            continue
        except Exception:
            raise

    if last_error is not None:
        raise last_error

    raise RuntimeError("retry exited without result")


def attach_retry_meta(result: ToolResult, attempt: int, retries_used: int) -> ToolResult:
    """Attach lightweight retry metadata to a tool result."""
    merged = {"attempt": attempt, "retries_used": retries_used}
    if result.meta:
        merged.update(result.meta)
    return ToolResult(
        tool=result.tool,
        success=result.success,
        message=result.message,
        data=result.data,
        meta=merged,
    )


def _attach_meta(value: T, attempt: int, retries_used: int) -> T:
    if isinstance(value, ToolResult):
        return attach_retry_meta(value, attempt, retries_used)  # type: ignore[return-value]
    return value


def _next_delay(config: RetryConfig, attempt: int) -> float:
    """
    Compute the next delay using exponential backoff and optional jitter.
    """
    delay = config.base_delay_s * (config.backoff ** (attempt - 1))
    delay = min(delay, config.max_delay_s)

    if config.jitter:
        delay = delay * (0.5 + random.random())

    return max(0.0, delay)
