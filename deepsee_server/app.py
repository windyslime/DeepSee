"""DeepSee server: OpenAI-compatible local endpoint with vision ability.

Exposes:
- GET  /v1/models             — list configured models (never hard-coded)
- POST /v1/chat/completions   — OpenAI-compatible chat (text & images, streaming)
- POST /v1/dsv                — DeepSee Vision orchestration/output protocol (SSE)
- POST /analyze               — internal vision analysis (for the future GUI)

Any local app can point its OpenAI-compatible client at
``http://127.0.0.1:8712/v1`` to get "DeepSeek that can see".
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask, BackgroundTasks

from deepsee import ask_async, ask_with_image_async, describe_image_async
from deepsee.composer.chat import chat_async
from deepsee.composer.vision_context import (
    VisionContextError,
    transform_messages_with_vision,
)
from deepsee.errors import ConfigError, ComposeError, ImageError, VisionBackendError
from deepsee_server.auth import (
    ApiKeyStoreError,
    admin_token,
    api_key_auth_mode,
    configured_api_key_store,
    public_token,
    token_digest,
)
from deepsee_server.connection_verify import verify_upstream_connections
from deepsee_server.protocols import anthropic, dsv, gemini
from deepsee_server.protocols.base import extract_image_from_url
from deepsee_server.protocols import openai as openai_protocol
from deepsee_server.request_guard import (
    GuardLease,
    QueueTimeout,
    RateLimitExceeded,
    RequestGuard,
)
from deepsee_server.request_limits import RequestLimits
from deepsee_server.runtime_control import configured_restart_controller
from deepsee_server.traces import RequestTrace, request_traces
from deepsee_server.upstream_config import (
    ManagedProviderConfig,
    ManagedUpstreamConfig,
    UpstreamConfigStore,
    default_upstream_config_path,
    load_effective_config,
    redacted_config_view,
    validate_provider_base_url,
)
from deepsee.config.loader import VISION_MODES

app = FastAPI(title="DeepSee Server", version="0.1.0")

_cors_origins = [
    origin.strip()
    for origin in os.environ.get(
        "DeepSee_CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if origin.strip()
]

# 请求体上限:含 base64 data URL 的图片请求会膨胀约 4/3,留出 JSON 与文本余量。
_MAX_REQUEST_BODY = 32 * 1024 * 1024
_VISION_MODES = frozenset({"auto", "ui", "general"})
_INSTANCE_ID = uuid.uuid4().hex
_request_guard: RequestGuard | None = None
_upstream_store = UpstreamConfigStore(default_upstream_config_path())
_REQUEST_LIMITS = RequestLimits.from_env()
_logger = logging.getLogger(__name__)


def _key_store_unavailable(exc: ApiKeyStoreError) -> JSONResponse:
    """key-store 存储错误统一映射为 503 ``configuration_error``(fail closed)。

    日志记录文件位置,绝不记录任何 key 内容;客户端响应不暴露本地路径。
    """
    _logger.warning("API key store 不可读: %s", exc.path)
    return JSONResponse(
        {
            "error": {
                "message": "API key 存储不可用",
                "type": "configuration_error",
            }
        },
        status_code=503,
    )


def configure_request_guard(guard: RequestGuard | None) -> None:
    global _request_guard
    _request_guard = guard


def configure_upstream_store(store: UpstreamConfigStore) -> None:
    global _upstream_store
    _upstream_store = store


def _required_scope(path: str) -> str | None:
    if path in {
        "/v1/models",
        "/v1/chat/completions",
        "/v1/dsv",
        "/v1/messages",
        "/analyze",
    } or (path.startswith("/v1beta/models/") and path.endswith(":generateContent")):
        return "public"
    if path.startswith("/admin/"):
        return "admin"
    return None


@app.middleware("http")
async def _local_auth(request: Request, call_next):
    """Enforce admin keys when configured, while retaining direct-import dev use."""
    path = request.url.path
    if request.method == "OPTIONS" or path == "/health":
        return await call_next(request)

    store = configured_api_key_store()
    mode = api_key_auth_mode()
    scope = _required_scope(path)
    identity = request.client.host if request.client else "unknown"
    if scope is not None and mode == "unconfigured":
        return JSONResponse(
            {
                "error": {
                    "message": "API authentication is not configured",
                    "type": "configuration_error",
                }
            },
            status_code=503,
        )
    if mode == "disabled":
        scope = None
    if scope == "admin":
        if mode == "enabled" and store is not None:
            token = admin_token(request)
            try:
                valid = store.validate(token, "admin")
            except ApiKeyStoreError as exc:
                # 存储损坏:fail closed,而不是让受保护路由裸 500
                return _key_store_unavailable(exc)
            if not valid:
                return JSONResponse(
                    {"error": {"message": "invalid admin API key", "type": "authentication_error"}},
                    status_code=401,
                )
            identity = token_digest(token)
        else:
            return JSONResponse(
                {"error": {"message": "admin API key is required", "type": "authentication_error"}},
                status_code=401,
            )
    elif scope == "public":
        if mode == "enabled":
            token = public_token(request)
            if store is None:
                return JSONResponse(
                    {"error": {"message": "invalid API key", "type": "authentication_error"}},
                    status_code=401,
                )
            try:
                valid = store.validate(token, "public")
            except ApiKeyStoreError as exc:
                # 存储损坏:fail closed,而不是让受保护路由裸 500
                return _key_store_unavailable(exc)
            if not valid:
                return JSONResponse(
                    {"error": {"message": "invalid API key", "type": "authentication_error"}},
                    status_code=401,
                )
            identity = token_digest(token)
    request.state.deepsee_identity = identity

    # 限流与并发租约不在中间件获取:普通 4xx(非法 JSON、协议校验失败等)
    # 不应消耗速率预算或抢占并发槽,由推理端点在请求校验通过后、首次调用
    # 上游之前自行获取(见 _acquire_inference_lease)。
    return await call_next(request)


async def _acquire_inference_lease(
    request: Request,
) -> tuple[GuardLease | None, JSONResponse | None]:
    """在请求校验通过后、首次调用视觉或 DeepSeek 上游之前获取租约。

    返回 ``(lease, None)`` 或 ``(None, error_response)``;错误响应为
    429(rate limit)或 503(并发队列超时),与旧中间件行为一致。
    """
    if _request_guard is None:
        return None, None
    identity = getattr(request.state, "deepsee_identity", "unknown")
    try:
        return await _request_guard.acquire(identity), None
    except RateLimitExceeded as exc:
        return None, JSONResponse(
            {"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}},
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
        )
    except QueueTimeout:
        return None, JSONResponse(
            {"error": {"message": "server is overloaded", "type": "overloaded_error"}},
            status_code=503,
            headers={"Retry-After": "1"},
        )


async def _attach_lease(response: Any, lease: GuardLease | None) -> Any:
    """把租约的释放绑定到响应生命周期。

    非流式响应(无 ``body_iterator``)在返回前立即释放;流式响应在 body
    迭代完成/异常/取消时释放,避免客户端断开后泄漏并发槽。
    """
    if lease is None:
        return response
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        await lease.release()
        return response

    async def guarded_body():
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            await lease.release()

    response.body_iterator = guarded_body()
    return response


@app.middleware("http")
async def _request_trace(request: Request, call_next):
    trace_id = uuid.uuid4().hex
    started = time.monotonic()
    context: dict[str, Any] = {}
    request.state.deepsee_trace = context
    request.state.deepsee_trace_id = trace_id
    try:
        response = await call_next(request)
    except Exception:
        request_traces.append(
            RequestTrace(
                id=trace_id,
                method=request.method,
                path=request.url.path,
                status=500,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                error_type=context.get("error_type", "internal_error"),
            )
        )
        raise

    async def finish_trace() -> None:
        error_type = context.get("error_type")
        if response.status_code >= 400 and not error_type:
            error_type = "http_error"
        request_traces.append(
            RequestTrace(
                id=trace_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                route=context.get("route") or request.url.path,
                has_image=bool(context.get("has_image", False)),
                image_count=int(context.get("image_count", 0)),
                cache_hits=int(context.get("cache_hits", 0)),
                upstream_model=context.get("upstream_model"),
                error_type=error_type,
                vision_analysis=context.get("vision_analysis"),
            )
        )

    response.headers["X-DeepSee-Trace-Id"] = trace_id
    existing_background = response.background
    if existing_background is None:
        response.background = BackgroundTask(finish_trace)
    else:
        response.background = BackgroundTasks(
            [existing_background, BackgroundTask(finish_trace)]
        )
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-DeepSee-Admin-Key",
        "X-DeepSee-Include-Vision",
        "X-DeepSee-Vision-Mode",
    ],
)


@app.get("/health")
def health():
    return {"status": "ok", "instanceId": _INSTANCE_ID}


@app.get("/admin/traces")
def list_request_traces():
    return {"data": request_traces.list()}


@app.get("/admin/config")
def get_upstream_config():
    try:
        view = redacted_config_view(_upstream_store)
    except (ConfigError, OSError, ValueError):
        _logger.exception("上游配置读取失败")
        return _config_read_error()
    return {
        **view,
        "restartSupported": configured_restart_controller().supported,
    }


def _config_request_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error"}},
        status_code=status_code,
    )


def _config_write_error() -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": "上游配置写入失败",
                "type": "configuration_write_error",
            }
        },
        status_code=500,
    )


def _config_read_error() -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": "上游配置读取失败",
                "type": "configuration_read_error",
            }
        },
        status_code=500,
    )


def _configuration_conflict(field: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": f"{field} 由进程环境变量提供，浏览器不能修改",
                "type": "configuration_conflict",
            }
        },
        status_code=409,
    )


def _environment_supplies(name: str) -> bool:
    return name in os.environ or f"DeepSee_{name}" in os.environ


def _provider_candidate(
    raw: object,
    *,
    name: str,
    current: ManagedProviderConfig | None,
    can_keep: bool,
    needs_backend: bool,
) -> tuple[ManagedProviderConfig, str | None]:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} 必须是对象")
    required = {"baseUrl", "key"}
    allowed = {"baseUrl", "model", "key", "models"}
    if needs_backend:
        required.add("backend")
        allowed.add("backend")
    if not required.issubset(raw) or not set(raw).issubset(allowed):
        raise ValueError(f"{name} 包含未知或缺失字段")
    base_url = raw.get("baseUrl")
    model = raw.get("model")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError(f"{name}.baseUrl 必须是非空字符串")
    validate_provider_base_url(base_url, f"{name}.baseUrl")
    if model is not None and (not isinstance(model, str) or not model):
        raise ValueError(f"{name}.model 必须是非空字符串")
    raw_models = raw.get("models", {})
    if "models" not in raw and current is not None:
        raw_models = dict(current.models)
    if not isinstance(raw_models, dict):
        raise ValueError(f"{name}.models 必须是对象")
    unknown_modes = set(raw_models) - set(VISION_MODES)
    if unknown_modes:
        raise ValueError(
            f"{name}.models 包含未知模式: "
            + ", ".join(sorted(str(item) for item in unknown_modes))
        )
    models: dict[str, str] = {}
    for mode, mode_model in raw_models.items():
        if not isinstance(mode_model, str) or not mode_model.strip():
            raise ValueError(f"{name}.models.{mode} 必须是非空字符串")
        models[mode] = mode_model.strip()
    if not model and current is not None:
        model = current.model
    if not model:
        model = models.get("auto") or models.get("general") or models.get("ui")
    if not model:
        raise ValueError(f"{name}.model 或 name.models 必须提供模型")
    backend: str | None = None
    if needs_backend:
        backend = raw.get("backend")
        if backend != "openai_compatible":
            raise ValueError("vision.backend 必须是 openai_compatible")
    mutation = raw.get("key")
    if not isinstance(mutation, dict):
        raise ValueError(f"{name}.key 必须是对象")
    action = mutation.get("action")
    mutation_fields = {"action", "value"} if action == "replace" else {"action"}
    if set(mutation) != mutation_fields:
        raise ValueError(f"{name}.key 包含未知或缺失字段")
    if action == "keep":
        if not can_keep:
            raise ValueError(f"{name}.key 未配置，不能保持原值")
        api_key = current.api_key if current is not None else None
        api_key_inherited = (
            current.api_key_inherited if current is not None else True
        )
    elif action == "remove":
        api_key = None
        api_key_inherited = False
    elif action == "replace":
        value = mutation.get("value")
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name}.key.value 必须是非空字符串")
        api_key = value
        api_key_inherited = False
    else:
        raise ValueError(f"{name}.key.action 非法")
    return ManagedProviderConfig(
        api_key,
        base_url,
        model,
        api_key_inherited=api_key_inherited,
        models=models,
    ), backend


@app.post("/admin/config")
async def save_upstream_config(request: Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _config_request_error("请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _config_request_error("请求体必须是 JSON 对象")
    if set(body) != {"deepseek", "vision"}:
        return _config_request_error("请求体包含未知或缺失字段")
    try:
        current = _upstream_store.load()
        current_view = redacted_config_view(_upstream_store)
    except (ConfigError, OSError, ValueError):
        _logger.exception("上游配置读取失败")
        return _config_read_error()
    for provider_name, environment_name in (
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("vision", "VISION_API_KEY"),
    ):
        provider = body.get(provider_name)
        mutation = provider.get("key") if isinstance(provider, dict) else None
        action = mutation.get("action") if isinstance(mutation, dict) else None
        if action in {"replace", "remove"} and _environment_supplies(environment_name):
            return _configuration_conflict(f"{provider_name}.key")
    field_overrides = (
        ("deepseek", "baseUrl", "DEEPSEEK_BASE_URL"),
        ("deepseek", "model", "DEEPSEEK_MODEL"),
        ("vision", "backend", "VISION_BACKEND"),
        ("vision", "baseUrl", "VISION_BASE_URL"),
        ("vision", "model", "VISION_MODEL"),
    )
    for provider_name, field, environment_name in field_overrides:
        provider = body.get(provider_name)
        requested_value = provider.get(field) if isinstance(provider, dict) else None
        effective_value = current_view[provider_name][field]
        if _environment_supplies(environment_name) and requested_value != effective_value:
            return _configuration_conflict(f"{provider_name}.{field}")
    vision_body = body.get("vision")
    requested_vision_models = (
        vision_body.get("models") if isinstance(vision_body, dict) else None
    )
    if requested_vision_models is not None and isinstance(requested_vision_models, dict):
        for mode, requested_value in requested_vision_models.items():
            environment_name = f"VISION_MODEL_{mode.upper()}"
            effective_value = current_view["vision"]["models"].get(mode)
            if _environment_supplies(environment_name) and requested_value != effective_value:
                return _configuration_conflict(f"vision.models.{mode}")
    try:
        deepseek, _ = _provider_candidate(
            body.get("deepseek"),
            name="deepseek",
            current=current.deepseek if current is not None else None,
            can_keep=current_view["deepseek"]["keyConfigured"],
            needs_backend=False,
        )
        vision, backend = _provider_candidate(
            body.get("vision"),
            name="vision",
            current=current.vision if current is not None else None,
            can_keep=current_view["vision"]["keyConfigured"],
            needs_backend=True,
        )
        candidate = ManagedUpstreamConfig(
            deepseek=deepseek,
            vision=vision,
            vision_backend=backend or "openai_compatible",
        )
    except ValueError as exc:
        return _config_request_error(str(exc))
    try:
        _upstream_store.save(candidate)
    except OSError:
        _logger.exception("上游配置写入失败")
        return _config_write_error()
    controller = configured_restart_controller()
    response = JSONResponse(
        {
            "saved": True,
            "restartRequired": True,
            "restartSupported": controller.supported,
        }
    )
    if controller.supported:
        response.background = BackgroundTask(controller.request_restart)
    return response


@app.post("/admin/config/verify")
async def verify_upstream_config():
    config = _load_config_or_none()
    if config is None:
        return _openai_config_error()
    return await verify_upstream_connections(config)


@app.get("/admin/keys")
def list_api_keys():
    store = configured_api_key_store()
    if store is None:
        return {"data": []}
    try:
        records = store.list()
    except ApiKeyStoreError as exc:
        return _key_store_unavailable(exc)
    return {"data": records}


@app.post("/admin/keys")
async def create_api_key(request: Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}},
            status_code=400,
        )
    scope = body.get("scope")
    label = body.get("label")
    if scope not in ("public", "admin") or not isinstance(label, str) or not label.strip():
        return JSONResponse(
            {"error": {"message": "scope 或 label 非法", "type": "invalid_request_error"}},
            status_code=400,
        )
    store = configured_api_key_store()
    if store is None:
        return _openai_config_error()
    try:
        created = store.create(scope, label.strip())
    except ApiKeyStoreError as exc:
        return _key_store_unavailable(exc)
    return {
        "id": created.id,
        "key": created.key,
        "scope": created.scope,
        "label": created.label,
        "created_at": created.created_at,
    }


@app.delete("/admin/keys/{key_id}")
def revoke_api_key(key_id: str):
    store = configured_api_key_store()
    if store is None:
        return _openai_config_error()
    try:
        revoked = store.revoke(key_id)
    except ApiKeyStoreError as exc:
        return _key_store_unavailable(exc)
    if not revoked:
        return JSONResponse(
            {"error": {"message": "API key not found", "type": "not_found_error"}},
            status_code=404,
        )
    return {"revoked": True}


def mount_web_dist(path: str | os.PathLike[str]) -> Path:
    """Serve a built Vite site after all API routes have been registered."""
    dist = Path(path).expanduser().resolve()
    if not (dist / "index.html").is_file():
        raise ValueError(f"web dist 缺少 index.html: {dist}")
    if any(getattr(route, "name", None) == "deepsee-web" for route in app.routes):
        raise RuntimeError("web dist 已挂载")
    app.mount("/", StaticFiles(directory=dist, html=True), name="deepsee-web")
    return dist


def _set_trace(request: Request, **values: Any) -> None:
    context = getattr(request.state, "deepsee_trace", None)
    if isinstance(context, dict):
        context.update(values)


def _vision_debug_metadata(analysis: str | None) -> str | None:
    if not analysis:
        return None
    digest = hashlib.sha256(analysis.encode("utf-8")).hexdigest()[:16]
    return f"chars={len(analysis)} sha256={digest}"


def _current_config():
    return load_effective_config(_upstream_store)


def _parse_stream(body: dict) -> bool:
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("stream 必须是布尔值")
    return stream


def _header_vision_mode(request: Request) -> str:
    mode = request.headers.get("X-DeepSee-Vision-Mode", "auto")
    if mode not in _VISION_MODES:
        raise ValueError("X-DeepSee-Vision-Mode 必须是 auto、ui 或 general")
    return mode


def _load_config_or_none():
    try:
        return _current_config()
    except (ConfigError, OSError, ValueError):
        return None


def _openai_config_error():
    return JSONResponse(
        {
            "error": {
                "message": "服务配置不可用",
                "type": "configuration_error",
            }
        },
        status_code=503,
    )


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
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_REQUEST_BODY:
            return None
        body.extend(chunk)
    return bytes(body)


@app.get("/v1/models")
def list_models():
    cfg = _load_config_or_none()
    if cfg is None:
        return _openai_config_error()
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
    vision_mode = request.headers.get("X-DeepSee-Vision-Mode", "auto")
    if vision_mode not in _VISION_MODES:
        return JSONResponse(
            {
                "error": {
                    "message": "X-DeepSee-Vision-Mode 必须是 auto、ui 或 general",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
        )
    try:
        parsed = openai_protocol.parse_chat_request(body)
        max_tokens = _REQUEST_LIMITS.validate_openai(body)
    except ValueError as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )

    stream = parsed.stream
    messages = parsed.messages
    params = parsed.params
    image_count = parsed.image_count
    if "max_tokens" not in params and "max_completion_tokens" not in params:
        params["max_tokens"] = max_tokens

    # 请求里的 model 字段接受任意值:不写死、不强制匹配,按配置执行
    cfg = _load_config_or_none()
    if cfg is None:
        _set_trace(request, error_type="configuration_error")
        return _openai_config_error()
    model_id = cfg.deepseek.model
    _set_trace(
        request,
        route=f"vision-{vision_mode}" if image_count > 0 else "text",
        has_image=image_count > 0,
        image_count=image_count,
        upstream_model=model_id,
    )

    # 请求校验已完成(400 不会消耗额度/并发槽);配置也有效。现在获取
    # 限流与并发租约,再开始任何视觉/推理上游调用。
    lease, guard_error = await _acquire_inference_lease(request)
    if guard_error is not None:
        return guard_error

    try:
        vision = None
        cache_hits = 0
        if image_count:
            transformed = await transform_messages_with_vision(
                messages, config=cfg, mode=vision_mode
            )
            messages = transformed.messages
            vision = "\n\n".join(transformed.analyses)
            cache_hits = transformed.cache_hits
            _set_trace(
                request,
                cache_hits=cache_hits,
                vision_analysis=_vision_debug_metadata(vision),
            )
        result = await chat_async(
            messages,
            stream=stream,
            config=cfg,
            params=params,
        )
    except (ImageError, VisionContextError) as exc:
        _set_trace(request, error_type="invalid_request_error")
        # 图片加载/解码失败(SSRF 拒绝、字节/像素超限、格式不支持等)映射为 4xx
        return await _attach_lease(
            JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status_code=400,
            ),
            lease,
        )
    except (ComposeError, VisionBackendError) as exc:
        _set_trace(request, error_type="upstream_error")
        # 上游(DeepSeek / 视觉后端)失败:映射为 502,保持 OpenAI 兼容 error 体
        return await _attach_lease(
            JSONResponse(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                status_code=502,
            ),
            lease,
        )

    include_vision = request.headers.get("X-DeepSee-Include-Vision") == "1"
    if not stream:
        response = JSONResponse(
            openai_protocol.encode_upstream_response(
                result,
                vision=vision,
                include_vision=include_vision,
            )
        )
        response.headers["X-DeepSee-Vision-Cache-Hits"] = str(cache_hits)
        return await _attach_lease(response, lease)

    response = StreamingResponse(
        openai_protocol.encode_upstream_stream(
            result,
            vision=vision,
            include_vision=include_vision,
            on_error=lambda error_type: _set_trace(request, error_type=error_type),
        ),
        media_type="text/event-stream",
    )
    response.headers["X-DeepSee-Vision-Cache-Hits"] = str(cache_hits)
    return await _attach_lease(response, lease)


@app.post("/v1/dsv")
async def dsv_endpoint(request: Request):
    """DeepSee Vision public orchestration/output protocol."""
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

    # DSV validation, including image normalization, must complete before
    # loading configuration or consuming a request-guard lease.
    try:
        parsed = dsv.parse_request(body)
        limits_body = {"messages": parsed.messages, **parsed.params}
        max_tokens = _REQUEST_LIMITS.validate_openai(limits_body)
    except ValueError as exc:
        _set_trace(request, error_type="invalid_request_error")
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )

    cfg = _load_config_or_none()
    if cfg is None:
        _set_trace(request, error_type="configuration_error")
        return _openai_config_error()
    if cfg.vision.backend != "openai_compatible":
        _set_trace(request, error_type="configuration_error")
        return JSONResponse(
            {
                "error": {
                    "message": "DSV v1 要求视觉 provider 使用 OpenAI-compatible 后端",
                    "type": "configuration_error",
                }
            },
            status_code=503,
        )

    params = copy.deepcopy(parsed.params)
    if "max_tokens" not in params and "max_completion_tokens" not in params:
        params["max_tokens"] = max_tokens
    _set_trace(
        request,
        route="dsv",
        has_image=True,
        image_count=parsed.image_count,
        # DSH may use a composite route id (for example, a vision route
        # label) as the public request model. The gateway owns the actual
        # DeepSeek model selection, just like /v1/chat/completions.
        upstream_model=cfg.deepseek.model,
    )

    lease, guard_error = await _acquire_inference_lease(request)
    if guard_error is not None:
        return guard_error

    analysis_started = time.monotonic()
    try:
        transformed = await transform_messages_with_vision(
            parsed.messages,
            config=cfg,
            mode=parsed.vision_mode,
        )
    except (ImageError, VisionContextError) as exc:
        _set_trace(request, error_type="invalid_request_error")
        if parsed.stream:
            return await _attach_lease(
                StreamingResponse(
                    dsv.encode_error_stream(stage="vision", message=str(exc)),
                    media_type="text/event-stream",
                ),
                lease,
            )
        return await _attach_lease(
            JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status_code=400,
            ),
            lease,
        )
    except (ComposeError, VisionBackendError) as exc:
        _set_trace(request, error_type="upstream_error")
        if parsed.stream:
            return await _attach_lease(
                StreamingResponse(
                    dsv.encode_error_stream(stage="vision", message=str(exc)),
                    media_type="text/event-stream",
                ),
                lease,
            )
        return await _attach_lease(
            JSONResponse(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                status_code=502,
            ),
            lease,
        )

    analysis = "\n\n".join(transformed.analyses)
    _set_trace(
        request,
        cache_hits=transformed.cache_hits,
        vision_analysis=_vision_debug_metadata(analysis),
    )
    metadata = {
        "analysis": analysis,
        "mode": parsed.vision_mode,
        "backend": cfg.vision.backend,
        "model": cfg.vision.model_for_mode(parsed.vision_mode),
        "latency_ms": max(0, int((time.monotonic() - analysis_started) * 1000)),
        "cache_hit": transformed.cache_hits > 0,
        "cache_hits": transformed.cache_hits,
        "trace_id": getattr(request.state, "deepsee_trace_id", None),
    }

    try:
        result = await chat_async(
            transformed.messages,
            stream=parsed.stream,
            config=cfg,
            params=params,
            model=cfg.deepseek.model,
        )
    except (ComposeError, VisionBackendError) as exc:
        _set_trace(request, error_type="upstream_error")
        if parsed.stream:
            return await _attach_lease(
                StreamingResponse(
                    dsv.encode_error_stream(
                        stage="reasoning",
                        message=str(exc),
                        vision=metadata,
                        include_analysis=parsed.include_analysis,
                    ),
                    media_type="text/event-stream",
                ),
                lease,
            )
        return await _attach_lease(
            JSONResponse(
                dsv.encode_error_response(
                    stage="reasoning",
                    message=str(exc),
                    vision=metadata,
                    include_analysis=parsed.include_analysis,
                ),
                status_code=502,
            ),
            lease,
        )

    if not parsed.stream:
        try:
            payload = dsv.encode_response(
                result,
                vision=metadata,
                include_analysis=parsed.include_analysis,
            )
        except ValueError as exc:
            _set_trace(request, error_type="upstream_error")
            return await _attach_lease(
                JSONResponse(
                    dsv.encode_error_response(
                        stage="reasoning",
                        message=str(exc),
                        vision=metadata,
                        include_analysis=parsed.include_analysis,
                    ),
                    status_code=502,
                ),
                lease,
            )
        return await _attach_lease(JSONResponse(payload), lease)

    response = StreamingResponse(
        dsv.encode_stream(
            result,
            vision=metadata,
            include_analysis=parsed.include_analysis,
            on_error=lambda error_type: _set_trace(request, error_type=error_type),
        ),
        media_type="text/event-stream",
    )
    return await _attach_lease(response, lease)


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
    vision_mode = body.get("mode", "general")
    if vision_mode not in _VISION_MODES:
        return JSONResponse(
            {
                "error": {
                    "message": "mode 必须是 auto、ui 或 general",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
        )
    if not image:
        return JSONResponse(
            {"error": {"message": "缺少 image 字段", "type": "invalid_request_error"}},
            status_code=400,
        )
    try:
        _REQUEST_LIMITS.validate_analyze(body)
    except ValueError as exc:
        # 校验失败属于普通 4xx:不获取 guard,不消耗限流额度/并发槽
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )

    # 校验通过后才获取租约;图片加载也算上游调用,必须覆盖在租约内
    lease, guard_error = await _acquire_inference_lease(request)
    if guard_error is not None:
        return guard_error

    try:
        img = extract_image_from_url(image)
        cfg = _load_config_or_none()
        if cfg is None:
            return await _attach_lease(_openai_config_error(), lease)
        answer = await describe_image_async(
            img,
            question or "请描述这张图片",
            config=cfg,
            mode=vision_mode,
        )
    except ValueError as exc:
        return await _attach_lease(
            JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status_code=400,
            ),
            lease,
        )
    except ImageError as exc:
        return await _attach_lease(
            JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status_code=400,
            ),
            lease,
        )
    except (ComposeError, VisionBackendError) as exc:
        return await _attach_lease(
            JSONResponse(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                status_code=502,
            ),
            lease,
        )
    return await _attach_lease(
        JSONResponse({"kind": "description", "text": answer}), lease
    )


def _anthropic_error(status: int, message: str):
    """Anthropic 形状错误体(请求级错误,如 413 / 非法 JSON)。"""
    return JSONResponse(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        },
        status_code=status,
    )


def _anthropic_config_error():
    return JSONResponse(
        {
            "type": "error",
            "error": {
                "type": "configuration_error",
                "message": "服务配置不可用",
            },
        },
        status_code=503,
    )


def _gemini_error(status: int, message: str):
    """Gemini 形状错误体(请求级错误,如 413 / 非法 JSON)。"""
    return JSONResponse(
        {"error": {"code": status, "message": message}},
        status_code=status,
    )


def _gemini_config_error():
    return JSONResponse(
        {"error": {"code": 503, "message": "服务配置不可用"}},
        status_code=503,
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

    try:
        stream = _parse_stream(body)
        max_tokens = _REQUEST_LIMITS.validate_anthropic(body)
        vision_mode = _header_vision_mode(request)
    except ValueError as exc:
        return _anthropic_error(400, str(exc))

    # 先解析并校验请求(畸形请求不依赖配置,统一返回 400),再加载配置
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

    cfg = _load_config_or_none()
    if cfg is None:
        return _anthropic_config_error()
    model_id = cfg.deepseek.model

    # 请求校验已完成,配置有效;获取租约后再开始上游调用
    lease, guard_error = await _acquire_inference_lease(request)
    if guard_error is not None:
        return guard_error

    try:
        if image is not None:
            result = await ask_with_image_async(
                image, text or "请描述这张图片", stream=stream, config=cfg,
                include_vision=True, max_tokens=max_tokens, mode=vision_mode,
            )
            answer, vision = result.text, result.vision
        else:
            answer = await ask_async(
                text, stream=stream, config=cfg, max_tokens=max_tokens
            )
            vision = None
    except ImageError as exc:
        return await _attach_lease(
            JSONResponse(
                {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}},
                status_code=400,
            ),
            lease,
        )
    except (ComposeError, VisionBackendError) as exc:
        return await _attach_lease(
            JSONResponse(
                {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}},
                status_code=502,
            ),
            lease,
        )

    if not stream:
        return await _attach_lease(
            JSONResponse(anthropic.encode_text(answer, vision, model_id)), lease
        )
    return await _attach_lease(
        StreamingResponse(
            anthropic.encode_stream(
                answer,
                vision,
                model_id,
                on_error=lambda error_type: _set_trace(
                    request, error_type=error_type
                ),
            ),
            media_type="text/event-stream",
        ),
        lease,
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

    try:
        stream = _parse_stream(body)
        max_tokens = _REQUEST_LIMITS.validate_gemini(body)
        vision_mode = _header_vision_mode(request)
    except ValueError as exc:
        return _gemini_error(400, str(exc))

    # 先解析并校验请求(畸形请求不依赖配置,统一返回 400),再加载配置
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

    cfg = _load_config_or_none()
    if cfg is None:
        return _gemini_config_error()
    model_id = cfg.deepseek.model

    # 请求校验已完成,配置有效;获取租约后再开始上游调用
    lease, guard_error = await _acquire_inference_lease(request)
    if guard_error is not None:
        return guard_error

    try:
        if image is not None:
            result = await ask_with_image_async(
                image, text or "请描述这张图片", stream=stream, config=cfg,
                include_vision=True, max_tokens=max_tokens, mode=vision_mode,
            )
            answer, vision = result.text, result.vision
        else:
            answer = await ask_async(
                text, stream=stream, config=cfg, max_tokens=max_tokens
            )
            vision = None
    except ImageError as exc:
        return await _attach_lease(
            JSONResponse(
                {"error": {"code": 400, "message": str(exc)}},
                status_code=400,
            ),
            lease,
        )
    except (ComposeError, VisionBackendError) as exc:
        return await _attach_lease(
            JSONResponse(
                {"error": {"code": 502, "message": str(exc)}},
                status_code=502,
            ),
            lease,
        )

    if not stream:
        return await _attach_lease(
            JSONResponse(gemini.encode_text(answer, vision, model)), lease
        )
    return await _attach_lease(
        StreamingResponse(
            gemini.encode_stream(
                answer,
                vision,
                model,
                on_error=lambda error_type: _set_trace(
                    request, error_type=error_type
                ),
            ),
            media_type="text/event-stream",
        ),
        lease,
    )
