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

app = FastAPI(title="DeepSee Server", version="0.1.0")


def _current_config():
    return load_config()


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
    """Accept base64 data: URLs (→ bytes) or http(s) URLs (→ URL string)."""
    if url.startswith("data:"):
        m = re.match(r"data:[^;]+;base64,(.*)", url, re.DOTALL)
        if not m:
            raise ValueError("仅支持 base64 data URL 图片")
        return base64.b64decode(m.group(1))
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
    body = await request.json()
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

    if image is not None:
        answer = ask_with_image(image, text or "请描述这张图片", stream=stream, config=cfg)
    else:
        answer = ask(text, stream=stream, config=cfg)

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
    body = await request.json()
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
    answer = ask_with_image(img, question or "请描述这张图片", config=cfg)
    return JSONResponse({"kind": "auto", "text": answer})
