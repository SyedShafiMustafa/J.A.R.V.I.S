"""
server/config.py
----------------
Configuration for the JARVIS V2 Dell server.

Every setting can be overridden through the environment (JARVIS_* keys) or a
`.env` file at the application root.  `load_config()` validates everything up
front and raises `ConfigError` listing *all* problems, so the server fails
fast with a clear message instead of limping along half-configured.

Only infrastructure settings live here for now.  Per-module settings (memory,
telephony, LLM, ...) are added in later phases but always through this same
loader, so there is exactly one configuration path.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

APP_ROOT = Path(__file__).resolve().parents[1]  # the `jarvis/` application root
DEFAULT_ENV_FILE = APP_ROOT / ".env"

_VALID_ENVS = {"development", "production"}
_VALID_ROLES = {"server", "brain"}  # Dell = server, Lenovo = brain
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigError(Exception):
    """Raised when the configuration is missing or inconsistent."""


@dataclass
class ServerConfig:
    env: str                # development | production
    role: str               # server (Dell) | brain (Lenovo, later phases)
    host: str               # bind address (default loopback only)
    port: int
    node_name: str
    log_level: str
    log_dir: Path
    data_dir: Path
    db_path: Path
    cors_origins: list[str]
    secret_key: str         # required (>= 16 chars) when env == production
    brain_url: str          # Lenovo heavy-AI endpoint (used from phase 1+)

    @property
    def label(self) -> str:
        return f"{self.role}@{self.node_name} ({self.env})"


# --------------------------------------------------------------------------- #
# .env seeding
# --------------------------------------------------------------------------- #

def seed_dotenv(env_file: Path | None = None) -> None:
    """Load key=value lines from *env_file* into os.environ (never overriding).

    Prefers python-dotenv when installed; falls back to a tiny parser so the
    server still works on a bare Python install.
    """
    env_file = Path(env_file) if env_file else DEFAULT_ENV_FILE
    if not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_file, override=False)
        return
    except Exception:
        pass  # python-dotenv missing -> fallback parser below

    for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #

def _get(overrides: dict | None, env_map: Mapping[str, str] | None, name: str, default=None):
    if overrides is not None and name in overrides:
        return overrides[name]
    if env_map is not None and f"JARVIS_{name}" in env_map:
        return env_map[f"JARVIS_{name}"]
    return default


def _as_dir(overrides, env_map, name: str, default: Path) -> Path:
    raw = _get(overrides, env_map, name, None)
    if raw is None:
        return default
    path = Path(str(raw))
    if not path.is_absolute():
        path = APP_ROOT / path
    return path


def load_config(
    overrides: dict | None = None,
    environ: Mapping[str, str] | None = None,
) -> ServerConfig:
    """Build and validate a ServerConfig.

    - *environ*: pass a dict to test with a fake environment.  When None the
      real process environment is used (after seeding the `.env` file).
    - *overrides*: short-name -> value dict (e.g. {"PORT": 9000}) applied on
      top of the environment.  Used by tests and the CLI runner.
    """
    if environ is None:
        seed_dotenv()
        env_map: Mapping[str, str] = os.environ
    else:
        env_map = environ

    errors: list[str] = []

    env = str(_get(overrides, env_map, "ENV", "development")).strip().lower()
    role = str(_get(overrides, env_map, "ROLE", "server")).strip().lower()

    host = str(_get(overrides, env_map, "HOST", "127.0.0.1")).strip()
    try:
        port = int(_get(overrides, env_map, "PORT", 8000))
    except (TypeError, ValueError):
        port = -1
        errors.append("JARVIS_PORT must be an integer")

    log_level = str(_get(overrides, env_map, "LOG_LEVEL", "INFO")).strip().upper()
    node_name = str(
        _get(overrides, env_map, "NODE_NAME", f"{role}-{socket.gethostname().lower()}")
    ).strip()

    log_dir = _as_dir(overrides, env_map, "LOG_DIR", APP_ROOT / "logs")
    data_dir = _as_dir(overrides, env_map, "DATA_DIR", APP_ROOT / "data")

    db_raw = _get(overrides, env_map, "DB_PATH", None)
    db_path = _as_dir(overrides, env_map, "DB_PATH", data_dir / "jarvis.db") if db_raw else data_dir / "jarvis.db"

    secret_key = str(_get(overrides, env_map, "SECRET_KEY", "") or "")
    cors_origins = [
        part.strip()
        for part in str(_get(overrides, env_map, "CORS_ORIGINS", "*")).split(",")
        if part.strip()
    ] or ["*"]
    brain_url = str(_get(overrides, env_map, "BRAIN_URL", "") or "").strip()

    # --- validation (collect every problem before raising) ------------------ #
    if env not in _VALID_ENVS:
        errors.append(f"JARVIS_ENV must be one of {sorted(_VALID_ENVS)}, got {env!r}")
    if role not in _VALID_ROLES:
        errors.append(f"JARVIS_ROLE must be one of {sorted(_VALID_ROLES)}, got {role!r}")
    if not host:
        errors.append("JARVIS_HOST must not be empty")
    if not (1 <= port <= 65535):
        errors.append(f"JARVIS_PORT must be between 1 and 65535, got {port}")
    if log_level not in _VALID_LOG_LEVELS:
        errors.append(f"JARVIS_LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {log_level!r}")
    if env == "production" and len(secret_key) < 16:
        errors.append(
            "JARVIS_SECRET_KEY must be set to at least 16 characters when "
            "JARVIS_ENV=production"
        )

    if errors:
        raise ConfigError("Invalid configuration: " + "; ".join(errors))

    return ServerConfig(
        env=env,
        role=role,
        host=host,
        port=port,
        node_name=node_name,
        log_level=log_level,
        log_dir=log_dir,
        data_dir=data_dir,
        db_path=db_path,
        cors_origins=cors_origins,
        secret_key=secret_key,
        brain_url=brain_url,
    )
