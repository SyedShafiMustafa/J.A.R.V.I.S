"""
server/run.py
-------------
Entry point for the JARVIS V2 Dell server.

    python server/run.py [--host 127.0.0.1] [--port 8000]

Runs the FastAPI app with uvicorn.  Configuration comes from the environment
/ `.env` (see server/config.py); the CLI flags override host and port only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `import server.*` work no matter where this script is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-server", description="JARVIS V2 Dell server")
    parser.add_argument("--host", default=None, help="bind address (default: JARVIS_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: JARVIS_PORT or 8000)")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed. Run:  "
            "python -m pip install -r requirements-server.txt",
            file=sys.stderr,
        )
        return 2

    from server.app import create_app
    from server.config import load_config

    overrides: dict = {}
    if args.host is not None:
        overrides["HOST"] = args.host
    if args.port is not None:
        overrides["PORT"] = args.port

    cfg = load_config(overrides=overrides or None)
    app = create_app(cfg)

    print(f"JARVIS V2 server ({cfg.label}) -> http://{cfg.host}:{cfg.port}")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
