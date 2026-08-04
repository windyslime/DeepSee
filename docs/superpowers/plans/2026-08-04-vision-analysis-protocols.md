# 多协议"看图问答 + 视觉分析可展开"实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 DeepSee Server 的聊天端点(OpenAI 兼容 / Anthropic messages / Gemini generateContent 三种协议形状)在回答中携带"视觉分析"元数据,供未来 GUI 像展开思考一样点击查看;库层 `ask_with_image_async` 增加 `include_vision` 参数暴露视觉分析。

**Architecture:** 库层组合管线(`deepsee/composer/deepseek.py`)新增 `VisionResult` 返回类型与 `include_vision` 参数(默认 False,零破坏);Server 层新增 `deepsee_server/protocols/` 模块,每个协议一个文件负责 `parse_request` / `encode_text` / `encode_stream`,`app.py` 只注册路由并复用现有请求体上限、图片防护与错误映射。视觉分析作为一次性前置元数据:非流式响应是普通字段,流式响应的首 chunk/首事件携带完整分析后再逐 chunk 输出回答。

**Tech Stack:** Python >= 3.10、FastAPI、httpx、pytest + respx。

## Global Constraints

- Python 版本要求:`requires-python = ">=3.10"`(禁止 3.11+ 专属语法,如 `asyncio.timeout`)。
- 命名规则:视觉分析字段名固定为 `vision_analysis`(OpenAI/Anthropic)与 `vision: true` part 标记(Gemini),见 spec §3。
- 错误映射:`ImageError` → 4xx;`ComposeError`/`VisionBackendError` → 5xx;错误体按各协议形状(OpenAI `{"error":{...}}`、Anthropic `{"type":"error","error":{...}}`、Gemini `{"error":{"code","message"}}`)。
- 安全:所有图片 URL(data URL / http / Anthropic source.url / Gemini file_uri)统一走 `protocols/base.py::extract_image_from_url`(data URL 解码 + 字节上限、http(s) 放行、其余拒绝);base64 解码走 `decode_base64_image`(字节上限)。
- `include_vision=False`(默认)必须保持现状行为:返回 `str` / `AsyncIterator[str]`。
- 请求体上限 32 MiB 与 chunked 流式读取对所有端点生效(复用 `app._read_body_limited`)。
- TDD:每个任务先写失败测试,再实现,再跑通,再提交;conventional commits。
- 计划执行时工作区已有未提交的 L1-L4 修复(7 个文件),与本计划无关,不要混入本计划的提交。

---

### Task 1: 库层 `VisionResult` + `include_vision` 参数

**Files:**
- Modify: `deepsee/composer/deepseek.py`(新增 `VisionResult` dataclass;修改 `ask_with_image_async` 223-243 行)
- Test: `tests/test_composer.py`

**Interfaces:**
- Consumes: `_analyze_image_async` / `_format_context` / `_compose_messages` / `_run_deepseek_async`(均已存在,签名不变)
- Produces:
  - `VisionResult` dataclass:`vision: str`, `text: str | AsyncIterator[str]`
  - `ask_with_image_async(image, question, *, stream=False, config=None, mode="auto", include_vision=False) -> str | AsyncIterator[str] | VisionResult`

- [ ] **Step 1: 写失败测试(追加到 `tests/test_composer.py` 末尾)**

```python
def test_ask_with_image_async_include_vision_non_stream(
    config, sample_image_bytes, monkeypatch
):
    # 视觉后端统一用 fake,避免真实网络调用
    _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )

    async def _run(include_vision):
        async with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "白猫"}}]},
                )
            )
            return await ask_with_image_async(
                sample_image_bytes, "是什么?", config=config,
                include_vision=include_vision,
            )

    result = asyncio.run(_run(include_vision=True))
    assert isinstance(result, VisionResult)
    assert result.text == "白猫"
    assert FAKE_DESCRIPTION in result.vision

    # 默认(include_vision=False)返回 str,行为不变
    plain = asyncio.run(_run(include_vision=False))
    assert isinstance(plain, str)
    assert plain == "白猫"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_composer.py -q -k include_vision`
Expected: FAIL,`ImportError: cannot import name 'VisionResult'`

- [ ] **Step 3: 实现**

在 `deepsee/composer/deepseek.py` 顶部(import 区,`from typing import Any, Union` 之后)新增:

