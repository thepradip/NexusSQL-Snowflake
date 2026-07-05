"""
AriaSQL CLI — entry points for `ariasql serve` and `ariasql ingest`.
"""
from __future__ import annotations

import argparse
import sys


def serve() -> None:
    """Start the AriaSQL server (API + bundled React UI)."""
    import uvicorn

    parser = argparse.ArgumentParser(prog="ariasql serve", description="Start AriaSQL server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev)")
    args = parser.parse_args(sys.argv[1:])

    print(f"AriaSQL starting on http://{args.host}:{args.port}")
    uvicorn.run(
        "ariasql.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def ingest() -> None:
    """Run the schema ingestion script (populates the demo SQLite database)."""
    from .ingest import main as _ingest_main
    _ingest_main()


def main() -> None:
    parser = argparse.ArgumentParser(prog="ariasql", description="AriaSQL — Agentic SQL Agent")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Start the server")
    sub.add_parser("ingest", help="Ingest demo database schema")
    args, remaining = parser.parse_known_args()

    sys.argv = [sys.argv[0]] + remaining
    if args.command == "serve":
        serve()
    elif args.command == "ingest":
        ingest()
    else:
        parser.print_help()
        sys.exit(1)
