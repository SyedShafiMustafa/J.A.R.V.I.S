"""
backend/validation.py

Central startup validation for the backend.

The goal is to fail early with clear messages if something required
is missing or inconsistent, instead of discovering it later deep
inside audio, tools, or LLM calls.

Rules of thumb:
- Configuration problems should raise ConfigurationError
- Missing runtime dependencies should also surface here when
  they can be checked cheaply at startup
- Don't make this too aggressive; only check things that are
  safe and useful to verify before the main loop starts
"""

from __future__ import annotations

from pathlib import Path

from backend.interfaces import ConfigurationError


def validate_model_paths() -> None:
    """Check that local model/assets the backend expects actually exist."""

    from config.config import PIPER_MODEL, PIPER_CONFIG

    missing: list[str] = []

    if not PIPER_MODEL.exists():
        missing.append(f"Piper model not found: {PIPER_MODEL}")
    if not PIPER_CONFIG.exists():
        missing.append(f"Piper config not found: {PIPER_CONFIG}")

    if missing:
        raise ConfigurationError(
            "Backend startup failed because required model files are missing:\n"
            + "\n".join(" - " + m for m in missing)
        )


def validate_required_settings() -> None:
    """
    Check that required runtime settings are present.

    This is intentionally small. It exists to catch obvious
    configuration problems before the backend enters the main loop.
    """

    from config.settings import LLM_PROVIDER, TTS_PROVIDER

    issues: list[str] = []

    if not LLM_PROVIDER:
        issues.append("LLM_PROVIDER is empty")

    if not TTS_PROVIDER:
        issues.append("TTS_PROVIDER is empty")

    if issues:
        raise ConfigurationError(
            "Backend startup failed because required settings are missing:\n"
            + "\n".join(" - " + i for i in issues)
        )


def validate_backend_startup() -> None:
    """
    Run all cheap, high-value startup checks in one place.

    This is the function the main entry point should call before
    entering the conversation loop.
    """

    validate_required_settings()
    validate_model_paths()
