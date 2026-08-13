# OpenAI Full Message History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `/v1/chat/completions` 丢弃完整对话历史、工具调用字段、支持参数和上游响应结构的问题，同时保持当前网关安全改动与其他协议行为不变。

**Architecture:** 新增一个只负责 DeepSeek Chat Completions JSON/SSE 无损传输的深模块，以及一个只负责深拷贝并替换 `image_url` 块的视觉上下文深模块。OpenAI 协议适配器负责结构校验和参数白名单，FastAPI 路由负责按“校验、限额、视觉变换、上游传输、响应编码”的顺序编排，不再调用单问题 `ask_async`/`ask_with_image_async`。

**Tech Stack:** Python >= 3.10、FastAPI、httpx、pytest、respx、Hatch/uv。

## Global Constraints

- 只修 OpenAI `POST /v1/chat/completions`；Anthropic、Gemini、`/analyze` 和 `/v1/responses` 不扩展完整历史支持。
- 保留当前未提交的鉴权、CORS、限流、追踪、配置错误清理、请求体限制和静态托管改动，不覆盖或回退这些行为。
- 纯文本请求必须原样保留完整 `system/user/assistant/tool` 历史、消息级字段、工具定义和支持参数。
- 带图请求只替换 `image_url` 块；原输入对象、其他内容块、消息顺序、`tool_calls` 和 `tool_call_id` 不变。
- `assistant.content: null` 仅在存在数组形态 `tool_calls` 时接受。
- 顶层白名单固定为 `model`、`messages`、`stream`、`stream_options`、`tools`、`tool_choice`、`parallel_tool_calls`、`temperature`、`top_p`、`max_tokens`、`max_completion_tokens`、`stop`、`presence_penalty`、`frequency_penalty`、`response_format`、`seed` 和 `user`；未知字段返回 `400`。
- 单请求最多 4 张图片；单图视觉上下文最多 12000 字符；总视觉上下文最多 32000 字符；图片输入继续沿用 20 MiB、4096x4096 和 SSRF 防护。
- `X-DeepSee-Vision-Mode` 只接受 `auto/ui/general`；`X-DeepSee-Include-Vision: 1` 是返回 `vision_analysis` 的唯一开关。
- `RequestLimits` 继续限制消息数、图片数、文本字符和输出 token；调用方显式使用 `max_completion_tokens` 时不得改名为 `max_tokens`。
- TDD 按垂直切片执行：一个失败测试、最小实现、聚焦通过，再进入下一行为。
- 不增加生产依赖。

---

### Task 1: Raw DeepSeek Chat Transport

**Files:**
- Create: `deepsee/composer/chat.py`
- Modify: `deepsee/composer/__init__.py`
- Modify: `deepsee/__init__.py`
- Test: `tests/test_chat_transport.py`

**Interfaces:**
- Consumes: `retry_request_async`、`stream_request_async`、`Config`、`ComposeError`。
- Produces: `chat_async(messages, *, stream=False, config=None, params=None) -> dict[str, Any] | AsyncIterator[dict[str, Any]]`。

- [ ] **Step 1: 写非流式失败测试**

```python
def test_chat_async_preserves_messages_params_inputs_and_response(config):
    messages = [
        {"role": "system", "content": "Use tools."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
    ]
    params = {"tools": [], "tool_choice": "auto", "temperature": 0.2}
    original_messages = copy.deepcopy(messages)
    original_params = copy.deepcopy(params)
    upstream = {
        "id": "upstream-id",
        "choices": [{
            "message": {"role": "assistant", "content": None, "tool_calls": []},
            "finish_reason": "tool_calls",
        }],
        "usage": {"total_tokens": 7},
    }

    async def run():
        async with respx.mock:
            route = respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(200, json=upstream)
            )
            result = await chat_async(messages, config=config, params=params)
            return result, json.loads(route.calls.last.request.content)

    result, sent = asyncio.run(run())
    assert result == upstream
    assert sent["messages"] == messages
    assert sent["tools"] == []
    assert sent["tool_choice"] == "auto"
    assert sent["temperature"] == 0.2
    assert messages == original_messages
    assert params == original_params
```

- [ ] **Step 2: 跑测试确认红灯**

Run: `uv run pytest tests/test_chat_transport.py -q -k preserves_messages`

Expected: FAIL，`deepsee.composer.chat` 尚不存在。

- [ ] **Step 3: 实现非流式传输与公开导出**