```python
from dataclasses import dataclass


@dataclass
class VisionResult:
    """Composed answer plus the vision analysis used as context.

    ``vision`` 是注入 DeepSeek 的上下文文本(_format_context 的结果,
    描述或 UI 元素地图),供调用方展开展示;"text" 是最终回答,流式时为
    ``AsyncIterator[str]``。
    """

    vision: str
    text: Union[str, AsyncIterator[str]]
```

将 `ask_with_image_async`(223-243 行)整体替换为:

```python
async def ask_with_image_async(
    image: ImageInput,
    question: str,
    *,
    stream: bool = False,
    config: Config | None = None,
    mode: str = "auto",
    include_vision: bool = False,
) -> Union[str, AsyncIterator[str], VisionResult]:
    """Async full composition: vision analysis → DeepSeek reasoning.

    Same ``mode`` semantics and prompt-injection mitigations as the
    synchronous ``ask_with_image``. With ``stream=True`` the returned async
    iterator must be exhausted or closed via ``aclose()`` (e.g. with
    ``contextlib.aclosing``) to release the underlying HTTP connection.

    ``include_vision=True`` 返回 ``VisionResult``(视觉分析 + 回答);默认
    ``False`` 保持原有返回类型(``str`` / ``AsyncIterator[str]``)不变。
    """
    cfg = config if config is not None else load_config()
    vision_result = await _analyze_image_async(image, question, mode, cfg)
    context = _format_context(vision_result)
    messages = _compose_messages(question, context)
    payload = {
        "model": cfg.deepseek.model,
        "messages": messages,
        "stream": stream,
    }
    answer = await _run_deepseek_async(cfg, payload)
    if not include_vision:
        return answer
    return VisionResult(vision=context, text=answer)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_composer.py -q -k include_vision`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add deepsee/composer/deepseek.py tests/test_composer.py
git commit -m "feat(composer): expose vision analysis via VisionResult/include_vision"
```

---

### Task 2: 库层 `VisionResult` 流式场景测试

**Files:**
- Test: `tests/test_composer.py`

**Interfaces:**
- Consumes: Task 1 的 `VisionResult` 与 `ask_with_image_async(..., include_vision=True)`
- Produces: 无新接口;验证流式路径 `VisionResult.text` 是 `AsyncIterator[str]` 且 `vision` 完整

- [ ] **Step 1: 写失败测试(追加到 `tests/test_composer.py` 末尾)**

```python
def test_ask_with_image_async_include_vision_stream(
    config, sample_image_bytes, monkeypatch
):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )
    sse_body = (
        'data: {"choices": [{"delta": {"content": "是"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "白猫"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    async def _run():
        async with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(200, content=sse_body.encode())
            )
            result = await ask_with_image_async(
                sample_image_bytes,
                "是什么?",
                config=config,
                stream=True,
                include_vision=True,
            )
            assert isinstance(result, VisionResult)
            chunks = [c async for c in result.text]
            return result, chunks

    result, chunks = asyncio.run(_run())
    assert chunks == ["是", "白猫"]
    assert FAKE_DESCRIPTION in result.vision  # vision 完整,不流式
```

- [ ] **Step 2: 跑测试确认通过(实现已在 Task 1 完成)**

Run: `uv run pytest tests/test_composer.py -q -k "include_vision and stream"`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add tests/test_composer.py
git commit -m "test(composer): cover streaming include_vision path"
```

---

### Task 3: protocols 共享工具 + OpenAI 协议适配器

**Files:**
- Create: `deepsee_server/protocols/__init__.py`
- Create: `deepsee_server/protocols/base.py`
- Create: `deepsee_server/protocols/openai.py`
- Test: `tests/test_protocols/__init__.py`、`tests/test_protocols/test_openai.py`

**Interfaces:**
- Consumes: `deepsee.pipeline.image.MAX_IMAGE_BYTES`、`deepsee.errors.ComposeError/VisionBackendError`
- Produces:
  - `protocols/base.py::extract_image_from_url(url: str) -> bytes | str`
  - `protocols/base.py::decode_base64_image(data: str) -> bytes`
  - `protocols/openai.py::parse_request(body: dict) -> tuple[str, bytes | str | None]`
  - `protocols/openai.py::encode_text(answer: str, vision: str | None, model: str) -> dict`
  - `protocols/openai.py::encode_stream(chunks: AsyncIterator[str], vision: str | None, model: str) -> AsyncIterator[bytes]`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_protocols/__init__.py`(空文件)。

创建 `tests/test_protocols/test_openai.py`:

```python
import asyncio
import base64
import json

