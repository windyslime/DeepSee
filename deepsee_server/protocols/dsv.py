"""DeepSee Vision (DSV) public orchestration protocol."""

from __future__ import annotations

import base64
import copy
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from deepsee.errors import ComposeError, VisionBackendError

from . import openai
from .base import decode_base64_image, extract_image_from_url


_VISION_MODES = frozenset({"auto", "ui", "general"})
_SUPPORTED_DSV_FIELDS = set(openai._SUPPORTED_CHAT_FIELDS) | {"vision"}


@dataclass(frozen=True)
class ParsedDsvRequest:
    messages: list[dict[str, Any]]
    stream: bool
    params: dict[str, Any]
    model: str | None
    vision_mode: str
    include_analysis: bool
    image_count: int


def _data_url(media_type: str, data: str) -> str:
    raw = decode_base64_image(data)
    return f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"


def _normalize_image_block(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    if not isinstance(source, dict):
        raise ValueError("image.source 必须是对象")
    source_type = source.get("type")
    if source_type == "base64":
        media_type = source.get("media_type")
        data = source.get("data")
        if not isinstance(media_type, str) or not media_type.startswith("image/"):
            raise ValueError("image.source.media_type 必须是 image/*")
        if not isinstance(data, str) or not data:
            raise ValueError("image.source.data 必须是非空字符串")
        url = _data_url(media_type, data)
    elif source_type == "url":
        url_value = source.get("url")
        if not isinstance(url_value, str) or not url_value:
            raise ValueError("image.source.url 必须是非空字符串")
        extracted = extract_image_from_url(url_value)
        if isinstance(extracted, bytes):
            media_type = source.get("media_type", "image/png")
            if not isinstance(media_type, str) or not media_type.startswith("image/"):
                raise ValueError("image.source.media_type 必须是 image/*")
            url = f"data:{media_type};base64,{base64.b64encode(extracted).decode('ascii')}"
        else:
            url = extracted
    else:
        raise ValueError("image.source.type 必须是 base64 或 url")
    return {"type": "image_url", "image_url": {"url": url}}


def _normalize_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        raise ValueError("messages 必须是数组")
    normalized = copy.deepcopy(messages)
    for message in normalized:
        if not isinstance(message, dict):
            raise ValueError("messages 项必须是对象")
        content = message.get("content")
        if not isinstance(content, list):
            continue
        blocks = []
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("content 块必须是对象")
            if block.get("type") == "image":
                blocks.append(_normalize_image_block(block))
            else:
                blocks.append(block)
        message["content"] = blocks
    return normalized


def parse_request(body: dict[str, Any]) -> ParsedDsvRequest:
    """Validate a DSV request and normalize its image blocks."""
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    unknown = set(body) - _SUPPORTED_DSV_FIELDS
    if unknown:
        raise ValueError(
            "不支持的请求参数: "
            + ", ".join(sorted(str(value) for value in unknown))
        )

    model = body.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("model 必须是非空字符串")
    if isinstance(model, str):
        model = model.strip()

    vision = body.get("vision", {})
    if vision is None or not isinstance(vision, dict):
        raise ValueError("vision 必须是对象")
    vision_unknown = set(vision) - {"mode", "include_analysis"}
    if vision_unknown:
        raise ValueError(
            "不支持的 vision 参数: "
            + ", ".join(sorted(str(value) for value in vision_unknown))
        )
    mode = vision.get("mode", "auto")
    if mode not in _VISION_MODES:
        raise ValueError("vision.mode 必须是 auto、ui 或 general")
    include_analysis = vision.get("include_analysis", True)
    if not isinstance(include_analysis, bool):
        raise ValueError("vision.include_analysis 必须是布尔值")

    normalized = copy.deepcopy(body)
    normalized["messages"] = _normalize_messages(body.get("messages"))
    normalized.pop("vision", None)
    parsed = openai.parse_chat_request(normalized)
    if parsed.image_count == 0:
        raise ValueError("DSV 请求至少包含一张图片")
    return ParsedDsvRequest(
        messages=parsed.messages,
        stream=parsed.stream,
        params=parsed.params,
        model=model,
        vision_mode=mode,
        include_analysis=include_analysis,
        image_count=parsed.image_count,
    )


def _first_choice(response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("DeepSeek 响应缺少 choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("DeepSeek 响应缺少 message")
    return choice, message


def _vision_payload(vision: dict[str, Any], include_analysis: bool) -> dict[str, Any]:
    payload = copy.deepcopy(vision)
    if not include_analysis:
        payload.pop("analysis", None)
    return payload


def encode_response(
    response: dict[str, Any],
    *,
    vision: dict[str, Any],
    include_analysis: bool = True,
) -> dict[str, Any]:
    """Encode a completed DeepSeek response as an independent DSV envelope."""
    try:
        choice, message = _first_choice(response)
    except ValueError as exc:
        raise ValueError("DeepSeek API 响应解析失败") from exc
    content = message.get("content")
    answer_text = content if isinstance(content, str) else ""
    reasoning = message.get("reasoning_content", message.get("reasoning"))
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        tool_calls = []
    finish_reason = choice.get("finish_reason")
    requires_action = bool(tool_calls) or finish_reason == "tool_calls"
    payload: dict[str, Any] = {
        "id": response.get("id") or f"dsv_{uuid.uuid4().hex[:12]}",
        "object": "dsv.response",
        "status": "requires_action" if requires_action else "completed",
        "vision": _vision_payload(vision, include_analysis),
        "answer": {"text": answer_text},
        "usage": copy.deepcopy(response.get("usage") or {}),
    }
    if isinstance(reasoning, str) and reasoning:
        payload["reasoning"] = {"text": reasoning}
    if tool_calls:
        payload["tool_calls"] = copy.deepcopy(tool_calls)
    return payload


def _frame(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _merge_tool_call(
    calls: dict[int, dict[str, Any]],
    delta: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    index = delta.get("index", len(calls))
    if not isinstance(index, int) or index < 0:
        index = len(calls)
    current = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    if isinstance(delta.get("id"), str):
        current["id"] = delta["id"]
    if isinstance(delta.get("type"), str):
        current["type"] = delta["type"]
    function = delta.get("function")
    if isinstance(function, dict):
        if isinstance(function.get("name"), str):
            current["function"]["name"] = function["name"]
        if isinstance(function.get("arguments"), str):
            current["function"]["arguments"] += function["arguments"]
    return index, copy.deepcopy(current)


async def encode_stream(
    chunks: AsyncIterator[dict[str, Any]],
    *,
    vision: dict[str, Any],
    include_analysis: bool = True,
    on_error: Callable[[str], None] | None = None,
) -> AsyncIterator[bytes]:
    """Encode raw DeepSeek SSE chunks into DSV event frames."""
    response_id = f"dsv_{uuid.uuid4().hex[:12]}"
    upstream_id: str | None = None
    model: str | None = None
    answer_text = ""
    reasoning_text = ""
    reasoning_started = False
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] = {}
    emit_done = False
    try:
        yield _frame({"type": "response.created", "id": response_id})
        yield _frame({"type": "vision.started", "id": response_id})
        yield _frame(
            {
                "type": "vision.completed",
                "id": response_id,
                "vision": _vision_payload(vision, include_analysis),
            }
        )
        async with contextlib.aclosing(chunks):
            async for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                if isinstance(chunk.get("id"), str):
                    upstream_id = upstream_id or chunk["id"]
                model = chunk.get("model") or model
                if isinstance(chunk.get("usage"), dict):
                    usage = copy.deepcopy(chunk["usage"])
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    continue
                choice = choices[0]
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                finish = choice.get("finish_reason")
                if isinstance(finish, str):
                    finish_reason = finish
                reasoning = delta.get("reasoning_content", delta.get("reasoning"))
                if isinstance(reasoning, str) and reasoning:
                    if not reasoning_started:
                        yield _frame({"type": "reasoning.started", "id": response_id})
                        reasoning_started = True
                    reasoning_text += reasoning
                    yield _frame({"type": "reasoning.delta", "id": response_id, "text": reasoning})
                content = delta.get("content")
                if isinstance(content, str) and content:
                    answer_text += content
                    yield _frame({"type": "answer.delta", "id": response_id, "text": content})
                raw_tool_calls = delta.get("tool_calls")
                if isinstance(raw_tool_calls, list):
                    for raw_call in raw_tool_calls:
                        if not isinstance(raw_call, dict):
                            continue
                        index, merged = _merge_tool_call(tool_calls, raw_call)
                        yield _frame(
                            {
                                "type": "tool_call.delta",
                                "id": response_id,
                                "index": index,
                                "delta": copy.deepcopy(raw_call),
                                "tool_call": merged,
                            }
                        )

        if tool_calls or finish_reason == "tool_calls":
            completed_calls = [tool_calls[index] for index in sorted(tool_calls)]
            for index, call in enumerate(completed_calls):
                yield _frame(
                    {
                        "type": "tool_call.completed",
                        "id": response_id,
                        "index": index,
                        "tool_call": copy.deepcopy(call),
                    }
                )
            yield _frame(
                {
                    "type": "response.requires_action",
                    "id": response_id,
                    "tool_calls": copy.deepcopy(completed_calls),
                }
            )
            status = "requires_action"
        else:
            yield _frame(
                {
                    "type": "answer.completed",
                    "id": response_id,
                    "text": answer_text,
                    "reasoning": reasoning_text,
                }
            )
            status = "completed"
        completed: dict[str, Any] = {
            "type": "response.completed",
            "id": response_id,
            "status": status,
            "usage": usage,
        }
        if model:
            completed["model"] = model
        if upstream_id:
            completed["upstream_id"] = upstream_id
        yield _frame(completed)
        emit_done = True
    except (ComposeError, VisionBackendError, ValueError) as exc:
        if on_error is not None:
            on_error("upstream_error")
        yield _frame(
            {
                "type": "error",
                "id": response_id,
                "stage": "reasoning",
                "error": {"message": str(exc), "type": "upstream_error"},
            }
        )
        yield _frame({"type": "response.completed", "id": response_id, "status": "failed"})
        emit_done = True
    finally:
        # A client disconnect closes this async generator with GeneratorExit or
        # CancelledError. Do not yield from finally in that path: async
        # generators must finish closing without attempting to write another
        # response chunk.
        if emit_done:
            yield b"data: [DONE]\n\n"


async def encode_error_stream(
    *,
    stage: str,
    message: str,
    vision: dict[str, Any] | None = None,
    include_analysis: bool = True,
) -> AsyncIterator[bytes]:
    """Encode a request-stage failure as a complete DSV SSE sequence."""
    response_id = f"dsv_{uuid.uuid4().hex[:12]}"
    yield _frame({"type": "response.created", "id": response_id})
    if stage == "vision" or vision is not None:
        yield _frame({"type": "vision.started", "id": response_id})
        if vision is not None:
            yield _frame(
                {
                    "type": "vision.completed",
                    "id": response_id,
                    "vision": _vision_payload(vision, include_analysis),
                }
            )
    yield _frame(
        {
            "type": "error",
            "id": response_id,
            "stage": stage,
            "error": {"message": message, "type": "upstream_error"},
        }
    )
    yield _frame({"type": "response.completed", "id": response_id, "status": "failed"})
    yield b"data: [DONE]\n\n"


def encode_error_response(
    *,
    stage: str,
    message: str,
    vision: dict[str, Any] | None = None,
    include_analysis: bool = True,
) -> dict[str, Any]:
    """Encode a failed non-stream response without dropping completed vision."""
    return {
        "id": f"dsv_{uuid.uuid4().hex[:12]}",
        "object": "dsv.response",
        "status": "failed",
        "vision": _vision_payload(vision or {}, include_analysis),
        "answer": {"text": ""},
        "error": {
            "stage": stage,
            "message": message,
            "type": "upstream_error",
        },
        "usage": {},
    }