```python
async def chat_async(messages, *, stream=False, config=None, params=None):
    cfg = config if config is not None else load_config()
    payload = copy.deepcopy(dict(params or {}))
    payload.update({
        "model": cfg.deepseek.model,
        "messages": copy.deepcopy(list(messages)),
        "stream": stream,
    })
    if stream:
        return _stream_json(cfg, payload)
    return await _request_json(cfg, payload)
```

`_request_json` 使用 `httpx.AsyncClient(timeout=120.0, trust_env=False)` 和
`retry_request_async`；响应根必须为对象，HTTP/网络/解析错误转换为带模型信息的
`ComposeError`，并在 `finally` 中关闭 client。将 `chat_async` 加入两个 `__all__`。

- [ ] **Step 4: 跑非流式测试确认绿灯**

Run: `uv run pytest tests/test_chat_transport.py -q -k preserves_messages`

Expected: PASS。

- [ ] **Step 5: 写流式失败测试**

```python
def test_chat_async_stream_preserves_complete_chunks(config):
    body = (
        'data: {"id":"stable","choices":[{"delta":{"tool_calls":'
        '[{"index":0,"id":"call-1","type":"function","function":'
        '{"name":"read","arguments":"{}"}}]},"finish_reason":null}]}\n\n'
        'data: {"id":"stable","choices":[{"delta":{},'
        '"finish_reason":"tool_calls"}],"usage":{"total_tokens":5}}\n\n'
        'data: [DONE]\n\n'
    )

    async def run():
        async with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(200, content=body.encode())
            )
            chunks = await chat_async(
                [{"role": "user", "content": "go"}],
                stream=True,
                config=config,
                params={"tools": []},
            )
            return [chunk async for chunk in chunks]

    chunks = asyncio.run(run())
    assert [chunk["id"] for chunk in chunks] == ["stable", "stable"]
    assert chunks[0]["choices"][0]["delta"]["tool_calls"][0]["id"] == "call-1"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"]["total_tokens"] == 5
```

- [ ] **Step 6: 实现受总时长限制的 SSE JSON 传输**

实现 `_bounded_lines(lines, timeout=300.0)`，用 `asyncio.wait_for` 限制整个流的墙钟
时间；`_stream_json` 只处理 `data:`，遇到 `[DONE]` 结束，JSON 根非对象时报
`ComposeError`，所有正常、异常和取消路径关闭 response 与 client。

- [ ] **Step 7: 跑传输测试和 composer 回归**

Run: `uv run pytest tests/test_chat_transport.py tests/test_composer.py -q`

Expected: PASS。

---

### Task 2: Bounded Vision Message Transformation

**Files:**
- Create: `deepsee/composer/vision_context.py`
- Test: `tests/test_vision_context.py`

**Interfaces:**
- Consumes: `_analyze_image_async(image, question, mode, config)`、`_format_context(result)`、`extract_image_from_url(url)`。
- Produces: `VisionTransformResult(messages, analyses, cache_hits)`、`VisionContextError`、`transform_messages_with_vision(...)`。

- [ ] **Step 1: 写“仅替换图片块”失败测试**

```python
def test_transform_replaces_only_images_and_preserves_history(config, monkeypatch):
    messages = [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64,aW1hZ2U="
                }},
            ],
        },
    ]
    original = copy.deepcopy(messages)

    async def fake_analyze(image, question, mode, cfg):
        assert image == b"image"
        assert question == "first\nsecond"
        assert mode == "ui"
        return {"kind": "description", "text": "save button"}

    monkeypatch.setattr(
        "deepsee.composer.vision_context._analyze_image_async", fake_analyze
    )
    result = asyncio.run(
        transform_messages_with_vision(messages, config=config, mode="ui")
    )

    assert messages == original
    assert result.messages[:4] == original[:4]
    assert result.messages[-1]["content"][:2] == original[-1]["content"][:2]
    replacement = result.messages[-1]["content"][2]["text"]
    assert 'trusted="false"' in replacement
    assert "DEEPSEE_VISUAL_CONTEXT" in replacement
    assert "save button" in replacement
```

- [ ] **Step 2: 跑测试确认红灯**

Run: `uv run pytest tests/test_vision_context.py -q -k replaces_only`

Expected: FAIL，视觉变换模块不存在。

- [ ] **Step 3: 实现深拷贝、问题聚合与不可信边界**

```python
MAX_IMAGES_PER_REQUEST = 4
MAX_CONTEXT_CHARS_PER_IMAGE = 12_000
MAX_CONTEXT_CHARS_TOTAL = 32_000

@dataclass
class VisionTransformResult:
    messages: list[dict[str, Any]]
    analyses: list[str]
    cache_hits: int = 0
```

