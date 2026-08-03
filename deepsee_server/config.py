"""Server runtime settings: host & port.

Priority: environment variables > deepsee.toml [server] section > defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8712


@dataclass
class ServerSettings:
    host: str
    port: int


def _find_config() -> Path | None:
    cwd = Path.cwd() / "deepsee.toml"
    if cwd.is_file():
        return cwd
    home = Path.home() / ".config" / "deepsee" / "deepsee.toml"
    if home.is_file():
        return home
    return None


def server_settings() -> ServerSettings:
    """Load server host/port: env > deepsee.toml [server] > defaults."""
    raw: dict = {}
    file = _find_config()
    if file is not None:
        with open(file, "rb") as fh:
            raw = tomllib.load(fh)

    server = raw.get("server", {}) if isinstance(raw.get("server"), dict) else {}
    host = os.environ.get("DeepSee_SERVER_HOST") or str(server.get("host", DEFAULT_HOST))
    port_raw = os.environ.get("DeepSee_SERVER_PORT") or server.get("port", DEFAULT_PORT)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        raise ValueError(f"server.port 必须是整数,当前: {port_raw!r}")
    return ServerSettings(host=host, port=port)
