"""Server runtime settings: host & port.

Priority: environment variables > deepsee.toml [server] section > defaults.
"""

from __future__ import annotations

import math
import os

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # Python 3.10: use the official backport
    import tomli as tomllib  # type: ignore[no-redef]

from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8712


@dataclass
class ServerSettings:
    host: str
    port: int


@dataclass(frozen=True)
class GuardSettings:
    max_concurrent: int
    queue_timeout: float
    rate_limit: int
    rate_window: float


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


def request_guard_settings() -> GuardSettings:
    def positive_int(name: str, default: int) -> int:
        raw = os.environ.get(name, str(default))
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是正整数,当前: {raw!r}") from exc
        if value <= 0:
            raise ValueError(f"{name} 必须是正整数,当前: {raw!r}")
        return value

    def positive_float(name: str, default: float) -> float:
        raw = os.environ.get(name, str(default))
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是正数,当前: {raw!r}") from exc
        if value <= 0 or not math.isfinite(value):
            raise ValueError(f"{name} 必须是有限正数,当前: {raw!r}")
        return value

    return GuardSettings(
        max_concurrent=positive_int("DeepSee_MAX_CONCURRENT_REQUESTS", 8),
        queue_timeout=positive_float("DeepSee_REQUEST_QUEUE_TIMEOUT", 2.0),
        rate_limit=positive_int("DeepSee_RATE_LIMIT_REQUESTS", 60),
        rate_window=positive_float("DeepSee_RATE_LIMIT_WINDOW", 60.0),
    )