import pytest

from deepsee_server.protocols import openai as openai_protocol
from deepsee_server.protocols.base import MAX_IMAGE_BYTES, extract_image_from_url


def _data_url(b: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(b).decode()}"


def test_parse_request_text_and_data_url_image(sample_image_bytes):
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这是什么?"},
                    {"type": "image_url", "image_url": {"url": _data_url(sample_image_bytes)}},
                ],
            }
        ]
    }
    text, image = openai_protocol.parse_request(body)
    assert text == "这是什么?"
    assert image == sample_image_bytes


def test_parse_request_http_url_passthrough():
    body = {
        "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
            ]}
        ]
    }
    text, image = openai_protocol.parse_request(body)
    assert text == ""
    assert image == "https://example.com/a.png"


def test_parse_request_rejects_file_url():
    body = {
        "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}}
            ]}
        ]
    }
    with pytest.raises(ValueError, match="不支持的图片 URL"):
        openai_protocol.parse_request(body)


def test_parse_request_no_image():
    text, image = openai_protocol.parse_request(
        {"messages": [{"role": "user", "content": "你好"}]}
    )
    assert text == "你好"
    assert image is None


def test_extract_image_from_url_over_limit(sample_image_bytes, monkeypatch):
    monkeypatch.setattr("deepsee_server.protocols.base.MAX_IMAGE_BYTES", 4)
    with pytest.raises(ValueError, match="图片数据过大"):
        extract_image_from_url(_data_url(sample_image_bytes))


def test_encode_text_carries_vision():
    payload = openai_protocol.encode_text("白猫", "图片里有一只猫", "deepseek-chat")
    assert payload["choices"][0]["message"]["content"] == "白猫"
    assert payload["choices"][0]["message"]["vision_analysis"] == "图片里有一只猫"
    assert payload["model"] == "deepseek-chat"


def test_encode_text_no_vision():
    payload = openai_protocol.encode_text("你好", None, "deepseek-chat")
    assert "vision_analysis" not in payload["choices"][0]["message"]


async def _chunks():
    yield "你"
    yield "好"


def test_encode_stream_first_chunk_carries_vision():
    async def _run():
        out = []
        async for chunk in openai_protocol.encode_stream(
            _chunks(), "视觉分析内容", "deepseek-chat"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    lines = [ln for ln in b"".join(out).decode().splitlines() if ln.startswith("data: ")]
    first = json.loads(lines[0][6:])
    assert first["choices"][0]["delta"]["vision_analysis"] == "视觉分析内容"
    assert first["choices"][0]["delta"]["content"] == "你"
    second = json.loads(lines[1][6:])
    assert "vision_analysis" not in second["choices"][0]["delta"]
    assert lines[-1] == "data: [DONE]"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_protocols/ -q`
Expected: FAIL,`ModuleNotFoundError: deepsee_server.protocols`

- [ ] **Step 3: 实现**

创建 `deepsee_server/protocols/__init__.py`:

```python
"""Protocol adapters: parse inbound requests, encode responses per shape."""
```

创建 `deepsee_server/protocols/base.py`:

```python
"""Shared helpers for protocol adapters: image extraction & size limits."""

from __future__ import annotations

import re
from base64 import b64decode

from deepsee.pipeline.image import MAX_IMAGE_BYTES


def extract_image_from_url(url: str) -> bytes | str:
    """Accept base64 data: URLs (→ bytes) or http(s) URLs (→ URL string).

    http(s) URL 的下载防护(SSRF / 字节上限)在 ``load_image`` 层;data URL
    在此解码并做字节上限检查;``file://`` 与本地路径一律拒绝。
    """
    if not isinstance(url, str):
        raise ValueError(f"不支持的图片 URL 形式: {url!r}")
    if url.startswith("data:"):
        m = re.match(r"data:[^;]+;base64,(.*)", url, re.DOTALL)
        if not m:
            raise ValueError("仅支持 base64 data URL 图片")
        raw = b64decode(m.group(1))
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"图片数据过大(超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB)"
            )
        return raw
    if url.startswith("http://") or url.startswith("https://"):
        return url
    raise ValueError(f"不支持的图片 URL 形式: {url[:60]}")


