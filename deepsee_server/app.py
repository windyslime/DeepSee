"""DeepSee server: OpenAI-compatible local endpoint with vision ability.

Exposes:
- GET  /v1/models             — list configured models (never hard-coded)
- POST /v1/chat/completions   — OpenAI-compatible chat (text & images, streaming)
- POST /analyze               — internal vision analysis (for the future GUI)

Any local app can point its OpenAI-compatible client at
``http://127.0.0.1:8712/v1`` to get "DeepSeek that can see".
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from deepsee import ask_async, ask_with_image_async, describe_image_async, load_config
from deepsee.errors import ComposeError, ImageError, VisionBackendError
from deepsee_server.protocols import anthropic, gemini
from deepsee_server.protocols.base import extract_image_from_url
from deepsee_server.protocols import openai as openai_protocol

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
        text, image = openai_protocol.parse_request(body)
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
            result = await ask_with_image_async(
                image, text or "请描述这张图片", stream=stream, config=cfg,
                include_vision=True,
            )
            answer, vision = result.text, result.vision
        else:
            answer = await ask_async(text, stream=stream, config=cfg)
            vision = None
    except ImageError as exc:
        # 图片加载/解码失败(SSRF 拒绝、字节/像素超限、格式不支持等)映射为 4xx
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )
    except (ComposeError, VisionBackendError) as exc:
        # 上游(DeepSeek / 视觉后端)失败:映射为 502,保持 OpenAI 兼容 error 体
        return JSONResponse(
            {"error": {"message": str(exc), "type": "upstream_error"}},
            status_code=502,
        )

    if not stream:
        return JSONResponse(openai_protocol.encode_text(answer, vision, model_id))

    return StreamingResponse(
        openai_protocol.encode_stream(answer, vision, model_id),
        media_type="text/event-stream",
    )


@app.post("/analyze")
async def analyze(request: Request):
    """Internal vision-only analysis (for the future GUI / Codewhale flow).

    纯视觉分析,只调视觉后端、不经过 DeepSeek 推理(设计文档 §5:
    ``describe_image_async``);返回原始视觉文本。
    """
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
        img = extract_image_from_url(image)
    except ValueError as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )
    cfg = _current_config()
    try:
        answer = await describe_image_async(
            img, question or "请描述这张图片", config=cfg
        )
    except ImageError as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )
    except (ComposeError, VisionBackendError) as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "upstream_error"}},
            status_code=502,
        )
    return JSONResponse({"kind": "description", "text": answer})


def _anthropic_error(status: int, message: str):
    """Anthropic 形状错误体(请求级错误,如 413 / 非法 JSON)。"""
    return JSONResponse(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        },
        status_code=status,
    )


def _gemini_error(status: int, message: str):
    """Gemini 形状错误体(请求级错误,如 413 / 非法 JSON)。"""
    return JSONResponse(
        {"error": {"code": status, "message": message}},
        status_code=status,
    )


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic messages 形状端点(内部视觉分析可展开,GUI 使用)。"""
    if _body_too_large(request):
        return _anthropic_error(413, "请求体过大")
    body_bytes = await _read_body_limited(request)
    if body_bytes is None:
        return _anthropic_error(413, "请求体过大")
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _anthropic_error(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _anthropic_error(400, "请求体必须是 JSON 对象")

    stream = bool(body.get("stream", False))
    cfg = _current_config()
    model_id = cfg.deepseek.model

    try:
        text, image = anthropic.parse_request(body)
    except ValueError as exc:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}},
            status_code=400,
        )

    if not text and image is None:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "请求中没有可用的文本或图片内容"}},
            status_code=400,
        )

    try:
        if image is not None:
            result = await ask_with_image_async(
                image, text or "请描述这张图片", stream=stream, config=cfg,
                include_vision=True,
            )
            answer, vision = result.text, result.vision
        else:
            answer = await ask_async(text, stream=stream, config=cfg)
            vision = None
    except ImageError as exc:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}},
            status_code=400,
        )
    except (ComposeError, VisionBackendError) as exc:
        return JSONResponse(
            {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}},
            status_code=502,
        )

    if not stream:
        return JSONResponse(anthropic.encode_text(answer, vision, model_id))
    return StreamingResponse(
        anthropic.encode_stream(answer, vision, model_id),
        media_type="text/event-stream",
    )


@app.post("/v1beta/models/{model}:generateContent")
async def gemini_generate_content(request: Request, model: str):
    """Gemini generateContent 形状端点(内部视觉分析可展开,GUI 使用)。"""
    if _body_too_large(request):
        return _gemini_error(413, "请求体过大")
    body_bytes = await _read_body_limited(request)
    if body_bytes is None:
        return _gemini_error(413, "请求体过大")
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _gemini_error(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _gemini_error(400, "请求体必须是 JSON 对象")

    stream = bool(body.get("stream", False))
    cfg = _current_config()
    model_id = cfg.deepseek.model

    try:
        text, image = gemini.parse_request(body)
    except ValueError as exc:
        return JSONResponse(
            {"error": {"code": 400, "message": str(exc)}},
            status_code=400,
        )

    if not text and image is None:
        return JSONResponse(
            {"error": {"code": 400, "message": "请求中没有可用的文本或图片内容"}},
            status_code=400,
        )

    try:
        if image is not None:
            result = await ask_with_image_async(
                image, text or "请描述这张图片", stream=stream, config=cfg,
                include_vision=True,
            )
            answer, vision = result.text, result.vision
        else:
            answer = await ask_async(text, stream=stream, config=cfg)
            vision = None
    except ImageError as exc:
        return JSONResponse(
            {"error": {"code": 400, "message": str(exc)}},
            status_code=400,
        )
    except (ComposeError, VisionBackendError) as exc:
        return JSONResponse(
            {"error": {"code": 502, "message": str(exc)}},
            status_code=502,
        )

    if not stream:
        return JSONResponse(gemini.encode_text(answer, vision, model))
    return StreamingResponse(
        gemini.encode_stream(answer, vision, model),
        media_type="text/event-stream",
    )
