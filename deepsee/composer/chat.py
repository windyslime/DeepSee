"""Lossless DeepSeek Chat Completions transport."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx

from deepsee.backends.base import retry_request_async, stream_request_async
from deepsee.config.loader import Config, load_config
from deepsee.errors import ComposeError


async def chat_async(
    messages: Sequence[Mapping[str, Any]],
    *,
    stream: bool = False,
    config: Config | None = None,
    params: Mapping[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
    """Call DeepSeek without flattening messages or response objects."""
    cfg = config if config is not None else load_config()
    payload = copy.deepcopy(dict(params or {}))
    payload.update(
        {
            "model": model or cfg.deepseek.model,
            "messages": copy.deepcopy(list(messages)),
            "stream": stream,
        }
    )
    if stream:
        return _stream_json(cfg, payload)
    return await _request_json(cfg, payload)


async def _request_json(cfg: Config, payload: dict[str, Any]) -> dict[str, Any]:
    client = httpx.AsyncClient(timeout=120.0, trust_env=False)
    try:
        response = await retry_request_async(
            client,
            "POST",
            f"{cfg.deepseek.base_url.rstrip('/')}/chat/completions",
            retries=cfg.retries,
            json=payload,
            headers={"Authorization": f"Bearer {cfg.deepseek.api_key}"},
        )
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("response root must be an object")
        return data
    except httpx.HTTPStatusError as exc:
        raise ComposeError(
            f"DeepSeek API 请求失败: HTTP {exc.response.status_code}",
            model=cfg.deepseek.model,
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise ComposeError(
            f"DeepSeek API 网络错误: {exc.__class__.__name__}",
            model=cfg.deepseek.model,
        ) from exc
    except (ValueError, TypeError) as exc:
        raise ComposeError(
            "DeepSeek API 响应解析失败",
            model=cfg.deepseek.model,
        ) from exc
    finally:
        await client.aclose()


async def _bounded_lines(
    lines: AsyncIterator[str], timeout: float
) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError("stream total duration exceeded")
        try:
            line = await asyncio.wait_for(lines.__anext__(), remaining)
        except StopAsyncIteration:
            return
        yield line


async def _stream_json(
    cfg: Config, payload: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    client = httpx.AsyncClient(timeout=120.0, trust_env=False)
    response: httpx.Response | None = None
    try:
        response = await stream_request_async(
            client,
            "POST",
            f"{cfg.deepseek.base_url.rstrip('/')}/chat/completions",
            retries=cfg.retries,
            json=payload,
            headers={"Authorization": f"Bearer {cfg.deepseek.api_key}"},
        )
        try:
            async for line in _bounded_lines(response.aiter_lines(), 300.0):
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    return
                chunk = json.loads(raw)
                if not isinstance(chunk, dict):
                    raise ValueError("stream chunk must be an object")
                yield chunk
        except asyncio.TimeoutError as exc:
            raise ComposeError(
                "DeepSeek 流式响应超过总时长限制",
                model=cfg.deepseek.model,
            ) from exc
    except httpx.HTTPStatusError as exc:
        raise ComposeError(
            f"DeepSeek API 请求失败: HTTP {exc.response.status_code}",
            model=cfg.deepseek.model,
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise ComposeError(
            f"DeepSeek API 网络错误: {exc.__class__.__name__}",
            model=cfg.deepseek.model,
        ) from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ComposeError(
            "DeepSeek 流式响应解析失败",
            model=cfg.deepseek.model,
        ) from exc
    finally:
        if response is not None:
            await response.aclose()
        await client.aclose()
