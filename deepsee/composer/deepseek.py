"""DeepSeek composition layer.

The vision backend produces an analysis of the image (a natural-language
description, or a structured UI element map for front-end screenshots).
That analysis is injected into the DeepSeek conversation as context, so
DeepSeek can answer questions about the image and act on UI change
instructions precisely.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Union

import httpx

from deepsee.backends import create_backend
from deepsee.backends.base import retry_request
from deepsee.config.loader import Config, load_config
from deepsee.errors import ComposeError
from deepsee.pipeline.image import ImageInput
from deepsee.pipeline.prompts import (
    build_auto_route_prompt,
    build_ui_analysis_prompt,
    build_vision_prompt,
)
from deepsee.pipeline.ui import parse_structured

_SYSTEM_TEMPLATE = (
    "你是 DeepSee 多模态助手。用户提供了一张图片,以下是视觉模型对图片的分析:\n\n"
    "{context}\n\n"
    "请基于以上分析回答用户的问题。若分析明确表明用户要求的元素不在图片中"
    "(target_found 为 false 或有重截图建议),请直接告知用户重新截图,不要猜测修改。"
)


def describe_image(
    image: ImageInput,
    prompt: str,
    *,
    config: Config | None = None,
) -> str:
    """Run the vision backend directly: image + prompt → raw text."""
    cfg = config if config is not None else load_config()
    backend = create_backend(cfg.vision, cfg.retries)
    try:
        return backend.describe(image, prompt)
    finally:
        backend.close()


def _compose_messages(question: str, context: str) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM_TEMPLATE.format(context=context)},
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
    mode: str = "auto",
) -> Union[str, Iterator[str]]:
    """Full composition: vision analysis → DeepSeek reasoning.

    ``mode``:
    - ``"auto"`` (default): single vision call; the model classifies the
      image and emits either a UI element map (front-end screenshot) or a
      natural-language description.
    - ``"ui"``: force structured UI analysis (skip classification).
    - ``"general"``: force general description (skip classification).

    ``stream=False`` returns the full answer as ``str``;
    ``stream=True`` returns an iterator of text chunks.
    """
    cfg = config if config is not None else load_config()
    vision_result = _analyze_image(image, question, mode, cfg)
    context = _format_context(vision_result)
    messages = _compose_messages(question, context)
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


def _analyze_image(
    image: ImageInput,
    question: str,
    mode: str,
    cfg: Config,
) -> dict[str, Any]:
    """Single vision call: classify and analyze in one request.

    Returns one of:
    - ``{"kind": "ui", "data": {...}}`` — structured element map
    - ``{"kind": "description", "text": str}`` — natural-language description
    - ``{"kind": "raw", "text": str}`` — unparseable output (fallback)
    """
    backend = create_backend(cfg.vision, cfg.retries)
    try:
        if mode == "general":
            raw = backend.describe(image, build_vision_prompt(question))
            return {"kind": "description", "text": raw}
        if mode == "ui":
            raw = backend.describe(image, build_ui_analysis_prompt(question))
        else:  # auto
            raw = backend.describe(image, build_auto_route_prompt(question))

        parsed = parse_structured(raw)
        if parsed is None:
            return {"kind": "raw", "text": raw}
        if mode == "ui":
            return {"kind": "ui", "data": parsed}
        if parsed.get("is_ui") is True:
            data = parsed.get("analysis")
            return {"kind": "ui", "data": data if isinstance(data, dict) else {}}
        description = parsed.get("analysis")
        if isinstance(description, str) and description:
            return {"kind": "description", "text": description}
        return {"kind": "description", "text": raw}
    finally:
        backend.close()


def _format_context(vision_result: dict[str, Any]) -> str:
    kind = vision_result["kind"]
    if kind == "ui":
        return _format_ui_map(vision_result["data"])
    if kind == "description":
        return vision_result["text"]
    return (
        "视觉模型输出未能结构化解析,以下为原始输出,请自行判断:\n"
        + vision_result["text"]
    )


def _format_ui_map(data: dict[str, Any]) -> str:
    """Render the structured UI analysis as a readable element map."""
    lines = ["以下是对用户截图的 UI 结构化分析(元素地图):"]
    lines.append(f"界面类型: {data.get('ui_type', 'unknown')}")
    layout = data.get("layout")
    if layout:
        lines.append(f"布局: {layout}")
    for el in data.get("elements") or []:
        if not isinstance(el, dict):
            continue
        lines.append(
            "- 元素 #{} [{}] {} | 位置: {} | 尺寸: {} | 样式: {} | 状态: {}".format(
                el.get("id", "?"),
                el.get("type", "?"),
                el.get("text", ""),
                el.get("location", ""),
                el.get("size", ""),
                el.get("style", ""),
                el.get("state", ""),
            )
        )
    if data.get("target_found") is False:
        advice = data.get("rescreenshot_advice") or "用户要求的元素未在截图中找到"
        lines.append(f"注意: 用户要求的元素未在截图中找到。{advice}")
    answer = data.get("answer_to_user")
    if answer:
        lines.append(f"针对用户问题的定位: {answer}")
    return "\n".join(lines)


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
