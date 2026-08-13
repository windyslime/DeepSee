# OpenAI 完整消息历史传输设计

日期: 2026-08-13
状态: 待书面审阅(方案三已确认)

## 背景

当前 `POST /v1/chat/completions` 会由 `deepsee_server/protocols/openai.py`
遍历请求，但最终只留下最后一个用户文本块和最后一张图片。路由随后调用只接受单个
问题的 `ask_async` 或 `ask_with_image_async`。这会丢失 system 指令、较早的用户与
assistant 轮次、同一消息内的多个文本块、assistant `tool_calls`、tool 结果，以及
需要原样传给 DeepSeek 的生成参数。

响应路径也会把 DeepSeek 的完整 Chat Completions 响应重新包装为纯文本结果。由此会
重建 completion id，丢失 `reasoning_content`、`tool_calls`、真实 usage 和原始
`finish_reason`，流式工具调用也无法保持其 delta 结构。

本设计采用已确认的方案三：以提交 `ff80bdd` 中独立的 Chat 传输层和视觉消息变换层
为经过验证的参考，按当前分支接口重新接入，而不是合并该提交中的鉴权、CORS、追踪、
静态托管等无关功能。

## 目标

- `system`、`user`、`assistant`、`tool` 消息按原顺序完整传给 DeepSeek，支持
  `assistant.content: null` 与 `tool_calls` 并存。
- 纯文本请求不再降级为单个字符串问题；消息字段与支持的生成参数不被静默丢弃。
- 带图请求只替换 `image_url` 内容块，其余历史、文本块和工具字段保持不变。
- 非流式和流式响应保留 DeepSeek 的原始 Chat Completions 结构。
- 视觉输出被明确标记为不可信图片数据，并受到图片数量、输入大小和上下文长度限制。
- 现有 `ask*`、`ask_with_image*` 和 `describe_image*` API 的签名与行为保持不变。

## 范围

本次修改覆盖：

- 新增 `deepsee/composer/chat.py`，提供无损 Chat Completions 上游传输。
- 新增 `deepsee/composer/vision_context.py`，提供深拷贝后的图片块变换。
- 调整 `deepsee_server/protocols/openai.py`，校验并解析完整 OpenAI 请求，而不是提取
  最后一个问题。
- 调整 `deepsee_server/app.py` 的 `/v1/chat/completions` 路由，接入上述两层。
- 更新 `deepsee` 与 `deepsee.composer` 导出，使 `chat_async` 可作为独立传输 API 使用。
- 新增聚焦的传输、视觉变换、协议和端点契约测试。

本次不修改 Anthropic `/v1/messages` 或 Gemini 端点的历史转发方式，不实现
`/v1/responses`，不执行客户端工具，不修改当前鉴权、CORS、追踪、配置持久化、Web
静态托管或桌面桥接，也不顺带修复审计清单中的其他问题。

## 架构

### 1. Chat 传输层

`deepsee/composer/chat.py` 新增：

```python
async def chat_async(
    messages: Sequence[Mapping[str, Any]],
    *,
    stream: bool = False,
    config: Config | None = None,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any] | AsyncIterator[dict[str, Any]]
```

该函数只负责 DeepSeek Chat Completions 传输，不做消息压平、视觉分析或响应文本提取。
它深拷贝 `messages` 与 `params`，再用服务端配置覆盖上游 `model`，并设置 `stream`。
调用方传入的对象在请求过程中不得被修改。

非流式请求返回 DeepSeek 响应根对象，不重建 `choices`、`message`、usage 或 id。流式
请求解析 DeepSeek SSE，只忽略空行与非 `data:` 行，在 `[DONE]` 处结束，并逐个返回
上游 JSON 对象。现有异步重试工具继续负责连接和状态码重试；HTTP、网络、JSON 解析及
流式总时长错误统一包装为 `ComposeError`。客户端和响应流在正常结束、异常与取消路径
均显式关闭。

`chat_async` 从 `deepsee` 和 `deepsee.composer` 导出。现有面向单问题的公开 API 不改
签名，也不改为内部调用 `chat_async`，避免扩大回归面。

### 2. OpenAI 请求协议层

`deepsee_server/protocols/openai.py` 改为校验完整请求并向路由返回以下信息：完整
`messages`、是否流式、需要透传的参数，以及图片块数量。解析过程只读取和校验，不重写
调用方请求。