`transform_messages_with_vision` 深拷贝消息；把同一消息内全部非空 text 块用 `\n`
连接；每个 `image_url.url` 通过 `extract_image_from_url` 解析；仅把对应块替换为带图片
编号的 text 块。非法模式在扫描图片前显式拒绝。

- [ ] **Step 4: 写资源上限与缓存失败测试**

追加测试覆盖 5 张图片返回 `VisionContextError`、单图 12001 字符、累计 32001 字符、
同图片/问题/模式/视觉配置第二次命中缓存、`clear_vision_context_cache()` 后重新分析。

- [ ] **Step 5: 实现 TTL/LRU 缓存和上下文限制**

使用进程内 `OrderedDict[str, tuple[float, str]]`，TTL 30 分钟、最多 128 项；缓存键为
图片 bytes 或 URL、问题、模式、视觉 backend/base URL/model 的 SHA-256。值只保存格式化
分析，不保存原图；超限抛 `VisionContextError`，不截断。

- [ ] **Step 6: 跑视觉变换与图片安全回归**

Run: `uv run pytest tests/test_vision_context.py tests/test_image.py tests/test_protocols/test_openai.py -q`

Expected: PASS。

---

### Task 3: OpenAI Request Contract

**Files:**
- Modify: `deepsee_server/protocols/openai.py`
- Modify: `tests/test_protocols/test_openai.py`

**Interfaces:**
- Consumes: JSON 请求对象。
- Produces: `ParsedChatRequest(messages, stream, params, image_count)`、`parse_chat_request(body)`、`encode_upstream_response(...)`、`encode_upstream_stream(...)`。

- [ ] **Step 1: 写完整消息和参数失败测试**

```python
def test_parse_chat_request_preserves_full_history_and_params():
    messages = [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
    ]
    body = {
        "model": "client-model",
        "messages": messages,
        "stream": False,
        "tools": [],
        "tool_choice": "auto",
        "max_completion_tokens": 100,
    }
    parsed = openai_protocol.parse_chat_request(body)
    assert parsed.messages == messages
    assert parsed.stream is False
    assert parsed.image_count == 0
    assert parsed.params == {
        "tools": [],
        "tool_choice": "auto",
        "max_completion_tokens": 100,
    }
```

- [ ] **Step 2: 跑测试确认红灯**

Run: `uv run pytest tests/test_protocols/test_openai.py -q -k full_history`

Expected: FAIL，`parse_chat_request` 尚不存在。

- [ ] **Step 3: 实现 dataclass、消息校验和参数白名单**

```python
@dataclass(frozen=True)
class ParsedChatRequest:
    messages: list[dict[str, Any]]
    stream: bool
    params: dict[str, Any]
    image_count: int
```

消息必须为非空数组，每项为对象且 role 是 `system/user/assistant/tool`；content 为字符串
或对象数组；assistant 仅在 `tool_calls` 为数组时允许 null；text 和 image_url 的叶字段
严格校验；未知对象内容块原样保留。`stream` 必须是 bool。白名单外字段以排序后的字段名
返回 `ValueError`。`model/messages/stream` 不进入 params。

- [ ] **Step 4: 写异常矩阵测试**

追加参数化测试：空 messages、未知 role、assistant null 无 tool_calls、非数组 tool_calls、
未知顶层字段、非布尔 stream、畸形 text/image_url、四种合法 role、未知对象内容块保留、
多图 image_count 正确。

- [ ] **Step 5: 实现非流式与流式响应保持器**

`encode_upstream_response` 深拷贝上游根对象，仅当 `include_vision and vision` 时向首个合法
choice.message 增加 `vision_analysis`。`encode_upstream_stream` 逐块 JSON 编码，不改变
上游块；视觉扩展在首个上游块前发送并复用其 id；已有 finish reason 不追加结束块；无
结束块时追加最小 stop 块；错误只发送 error 事件和 `[DONE]`；迭代器用 `aclosing` 关闭。

- [ ] **Step 6: 跑协议测试**

Run: `uv run pytest tests/test_protocols/test_openai.py -q`

Expected: PASS。

---

### Task 4: FastAPI Route Integration

**Files:**
- Modify: `deepsee_server/app.py`
- Create: `tests/test_server/test_openai_contract.py`

**Interfaces:**
- Consumes: `parse_chat_request`、`RequestLimits.validate_openai`、`transform_messages_with_vision`、`chat_async`、响应保持器。
- Produces: `/v1/chat/completions` 完整历史、工具和多图兼容行为。

- [ ] **Step 1: 写纯文本端到端失败测试**