def decode_base64_image(data: str) -> bytes:
    """Decode a bare base64 payload (Anthropic source / Gemini inline_data)."""
    if not isinstance(data, str) or not data:
        raise ValueError("图片 base64 数据缺失")
    raw = b64decode(data)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"图片数据过大(超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB)"
        )
    return raw
```

创建 `deepsee_server/protocols/openai.py`:

```python
"""OpenAI-compatible chat completions protocol adapter."""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from deepsee.errors import ComposeError, VisionBackendError

from .base import extract_image_from_url


def parse_request(body: dict) -> tuple[str, bytes | str | None]:
    """Extract the last user text and an optional image (OpenAI shape)."""
    text = ""
    image = None
    for msg in body.get("messages", []):
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
                        image = extract_image_from_url(url)
    return text, image


def encode_text(answer: str, vision: str | None, model: str) -> dict:
    """Non-streaming completion payload with optional vision_analysis."""
    message: dict[str, Any] = {"role": "assistant", "content": answer}
    if vision is not None:
        message["vision_analysis"] = vision
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def encode_stream(
    chunks: AsyncIterator[str],
    vision: str | None,
    model: str,
) -> AsyncIterator[bytes]:
    """SSE stream: vision_analysis on the first chunk, then content, then [DONE].

    ``chunks`` 在结束/异常/取消时都会被 ``aclose``(不依赖 GC)。
    """
    try:
        async with contextlib.aclosing(chunks):
            async for chunk in chunks:
                delta: dict[str, Any] = {"content": chunk}
                if vision is not None:
                    delta["vision_analysis"] = vision
                    vision = None  # 只出现在首个 chunk
                payload = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": None}
                    ],
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
    except (ComposeError, VisionBackendError) as exc:
        yield (
            "data: "
            + json.dumps(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode()
    yield b"data: [DONE]\n\n"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_protocols/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add deepsee_server/protocols/ tests/test_protocols/
git commit -m "feat(protocols): add shared image helpers and OpenAI adapter"
```

---

### Task 4: Anthropic messages 协议适配器

**Files:**
- Create: `deepsee_server/protocols/anthropic.py`
- Test: `tests/test_protocols/test_anthropic.py`

**Interfaces:**
- Consumes: `protocols/base.py::extract_image_from_url` / `decode_base64_image`、`deepsee.errors`
- Produces:
  - `anthropic.parse_request(body) -> tuple[str, bytes | str | None]`
  - `anthropic.encode_text(answer, vision, model) -> dict`
  - `anthropic.encode_stream(chunks, vision, model) -> AsyncIterator[bytes]`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_protocols/test_anthropic.py`:

```python
import asyncio
import base64
import json

import pytest

from deepsee_server.protocols import anthropic as anthropic_protocol


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_parse_request_base64_image_and_text(sample_image_bytes):
    body = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": _b64(sample_image_bytes),
                        },
                    },
                    {"type": "text", "text": "这是什么?"},
                ],
            }
        ],
    }
    text, image = anthropic_protocol.parse_request(body)
    assert text == "这是什么?"
    assert image == sample_image_bytes


def test_parse_request_url_image(sample_image_bytes):
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": "https://example.com/a.png",
                        },
                    },
                    {"type": "text", "text": "q"},
                ],
            }
        ]
    }
    text, image = anthropic_protocol.parse_request(body)
    assert text == "q"
    assert image == "https://example.com/a.png"


def test_parse_request_rejects_file_url():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "file:///etc/passwd"},
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="不支持的图片 URL"):
        anthropic_protocol.parse_request(body)


def test_parse_request_no_image():
    text, image = anthropic_protocol.parse_request(
        {"messages": [{"role": "user", "content": "你好"}]}
    )
    assert text == "你好"
    assert image is None


def test_encode_text_carries_vision():
    payload = anthropic_protocol.encode_text("白猫", "视觉分析", "claude-3-5-sonnet")
    assert payload["content"] == [{"type": "text", "text": "白猫"}]
    assert payload["vision_analysis"] == "视觉分析"
    assert payload["model"] == "claude-3-5-sonnet"


def test_encode_text_no_vision():
    payload = anthropic_protocol.encode_text("你好", None, "m")
    assert "vision_analysis" not in payload


async def _chunks():
    yield "你"
    yield "好"


def test_encode_stream_emits_vision_event_before_content():
    async def _run():
        out = []
        async for chunk in anthropic_protocol.encode_stream(
            _chunks(), "视觉分析", "m"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    lines = [ln for ln in b"".join(out).decode().splitlines() if ln.startswith("data: ")]
    events = [json.loads(ln[6:]) for ln in lines]
    assert events[0]["type"] == "message_start"
    assert events[1]["type"] == "vision_analysis"
    assert events[1]["vision"] == "视觉分析"
    assert events[2]["type"] == "content_block_start"
    # 回答文本以 text_delta 逐块到达
    deltas = [e for e in events if e["type"] == "content_block_delta"]
    assert [d["delta"]["text"] for d in deltas] == ["你", "好"]
    assert events[-1]["type"] == "message_stop"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_protocols/test_anthropic.py -q`
Expected: FAIL,`ModuleNotFoundError: deepsee_server.protocols.anthropic`

- [ ] **Step 3: 实现**

创建 `deepsee_server/protocols/anthropic.py`:

```python
"""Anthropic messages protocol adapter (shape-compatible)."""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator

from deepsee.errors import ComposeError, VisionBackendError

from .base import decode_base64_image, extract_image_from_url


def parse_request(body: dict) -> tuple[str, bytes | str | None]:
    """Extract the last user text and an optional image (Anthropic shape).

    图片块 ``{type: "image", source: ...}``:base64 source 解码为 bytes;url
    source 交给 ``extract_image_from_url``(http(s) 放行,file:// 拒绝)。
    """
    text = ""
    image = None
    for msg in body.get("messages", []):
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
                elif btype == "image" and image is None:
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        data = source.get("data", "")
                        if data:
                            image = decode_base64_image(data)
                    elif source.get("type") == "url":
                        url = source.get("url", "")
                        if url:
                            image = extract_image_from_url(url)
    return text, image


def encode_text(answer: str, vision: str | None, model: str) -> dict:
    """Non-streaming message payload with optional top-level vision_analysis."""
    resp: dict = {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": answer}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    if vision is not None:
        resp["vision_analysis"] = vision
    return resp


def _event(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


async def encode_stream(
    chunks: AsyncIterator[str],
    vision: str | None,
    model: str,
) -> AsyncIterator[bytes]:
    """SSE event stream: message_start → vision_analysis → text deltas → stop."""
    yield _event(
        {
            "type": "message_start",
            "message": {
                "id": f"msg_{uuid.uuid4().hex[:12]}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
    )
    if vision is not None:
        yield _event({"type": "vision_analysis", "vision": vision})
    yield _event(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
    )
    try:
        async with contextlib.aclosing(chunks):
            async for chunk in chunks:
                yield _event(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": chunk},
                    }
                )
    except (ComposeError, VisionBackendError) as exc:
        yield _event(
            {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}}
        )
    yield _event({"type": "content_block_stop", "index": 0})
    yield _event(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 0},
        }
    )
    yield _event({"type": "message_stop"})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_protocols/test_anthropic.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add deepsee_server/protocols/anthropic.py tests/test_protocols/test_anthropic.py
git commit -m "feat(protocols): add Anthropic messages adapter with vision event"
```

---

### Task 5: Gemini generateContent 协议适配器

**Files:**
- Create: `deepsee_server/protocols/gemini.py`
- Test: `tests/test_protocols/test_gemini.py`

**Interfaces:**
- Consumes: `protocols/base.py::extract_image_from_url` / `decode_base64_image`、`deepsee.errors`
- Produces:
  - `gemini.parse_request(body) -> tuple[str, bytes | str | None]`
  - `gemini.encode_text(answer, vision, model) -> dict`
  - `gemini.encode_stream(chunks, vision, model) -> AsyncIterator[bytes]`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_protocols/test_gemini.py`:

```python
import asyncio
import base64
import json

import pytest

from deepsee_server.protocols import gemini as gemini_protocol


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_parse_request_inline_data_image(sample_image_bytes):
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": _b64(sample_image_bytes),
                        }
                    },
                    {"text": "这是什么?"},
                ],
            }
        ]
    }
    text, image = gemini_protocol.parse_request(body)
    assert text == "这是什么?"
    assert image == sample_image_bytes


