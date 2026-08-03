"""Run the DeepSee server: ``python -m deepsee_server``.

Host/port come from environment or deepsee.toml ``[server]`` (default
127.0.0.1:8712); command-line flags override them for one-off runs.
"""

from __future__ import annotations

import argparse

import uvicorn

from deepsee_server.app import app
from deepsee_server.config import server_settings


def main() -> None:
    settings = server_settings()
    parser = argparse.ArgumentParser(description="DeepSee OpenAI-compatible server")
    parser.add_argument("--host", default=settings.host, help=f"监听地址(默认 {settings.host})")
    parser.add_argument("--port", type=int, default=settings.port, help=f"监听端口(默认 {settings.port})")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
