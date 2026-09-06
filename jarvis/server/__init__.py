"""
JARVIS V2 — Dell always-on server package.

This package is the foundation of the always-on JARVIS server that will
eventually run on the Dell machine (see the V2 roadmap in README.md).

Phase 0 scope (infrastructure):
    - server/config.py        environment-driven configuration + fail-fast validation
    - server/logging_setup.py rotating file + console logs
    - server/db.py            SQLite bootstrap (WAL, schema versioning)
    - server/app.py           FastAPI app factory with /healthz + /readyz
    - server/run.py           `python server/run.py` entry point

Later phases layer JARVIS core, voice, memory, tools, telephony etc. on top
of this package.  Nothing here may import the legacy voice runtime.
"""

__version__ = "0.1.0"