def test_parse_request_file_data_image():
    body = {
        "contents": [
            {
                "parts": [
                    {"file_data": {"file_uri": "https://example.com/a.png"}},
                    {"text": "q"},
                ]
            }
        ]
    }
    text, image = gemini_protocol.parse_request(body)
    assert text == "q"
    assert image == "https://example.com/a.png"


def test_parse_request_rejects_file_uri():
    body = {
        "contents": [
            {
                "parts": [
                    {"file_data": {"file_uri": "file:///etc/passwd"}},
                ]
            }
        ]
    }
    with pytest.raises(ValueError, match="不支持的图片 URL"):
        gemini_protocol.parse_request(body)


def test_parse_request_no_image():
    text, image = gemini_protocol.parse_request(
        {"contents": [{"parts": [{"text": "你好"}]}]}
    )
    assert text == "你好"
    assert image is None


def test_encode_text_vision_part_first():
    payload = gemini_protocol.encode_text("白猫", "视觉分析", "gemini-2.0-flash")
    parts = payload["candidates"][0]["content"]["parts"]
    assert parts[0] == {"text": "视觉分析", "vision": True}
    assert parts[1] == {"text": "白猫"}


def test_encode_text_no_vision():
    payload = gemini_protocol.encode_text("你好", None, "m")
    assert payload["candidates"][0]["content"]["parts"] == [{"text": "你好"}]


