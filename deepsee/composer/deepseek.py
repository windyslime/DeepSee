"""DeepSeek composition layer.

The vision backend produces a text description of the image; that
description is injected into the DeepSeek conversation as visual context,
so DeepSeek can answer questions about the image.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Union

import httpx

from deepsee.backends import create_backend
from deepsee.backends.base import retry_request
from deepsee.config.loader import Config, load_config
from deepsee.errors import ComposeError
from deepsee.pipeline.image import ImageInput
from deepsee.pipeline.prompts import build_vision_prompt

_SYSTEM_TEMPLATE = (
    "你是 DeepSee 多模态助手。用户提供了一张图片,以下是视觉模型对该图片的描述:\n\n"
    "{description}\n\n"
    "请基于以上描述回答用户的问题。如果描述信息不足以回答,请如实说明。"
)


def describe_image(
    image: ImageInput,
    prompt: str,
    *,
    config: Config | None = None,
) -> str:
    """Run the vision backend: image + prompt → text description."""
    cfg = config if config is not None else load_config()
    backend = create_backend(cfg.vision, cfg.retries)
    try:
        return backend.describe(image, prompt)
    finally:
        backend.close()


def _compose_messages(question: str, description: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": _SYSTEM_TEMPLATE.format(description=description),
        },
        {"role": "user", "content": question},
    ]


def _request_deepseek(cfg: Config, payload: dict) -> httpx.Response:
    url = f"{cfg.deepseek.base_url.rstrip('/')}/chat/completions"
    client = httpx.Client(timeout=120.0)
    try:
        return retry_request(
            client,
            "POST",
            url,
            retries=cfg.retries,
            json=payload,
            headers={"Authorization": f"Bearer {cfg.deepseek.api_key}"},
        )
    except httpx.HTTPStatusError as exc:
        raise ComposeError(
            f"DeepSeek API 请求失败: HTTP {exc.response.status_code}",
            model=cfg.deepseek.model,
            status_code=exc.response.status_code,
        ) from exc
    finally:
        client.close()


def ask_with_image(
    image: ImageInput,
    question: str,
    *,
    stream: bool = False,
    config: Config | None = None,
) -> Union[str, Iterator[str]]:
    """Full composition: vision description → DeepSeek reasoning.

    ``stream=False`` returns the full answer as ``str``;
    ``stream=True`` returns an iterator of text chunks.
    """
    cfg = config if config is not None else load_config()
    description = describe_image(
        image, build_vision_prompt(question), config=cfg
    )
    messages = _compose_messages(question, description)
    payload = {
        "model": cfg.deepseek.model,
        "messages": messages,
        "stream": stream,
    }
    if not stream:
        resp = _request_deepseek(cfg, payload)
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, ValueError, TypeError) as exc:
            raise ComposeError(
                "DeepSeek API 响应解析失败",
                model=cfg.deepseek.model,
            ) from exc
    return _stream_answers(cfg, payload)


def _stream_answers(cfg: Config, payload: dict) -> Iterator[str]:
    """SSE-stream the DeepSeek answer chunk by chunk."""
    url = f"{cfg.deepseek.base_url.rstrip('/')}/chat/completions"
    client = httpx.Client(timeout=120.0)
    try:
        resp = retry_request(
            client,
            "POST",
            url,
            retries=cfg.retries,
            json=payload,
            headers={"Authorization": f"Bearer {cfg.deepseek.api_key}"},
        )
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError as exc:
                raise ComposeError(
                    "DeepSeek 流式响应解析失败",
                    model=cfg.deepseek.model,
                ) from exc
            content = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
            if content:
                yield content
    except httpx.HTTPStatusError as exc:
        raise ComposeError(
            f"DeepSeek API 请求失败: HTTP {exc.response.status_code}",
            model=cfg.deepseek.model,
            status_code=exc.response.status_code,
        ) from exc
    finally:
        client.close()