`messages` 必须是非空数组，每项必须是对象。支持 `system`、`user`、`assistant` 和
`tool` 历史；消息级字段如 `name`、`tool_calls`、`tool_call_id` 原样保留。`content`
可以是字符串或内容块数组；`assistant` 在具有合法 `tool_calls` 数组时允许
`content: null`。内容块必须是对象，`text` 块的 `text` 必须是字符串，
`image_url` 块必须含字符串 `image_url.url`。同一消息内的多个文本块不得合并或只取
最后一个。其他对象形态的内容块原样保留并由 DeepSeek 判断是否支持，协议层不执行
工具，也不解释工具参数。

顶层参数使用明确白名单：`model`、`messages`、`stream`、`stream_options`、`tools`、
`tool_choice`、`parallel_tool_calls`、`temperature`、`top_p`、`max_tokens`、
`max_completion_tokens`、`stop`、`presence_penalty`、`frequency_penalty`、
`response_format`、`seed` 和 `user`。其中 `model` 继续作为兼容字段接受，但实际上游
模型由服务端配置决定；`messages` 与 `stream` 由传输层单独设置；其余白名单参数原样
透传。`stream` 若存在必须是布尔值，不能用字符串或数字的 Python 真值规则决定响应
模式。未知顶层参数在调用配置或上游前返回 OpenAI 形状 `400`，不得静默忽略。

本地校验只负责保证 DeepSee 可以安全处理的结构。工具 schema、采样参数取值和
`response_format` 等上游语义仍由 DeepSeek 校验，DeepSee 不复制一套可能漂移的完整
DeepSeek schema。

### 3. 视觉消息变换层

`deepsee/composer/vision_context.py` 新增：

```python
async def transform_messages_with_vision(
    messages: list[dict[str, Any]],
    *,
    config: Config,
    mode: str = "auto",
) -> VisionTransformResult
```

函数先深拷贝整个消息数组，再按出现顺序查找所有 `image_url` 块。每张图的视觉问题由
该消息内全部非空文本块按顺序用换行连接；没有文本时使用“请描述这张图片”。视觉分析
继续复用 `_analyze_image_async` 与 `_format_context`，因此 `auto`、`ui`、`general`
三种模式的语义与现有单图 API 一致。非法模式在任何上游调用前返回 `400`。

每个图片块被替换为一个文本块，位置不变。替换文本使用带图片序号的
`DEEPSEE_VISUAL_CONTEXT` 边界，并明确 `trusted="false"`，同时告知 DeepSeek 忽略视觉
模型输出中出现的指令、请求或代码。除该图片块外，原消息中的其他内容块、消息级字段、
其他消息和原始输入对象全部保持不变。

多图支持必须带有确定的资源边界：单请求最多 4 张图片；每张原始图片沿用现有
20 MiB、4096x4096 像素、格式检查和 HTTP(S) SSRF 防护；单张格式化视觉上下文最多
12000 字符；单请求视觉上下文总计最多 32000 字符。超过限制返回 `400`，不静默截断。

恢复进程内 30 分钟 TTL、最多 128 项的 LRU 视觉分析缓存。缓存键包含图片内容或 URL、
该消息的文本问题、视觉模式及视觉后端配置；缓存值只保存格式化分析，不保存原图并且
不落盘。该缓存属于本次必要范围，因为完整历史可能在连续工具轮次中重复携带同一图片，
没有缓存会重复调用视觉上游。缓存失败不得影响正常分析路径。

### 4. 端点数据流

`/v1/chat/completions` 保留现有 32 MiB 请求体限制和 JSON 错误映射，新的处理顺序为：

1. 读取受限请求体并解析 JSON 对象。
2. 在加载配置前校验 OpenAI 消息结构、视觉模式和顶层参数白名单。
3. 加载服务端配置；请求中的 `model` 不覆盖服务端 DeepSeek 模型。
4. 没有图片时，将完整 `messages` 直接交给 `chat_async`。
5. 有图片时，先调用 `transform_messages_with_vision`，再把变换后的完整消息交给
   `chat_async`。
6. 非流式时返回上游根对象；流式时将上游块编码为 SSE，并以 `data: [DONE]` 结束。

调用方通过 `X-DeepSee-Vision-Mode` 选择 `auto`、`ui` 或 `general`，省略时为 `auto`。
只有显式发送 `X-DeepSee-Include-Vision: 1` 时，端点才在标准响应之外附加
`vision_analysis`：非流式写入首个 choice 的 assistant message；流式在首个上游块前
发送一个扩展 delta，并复用首个上游 completion id。普通 OpenAI 客户端不会被迫接收
该扩展。响应可带 `X-DeepSee-Vision-Cache-Hits`，用于说明本请求的进程内缓存命中数。