```python
def test_chat_preserves_multi_turn_tools_params_and_upstream_response(config, monkeypatch):
    messages = [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "read"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
    ]
    upstream = {
        "id": "upstream-id",
        "choices": [{
            "message": {"role": "assistant", "content": None, "tool_calls": []},
            "finish_reason": "tool_calls",
        }],
        "usage": {"total_tokens": 9},
    }
    seen = {}

    async def fake_chat(received, **kwargs):
        seen["messages"] = received
        seen.update(kwargs)
        return upstream

    monkeypatch.setattr("deepsee_server.app._current_config", lambda: config)
    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)
    response = client.post("/v1/chat/completions", json={
        "messages": messages,
        "tools": [],
        "tool_choice": "auto",
        "max_completion_tokens": 123,
    })
    assert response.status_code == 200
    assert response.json() == upstream
    assert seen["messages"] == messages
    assert seen["params"]["max_completion_tokens"] == 123
```

- [ ] **Step 2: 跑测试确认红灯**

Run: `uv run pytest tests/test_server/test_openai_contract.py -q -k multi_turn`

Expected: FAIL，当前路由仍调用单问题 composer。

- [ ] **Step 3: 接入完整请求和安全限额**

路由顺序固定为：请求体/JSON → `parse_chat_request` → 视觉模式 →
`RequestLimits.validate_openai` → 配置加载 → 可选视觉变换 → `chat_async`。
`validate_openai` 的返回值只在调用方未提供两个输出字段时写入 `params["max_tokens"]`；
显式 `max_tokens` 或 `max_completion_tokens` 保持字段名和值。

- [ ] **Step 4: 写图片历史和视觉扩展失败测试**

测试传入工具历史、同消息多个 text 块和两张图片；fake transformer 验证收到完整消息；
fake chat 验证收到变换后的完整消息和 tools；普通响应不含 `vision_analysis`，带
`X-DeepSee-Include-Vision: 1` 时加入分析，cache hit header 和 trace 的图片数正确。

- [ ] **Step 5: 接入视觉变换、trace 与响应保持器**

图片存在时调用 `transform_messages_with_vision(parsed.messages, config=cfg, mode=mode)`；
视觉文本按 `\n\n` 合并；trace 填入 `route`、`has_image`、`image_count`、`cache_hits`、
`upstream_model` 和哈希化视觉元数据。捕获 `ImageError`、`VisionContextError` 为 400，
`ComposeError`、`VisionBackendError` 为 502。

- [ ] **Step 6: 写流式工具调用和错误失败测试**

测试两块上游数据具有同一 id，第一块含 tool_calls delta，第二块含
`finish_reason: tool_calls`；响应逐块相同且最后 `[DONE]`。另测流式迭代中抛
`ComposeError` 时仅出现 error 事件和 `[DONE]`，没有伪造 stop 块。

- [ ] **Step 7: 跑端点和安全回归**

Run: `uv run pytest tests/test_server/test_openai_contract.py tests/test_server/test_app.py tests/test_server/test_auth.py tests/test_server/test_request_guard.py tests/test_server/test_request_limits.py -q`

Expected: PASS。

---

### Task 5: Full Verification and Implementation Record

**Files:**
- Modify: `.ohmycodex/plans/implementation.md`
- Verify: all files changed by Tasks 1-4

**Interfaces:**
- Consumes: 完整实现和测试套件。
- Produces: 可复核的验证记录与残余风险说明。

- [ ] **Step 1: 跑聚焦协议与服务测试**

Run: `uv run pytest tests/test_chat_transport.py tests/test_vision_context.py tests/test_protocols/test_openai.py tests/test_server/test_openai_contract.py -q`

Expected: PASS。

- [ ] **Step 2: 跑全量测试**

Run: `uv run pytest -q`

Expected: 全部 PASS；仅允许已知 Starlette TestClient/httpx 弃用警告。

- [ ] **Step 3: 构建发行包**

Run: `uv build`

Expected: 成功生成 sdist 与 wheel。

- [ ] **Step 4: 静态检查变更**

Run: `git diff --check`

Expected: 无输出。

- [ ] **Step 5: 更新 OhMyCodex 实施记录**

在 `.ohmycodex/plans/implementation.md` 追加问题 4 的完成状态、实际命令与结果，并说明
未向真实付费上游发送请求、Anthropic/Gemini 完整历史仍不在范围。

- [ ] **Step 6: 提交策略**

计划文件单独提交。实现完成后，只提交能够与现有未提交网关改动明确区分的文件；若
`app.py` 或现有测试文件的提交会不可避免地夹带用户已有改动，则保留实现为未提交状态，
在最终交付中明确说明，而不重置、丢弃或擅自提交那些改动。