async def _chunks():
    yield "你"
    yield "好"


def test_encode_stream_first_chunk_carries_vision():
    async def _run():
        out = []
        async for chunk in gemini_protocol.encode_stream(
            _chunks(), "视觉分析", "m"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    chunks = [json.loads(c) for c in out]
    first_parts = chunks[0]["candidates"][0]["content"]["parts"]
    assert first_parts[0] == {"text": "视觉分析", "vision": True}
    assert first_parts[1] == {"text": "你"}
    second_parts = chunks[1]["candidates"][0]["content"]["parts"]
    assert second_parts == [{"text": "好"}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_protocols/test_gemini.py -q`
Expected: FAIL,`ModuleNotFoundError: deepsee_server.protocols.gemini`

- [ ] **Step 3: 实现**

创建 `deepsee_server/protocols/gemini.py`:

```python
"""Google Gemini generateContent protocol adapter (shape-compatible)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator

from deepsee.errors import ComposeError, VisionBackendError

from .base import decode_base64_image, extract_image_from_url


def parse_request(body: dict) -> tuple[str, bytes | str | None]:
    """Extract the last text and an optional image (Gemini shape).

    ``inline_data``(base64)解码为 bytes;``file_data.file_uri`` 交给
    ``extract_image_from_url``(http(s) 放行,file:// 拒绝)。
    """
    text = ""
    image = None
    for content in body.get("contents", []):
        for part in content.get("parts", []):
            if "text" in part:
                text = part["text"]
            elif "inline_data" in part and image is None:
                data = part["inline_data"].get("data", "")
                if data:
                    image = decode_base64_image(data)
            elif "file_data" in part and image is None:
                uri = part["file_data"].get("file_uri", "")
                if uri:
                    image = extract_image_from_url(uri)
    return text, image


def encode_text(answer: str, vision: str | None, model: str) -> dict:
    """Non-streaming generateContent payload with vision part first."""
    parts = []
    if vision is not None:
        parts.append({"text": vision, "vision": True})
    parts.append({"text": answer})
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 0,
            "candidatesTokenCount": 0,
            "totalTokenCount": 0,
        },
    }


async def encode_stream(
    chunks: AsyncIterator[str],
    vision: str | None,
    model: str,
) -> AsyncIterator[bytes]:
    """Chunk stream (newline-delimited JSON): vision part on first chunk."""
    try:
        async with contextlib.aclosing(chunks):
            async for chunk in chunks:
                parts = []
                if vision is not None:
                    parts.append({"text": vision, "vision": True})
                    vision = None
                parts.append({"text": chunk})
                payload = {
                    "candidates": [
                        {"content": {"role": "model", "parts": parts}, "index": 0}
                    ]
                }
                yield json.dumps(payload, ensure_ascii=False).encode() + b"\n"
    except (ComposeError, VisionBackendError) as exc:
        yield json.dumps(
            {"error": {"code": 502, "message": str(exc)}}, ensure_ascii=False
        ).encode() + b"\n"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_protocols/test_gemini.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add deepsee_server/protocols/gemini.py tests/test_protocols/test_gemini.py
git commit -m "feat(protocols): add Gemini generateContent adapter with vision part"
```

---

### Task 6: server 端点接入(chat_completions 升级 + 新增两协议端点)

**Files:**
- Modify: `deepsee_server/app.py`
- Modify: `tests/test_server/test_app.py`

**Interfaces:**
- Consumes: Task 1 的 `VisionResult` / `ask_with_image_async(include_vision=True)`;Task 3-5 的三个协议适配器;现有 `_read_body_limited` / `_body_too_large`
- Produces:
  - `POST /v1/chat/completions` 响应携带 `vision_analysis`(有图时)
  - `POST /v1/messages`(Anthropic 形状)
  - `POST /v1beta/models/{model}:generateContent`(Gemini 形状)

- [ ] **Step 1: 写失败测试(更新 + 追加 `tests/test_server/test_app.py`)**

更新 `test_chat_with_image`(69-97 行):fake 改为返回 `VisionResult`,断言 `vision_analysis` 字段:

```python
def test_chat_with_image(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    seen = {}

    async def fake_ask_with_image(image, question, **kw):
        seen["image"] = image
        seen["question"] = question
        return VisionResult(vision="图里有一只猫", text="图里是一只猫")

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "图里有什么?"},
                        {"type": "image_url", "image_url": {"url": _png_data_url()}},
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()["choices"][0]["message"]
    assert body["content"] == "图里是一只猫"
    assert body["vision_analysis"] == "图里有一只猫"
    assert isinstance(seen["image"], bytes)
    assert seen["question"] == "图里有什么?"
```

更新 `test_chat_data_url_over_limit_400`(162-181 行)的 monkeypatch 路径:

```python
def test_chat_data_url_over_limit_400(use_cfg, monkeypatch):
    monkeypatch.setattr("deepsee_server.protocols.base.MAX_IMAGE_BYTES", 64)
    ...
```

追加(文件末尾)新端点与流式 vision 测试:

```python
def test_chat_stream_with_vision_first_chunk(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    async def fake_ask_with_image(image, question, **kw):
        async def gen():
            yield "你"
            yield "好"

        return VisionResult(vision="视觉分析内容", text=gen())

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1/chat/completions",
        json={
            "stream": True,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _png_data_url()}},
                        {"type": "text", "text": "hi"},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    first = json.loads(lines[0][6:])
    assert first["choices"][0]["delta"]["vision_analysis"] == "视觉分析内容"
    assert first["choices"][0]["delta"]["content"] == "你"
    assert lines[-1] == "data: [DONE]"


def test_messages_endpoint_anthropic(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    async def fake_ask_with_image(image, question, **kw):
        return VisionResult(vision="视觉分析", text="白猫")

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            },
                        },
                        {"type": "text", "text": "这是什么?"},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "白猫"}]
    assert body["vision_analysis"] == "视觉分析"


def test_messages_endpoint_no_image_plain_text(use_cfg, monkeypatch):
    async def fake_ask(question, **kw):
        return "你好!"

    monkeypatch.setattr("deepsee_server.app.ask_async", fake_ask)
    resp = client.post(
        "/v1/messages",
        json={"model": "m", "messages": [{"role": "user", "content": "你好"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == [{"type": "text", "text": "你好!"}]
    assert "vision_analysis" not in body


def test_gemini_endpoint(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    async def fake_ask_with_image(image, question, **kw):
        return VisionResult(vision="视觉分析", text="白猫")

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            }
                        },
                        {"text": "这是什么?"},
                    ]
                }
            ]
        },
    )
    assert resp.status_code == 200
    parts = resp.json()["candidates"][0]["content"]["parts"]
    assert parts[0] == {"text": "视觉分析", "vision": True}
    assert parts[1] == {"text": "白猫"}


def test_messages_endpoint_upstream_error_502(use_cfg, monkeypatch):
    from deepsee.errors import ComposeError

    async def boom(image, question, **kw):
        raise ComposeError("DeepSeek API 请求失败: HTTP 502", model="m", status_code=502)

    monkeypatch.setattr("deepsee_server.app.ask_with_image_async", boom)
    resp = client.post(
        "/v1/messages",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            },
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 502
    assert resp.json()["type"] == "error"
    assert resp.json()["error"]["type"] == "upstream_error"
```

在 `test_app.py` 顶部 helper 区(`_png_data_url` 之后)追加:

```python
def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_server/test_app.py -q`
Expected: FAIL(新端点 404、旧端点缺 `vision_analysis` 字段)

- [ ] **Step 3: 实现(修改 `deepsee_server/app.py`)**

3a. 替换 import 区(25-27 行):

```python
from deepsee import ask_async, ask_with_image_async, describe_image_async, load_config
from deepsee.errors import ComposeError, ImageError, VisionBackendError
from deepsee.pipeline.image import MAX_IMAGE_BYTES
```
改为:

```python
from deepsee import ask_async, ask_with_image_async, describe_image_async, load_config
from deepsee.errors import ComposeError, ImageError, VisionBackendError
from deepsee_server.protocols import anthropic, gemini
from deepsee_server.protocols.base import extract_image_from_url
from deepsee_server.protocols import openai as openai_protocol
```

> 注意:`describe_image_async` 必须保留(`/analyze` 端点仍用它);`MAX_IMAGE_BYTES`
> 在删除 `_extract_image_from_url` 后不再被 app.py 直接引用,移除该 import
> (字节上限检查由 `protocols/base.py` 承担)。

3b. 删除 `_extract_image_from_url`(84-102 行)与 `_parse_messages`(106-124 行)两个函数(逻辑已由协议适配器承担;`extract_image_from_url` 现在从 `protocols.base` 导入)。

3c. 替换 `chat_completions` 主体(176-246 行区域),要点:

```python
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
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )
    except (ComposeError, VisionBackendError) as exc:
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
```

> 注意:原 `gen()` 闭包与 `_completion_payload` 的职责由 `openai_protocol.encode_stream` / `encode_text` 承担,删除 `_completion_payload`(128-142 行)。

3d. 修改 `/analyze` 端点(250-302 行):`_extract_image_from_url` 调用改为 `extract_image_from_url`(已 import)。

3e. 在 `/analyze` 之后追加两个新端点:

```python
def _openai_style_413():
    return JSONResponse(
        {"error": {"message": "请求体过大", "type": "invalid_request_error"}},
        status_code=413,
    )


def _openai_style_400(message: str):
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error"}},
        status_code=400,
    )


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic messages 形状端点(内部视觉分析可展开,GUI 使用)。"""
    if _body_too_large(request):
        return _openai_style_413()
    body_bytes = await _read_body_limited(request)
    if body_bytes is None:
        return _openai_style_413()
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _openai_style_400("请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _openai_style_400("请求体必须是 JSON 对象")

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
        return _openai_style_413()
    body_bytes = await _read_body_limited(request)
    if body_bytes is None:
        return _openai_style_413()
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _openai_style_400("请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _openai_style_400("请求体必须是 JSON 对象")

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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_server/test_app.py tests/test_protocols/ -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
uv run pytest -q
git add deepsee_server/app.py tests/test_server/test_app.py
git commit -m "feat(server): wire vision_analysis into chat completions; add anthropic/gemini endpoints"
```

---

### Task 7: README 文档

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 6 的端点与字段命名

- [ ] **Step 1: 更新 README**

在「## 安全限制」之前新增小节「## 多协议端点」:

```markdown
## 多协议端点

服务同时暴露三种协议形状的聊天端点,视觉分析结果作为响应元数据返回,
供 GUI 像展开思考过程一样点击查看(字段语义 = "模型看到了什么"):

- `POST /v1/chat/completions` — OpenAI 兼容;有图时非流式响应
  `choices[0].message.vision_analysis`,流式响应的首个 chunk 携带
  `choices[0].delta.vision_analysis`;
- `POST /v1/messages` — Anthropic messages 形状;非流式响应顶层
  `vision_analysis`,流式响应在 `message_start` 后发
  `{"type": "vision_analysis", "vision": ...}` 事件;
- `POST /v1beta/models/{model}:generateContent` — Gemini 形状;视觉分析
  作为 `parts` 首位的 `{"text": ..., "vision": true}` part。

三种端点都支持 `stream` 参数(流式/非流式),图片输入按各自协议形状
(data URL / base64 source / inline_data / http URL),统一受 SSRF 防护与
字节上限约束;`file://` 与本地路径一律拒绝。
```

- [ ] **Step 2: 验证无测试受影响**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs: document multi-protocol endpoints and vision_analysis field"
```
