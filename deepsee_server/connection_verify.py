"""Sanitized, independent upstream connection verification."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable

import httpx

from deepsee import ask_async, describe_image_async
from deepsee.config.loader import Config
from deepsee.errors import DeepSeeError

Probe = Callable[[Config], Awaitable[None]]

_TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGP8//8/AwwwwVl4OQCWbgMF7ZjH1AAAAABJRU5ErkJggg=="
)


async def _deepseek_probe(config: Config) -> None:
    await ask_async("Reply OK", config=config, max_tokens=1)


async def _vision_probe(config: Config) -> None:
    await describe_image_async(_TEST_PNG, "Reply OK", config=config, max_tokens=1)


def _safe_error(exc: Exception) -> dict:
    status = exc.status_code if isinstance(exc, DeepSeeError) else None
    if status in (401, 403):
        return {"code": "AUTH", "message": "认证失败"}
    if status == 429:
        return {"code": "RATE_LIMIT", "message": "请求过于频繁"}
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(
            cause,
            (httpx.TimeoutException, httpx.NetworkError, asyncio.TimeoutError),
        ):
            return {"code": "TRANSPORT", "message": "网络连接失败"}
        cause = cause.__cause__ or cause.__context__
    if isinstance(exc, DeepSeeError):
        return {"code": "UPSTREAM", "message": "上游服务请求失败"}
    return {"code": "INTERNAL", "message": "连接验证失败"}


async def _verify_one(config: Config, probe: Probe) -> dict:
    started = time.monotonic()
    try:
        await probe(config)
    except Exception as exc:
        return {
            "ok": False,
            "latencyMs": max(0, int((time.monotonic() - started) * 1000)),
            "error": _safe_error(exc),
        }
    return {
        "ok": True,
        "latencyMs": max(0, int((time.monotonic() - started) * 1000)),
    }


async def verify_upstream_connections(
    config: Config,
    *,
    deepseek_probe: Probe = _deepseek_probe,
    vision_probe: Probe = _vision_probe,
) -> dict:
    """Verify both providers and return only stable, secret-free results."""

    deepseek = await _verify_one(config, deepseek_probe)
    vision = await _verify_one(config, vision_probe)
    return {"deepseek": deepseek, "vision": vision}
