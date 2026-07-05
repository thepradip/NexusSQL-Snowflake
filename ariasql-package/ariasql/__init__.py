"""
AriaSQL — Agentic SQL Agent
Natural language to SQL with multi-step ReAct reasoning, semantic cache,
schema-aware retrieval, and a bundled React UI.

Quick start:
    ariasql serve          # starts API + UI on http://localhost:8000
    ariasql ingest         # loads the demo SQLite database

Author: Pradip Tivhale <pradiptivhale@gmail.com>
"""

__version__ = "1.0.1"
__author__ = "Pradip Tivhale"


def get_app():
    """Return the FastAPI app (lazy — requires env vars to be set first)."""
    from .server import app
    return app


def get_settings():
    """Return the settings object (lazy — reads env vars / .env on first call)."""
    from .config import get_settings as _get
    return _get()


__all__ = ["get_app", "get_settings", "__version__"]
