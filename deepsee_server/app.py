"""DeepSee server: OpenAI-compatible local endpoint with vision ability.

Exposes:
- GET  /v1/models             — list configured models (never hard-coded)
- POST /v1/chat/completions   — OpenAI-compatible chat (text & images, streaming)
- POST /analyze               — internal vision analysis (for the future GUI)

Any local app can point its OpenAI-compatible client at
``http://127.0.0.1:8712/v1`` to get "DeepSeek that can see".
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from deepsee import ask, ask_with_image, load_config
from deepsee.errors import ImageError
from deepsee.pipeline.image import MAX_IMAGE_BYTES

app = FastAPI(title="DeepSee Server", version="0.1.0")

# 请求体上限:含 base64 data URL 的图片请求会膨胀约 4/3,留出 JSON 与文本余量。
_MAX_REQUEST_BODY = 32 * 1024 * 1024


def _current_config():
    return load_config()


def _body_too_large(request: Request) -> bool:
    """Content-Length 预检(快速路径),防止超大请求体在读入内存前被处理。"""
    length = request.headers.get("content-length")
    if not length:
        return False  # chunked 请求无 Content-Length,由 _read_body_limited 兜底
    try:
        return int(length) > _MAX_REQUEST_BODY
    except ValueError:
        return False


async def _read_body_limited(request: Request) -> bytes | None:
    """流式读取请求体并强制字节上限;超限返回 None。

    ``await request.json()`` 会把任意大小的请求体完整缓冲到内存,chunked
    请求(无 Content-Length)会绕过 ``_body_too_large`` 预检;这里在读取
    阶段逐块累计,超限即中止。
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_REQUEST_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@app.get("/v1/models")
def list_models():
    cfg = _current_config()
    model_id = cfg.deepseek.model
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "deepsee",
            }
        ],
    }


def _extract_image_from_url(url: str) -> bytes | str:
    """Accept base64 data: URLs (→ bytes) or http(s) URLs (→ URL string).

    http(s) URL 的下载防护(SSRF / 字节上限)在 ``load_image`` 层;data URL
    在此解码并做字节上限检查;其他形式(含 ``file://`` 本地路径)一律拒绝。
    """
    if not isinstance(url, str):
        # JSON 里 url 字段可能是数字/布尔等,直接调用 str 方法会 500
        raise ValueError(f"不支持的图片 URL 形式: {url!r}")
    if url.startswith("data:"):
        m = re.match(r"data:[^;]+;base64,(.*)", url, re.DOTALL)
        if not m:
            raise ValueError("仅支持 base64 data URL 图片")
        raw = base64.b64decode(m.group(1))
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"图片数据过大(超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB)")
        return raw
    if url.startswith("http://") or url.startswith("https://"):
        return url
    raise ValueError(f"不支持的图片 URL 形式: {url[:60]}")


def _parse_messages(messages: list[dict]) -> tuple[str, bytes | str | None]:
    """Extract the last user text and an optional image from OpenAI messages."""
    text = ""
    image = None
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                elif btype == "image_url":
                    url = block["image_url"].get("url", "")
                    if url and image is None:
                        image = _extract_image_from_url(url)
    return text, image


def _completion_payload(content: str, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if _body_too_large(request):
        return JSONResponse(
            {"error": {"message": "请求体过大", "type": "invalid_request_error"}},
            status_code=413,
        )
    body_bytes = await _read_body_limited(request)
    if body_bytes is None:
        return JSONResponse(
            {"error": {"message": "请求体过大", "type": "invalid_request_error"}},
            status_code=413,
        )
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"error": {"message": "请求体不是合法 JSON", "type": "invalid_request_error"}},
            status_code=400,
        )
    if not isinstance(body, dict):
        # [] / "hello" / null 等合法 JSON 但没有 .get,直接调用会 500
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}},
            status_code=400,
        )
    stream = bool(body.get("stream", False))
    messages = body.get("messages", [])
    # 请求里的 model 字段接受任意值:不写死、不强制匹配,按配置执行
    cfg = _current_config()
    model_id = cfg.deepseek.model

    try:
        text, image = _parse_messages(messages)
    except ValueError as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )

    if not text and image is None:
        return JSONResponse(
            {
                "error": {
                    "message": "请求中没有可用的文本或图片内容",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
        )

    try:
        if image is not None:
            answer = ask_with_image(image, text or "请描述这张图片", stream=stream, config=cfg)
        else:
            answer = ask(text, stream=stream, config=cfg)
    except ImageError as exc:
        # 图片加载/解码失败(SSRF 拒绝、字节/像素超限、格式不支持等)映射为 4xx
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )

    if not stream:
        return JSONResponse(_completion_payload(answer, model_id))

    def gen():
        for chunk in answer:
            payload = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_id,
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/analyze")
async def analyze(request: Request):
    """Internal vision-only analysis (for the future GUI / Codewhale flow)."""
    if _body_too_large(request):
        return JSONResponse(
            {"error": {"message": "请求体过大", "type": "invalid_request_error"}},
            status_code=413,
        )
    body_bytes = await _read_body_limited(request)
    if body_bytes is None:
        return JSONResponse(
            {"error": {"message": "请求体过大", "type": "invalid_request_error"}},
            status_code=413,
        )
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"error": {"message": "请求体不是合法 JSON", "type": "invalid_request_error"}},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}},
            status_code=400,
        )
    image = body.get("image")
    question = body.get("question", "")
    if not image:
        return JSONResponse(
            {"error": {"message": "缺少 image 字段", "type": "invalid_request_error"}},
            status_code=400,
        )
    try:
        img = _extract_image_from_url(image)
    except ValueError as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )
    cfg = _current_config()
    try:
        answer = ask_with_image(img, question or "请描述这张图片", config=cfg)
    except ImageError as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )
    return JSONResponse({"kind": "auto", "text": answer})
