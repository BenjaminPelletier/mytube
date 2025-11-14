"""Command-line interface for running the MyTube web server."""

from __future__ import annotations

import argparse

import uvicorn

from . import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MyTube Chromecast remote web server.")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on.")
    args = parser.parse_args()

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":  # pragma: no cover - direct execution guard
    main()