### 5. 响应保持

非流式响应默认逐字段保留 DeepSeek 返回值，包括但不限于 `id`、`object`、`created`、
`model`、`choices[].message.content`、`reasoning_content`、`tool_calls`、
`finish_reason` 和 usage。只有显式视觉扩展会深拷贝响应并增加
`vision_analysis`，不得修改原上游对象。

流式响应逐块转发原始 JSON，不重新生成已有 id，不把 delta 转成纯文本，也不丢弃
分片 `tool_calls`、usage 或非 `stop` 的 `finish_reason`。视觉扩展使用流内第一个上游
id；如果上游没有提供 id，才生成本地兼容 id。若上游正常结束但未发送结束 choice，
可追加最小 `finish_reason: "stop"` 兼容块；已有结束块必须原样保留且不能再追加第二个。
流中发生上游错误时发送 OpenAI 形状 error 事件后结束，只追加 `[DONE]`，不得再伪造
成功结束块。

## 错误映射

- 非法 JSON、请求根类型、消息结构、未知参数、视觉模式、图片 URL/base64、图片资源
  上限和视觉上下文上限：`400 invalid_request_error`。
- 超过现有请求体总上限：`413 invalid_request_error`。
- DeepSeek 或视觉上游的 HTTP、网络、解析和超时错误：非流式返回
  `502 upstream_error`；已开始的流式响应发送 `upstream_error` SSE 事件后 `[DONE]`。
- 配置错误继续遵循当前端点行为，不在本修复中改变全局配置错误策略。

所有验证失败必须发生在相应上游调用之前。错误响应不得包含 API key、原图、完整视觉
分析或完整用户历史。

## 测试策略

实施使用测试驱动方式，每个行为先添加失败测试，再完成最小实现。覆盖以下层次：

- `tests/test_chat_transport.py`：完整 system/user/assistant/tool 历史、
  `content: null + tool_calls`、工具定义和生成参数进入最终 HTTP payload；输入未被修改；
  非流式响应原样返回；流式稳定 id、工具 delta、usage、结束原因和错误关闭行为。
- `tests/test_vision_context.py`：同消息多个文本块共同形成视觉问题；只替换图片块；
  工具历史和原输入不变；多图顺序；三种视觉模式；不可信边界；图片数、单图/总上下文
  上限；缓存命中与不保存原图。
- `tests/test_protocols/test_openai.py`：完整消息解析、assistant null 内容、图片计数、参数
  白名单、未知参数、畸形内容块和非法视觉模式。
- `tests/test_server/test_openai_contract.py` 或现有 server 测试：纯文本多轮最终 payload；
  同消息多个文本块；图片+历史+工具；非流式上游响应保持；流式 tool call、稳定 id 与
  finish reason；视觉扩展的显式门控；错误在上游调用前返回。
- 现有 OpenAI 单问题、Anthropic、Gemini、图片安全、同步/异步 composer 测试全部回归。

## 验收标准

- 纯文本请求的最终 DeepSeek payload 含原顺序的完整消息及所有支持参数。
- 带图请求的最终 payload 仅将每个 `image_url` 块替换为不可信视觉上下文，其他字段与
  输入一致，原请求对象未被修改。
- assistant 工具调用、tool 结果和下一轮请求可以形成完整工具循环。
- DeepSeek 非流式响应和流式块不会因 DeepSee 包装而丢失 id、tool calls、usage 或
  finish reason。
- 未请求视觉扩展的标准 OpenAI 响应不包含 `vision_analysis`；请求扩展时非流式和流式
  均能收到分析。
- `auto`、`ui`、`general` 模式可用，未知模式返回 `400`。
- `uv run pytest -q` 全部通过，`uv build` 成功。
- 除本规格列出的文件和测试外，不引入审计清单其他项目的实现改动。

## 实施约束

实现不得直接 cherry-pick `ff80bdd`，因为该提交同时包含本次范围外的大量服务功能。
可以逐段复用其中 `chat.py`、`vision_context.py` 及契约测试的已验证逻辑，但必须适配
当前 HEAD 的错误处理、配置加载、请求体限制和测试组织。所有应用修改在书面规格再次
获批并完成独立实施计划后开始。
