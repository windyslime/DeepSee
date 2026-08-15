# DeepSee × DeepSeek Harness 视觉集成设计

日期: 2026-08-15
状态: 已批准并实现；DeepSee 服务端 DSV 垂直切片与 `@deepseek-ai/dsh-llm-dsv` 插件均已落地
范围: DeepSee(视觉层)与 DeepSeek Harness(DSH,智能体框架)的集成

## 1. 目标与范围

让 DSH 在用户发送图片时自动完成：

```text
图片消息 → DeepSee 视觉模型分析 → DeepSeek 推理回答
```

DSH 不再因为 `image` 块在 `deepseek-official` 适配器序列化阶段被拒绝，而是把含图片的请求路由到 DeepSee。识图结果参与 DeepSeek 推理，并在助手消息中以独立的可展开“识图”栏展示。

DSV 是 DeepSee 对外提供的公开视觉编排/输出协议，固定入口为 `POST /v1/dsv`；它不是另一个视觉模型后端，也不是 DSH 直接调用视觉模型的通道。DSH 侧通过插件消费 DSV，DeepSee 在协议内部调用视觉 API（统一采用 OpenAI-compatible 请求形状），再编排 DeepSeek 推理并输出 SSE 事件。现有 OpenAI、Anthropic 和 Gemini 形状端点继续保留为兼容入口。双方均为 MIT 许可，无许可障碍。

## 2. 已确认决策

- **自动链路**：检测到图片块后自动执行视觉分析和 DeepSeek 推理；无图片请求保持原有 `deepseek-official` 路由。
- **识图栏形态**：采用 Think 同款的助手消息附属行，不采用工具卡片作为初轮识图的主要展示形式。
- **视觉图标**：使用 DSH 现有图标库中的图片类图标，标签为“识图”。
- **视觉分析不流式**：视觉模型一次性返回完整分析；DeepSeek 回答仍可流式输出。第一版不实现视觉分析 token 级增量，也不为 DSV 增加视觉 provider 流式接口；现有兼容端点按原实现保留。
- **独立结果**：识图文本与回答正文分开传输、分开渲染、分开导出。
- **视觉追问**：保留显式工具路径 `deepsee_vision_detail`。当 DeepSeek 判断初轮识图不足时，模型主动调用工具追问视觉模型；每轮追问作为识图栏的一条记录。
- **公开协议**：DSV v1 是 DeepSee 的公开编排/输出协议，数据入口固定为 `POST /v1/dsv`，支持 JSON 非流式和 SSE 流式；DSH 将 DSV 作为图片请求的主 LLM 路由。
- **内部视觉协议**：DeepSee 调用视觉 provider 时使用 OpenAI-compatible 协议；该调用发生在 DeepSee 内部，DSH 不直接连接视觉 provider，也不把 provider 凭证放进 DSV 请求。
- **工具兼容**：DSV 接收 DSH 的工具 schema 并返回工具调用事件，但不执行 DSH 工具；工具仍由 DSH 的 `ctx.tools`、权限、沙箱和结果回写管线负责。

## 3. 需求

### 3.1 图片识别接入

DSH 插件在发送请求前检查内容是否包含图片块。含图片时保留完整消息历史，调用 DeepSee 的 DSV 端点；不得先把图片发送给不支持视觉的官方适配器再补救。无图片时继续调用原有 `deepseek-official` 路由。

DeepSee 负责图片校验、SSRF 防护、调用 OpenAI-compatible 视觉 API、视觉分析、上下文注入和 DeepSeek 推理编排。DSH 负责路由选择、DSV 凭证读取、图片 attachment 转换、DSV SSE 解析、现有工具执行以及 UI/导出模型转换；DSH 不负责视觉编排。

### 3.2 DSH 设置

插件通过 `ctx.settings` 注册一个小写 kebab-case 的视觉配置 namespace。配置 schema 负责 UI 表单生成和字段校验；这不是把密钥写进插件的普通 `Config` 或 `cordis.yml`。

设置界面提供以下用户字段（实现内部可使用 camelCase 名称）：

| 字段 | 说明 |
| --- | --- |
| `backend` | DeepSee 内部视觉 provider profile；DSV v1 使用 `openai_compatible`，现有 `anthropic`/`gemini` 选项仅作为 DeepSee 兼容端点的保留配置，不改变 DSV 的内部调用契约 |
| `api_key` | 视觉服务密钥；设置层只保存 credential reference，真实值由 `ctx.credentials` 保存 |
| `base_url` | DeepSee 调用 OpenAI-compatible 视觉 API 的地址 |
| `model` | 视觉模型名称（与 DSV 请求中的 DeepSeek `model` 分开） |
| `mode` | 默认 `auto`，可选 `ui`、`general` |

namespace 使用 `applies: 'live'`，设置更新通过 scope 的 `watch()` 进入下一次请求；配置变更不需要重启。凭证按每次请求重新 `resolve()`，轮换后的密钥对紧随其后的请求生效。设置描述和凭证描述必须使用脱敏模式，设置页只显示是否已配置，不回显密钥。密钥不得出现在日志、错误消息、请求追踪或导出数据中。

上述字段配置的是 DeepSee 内部视觉 provider，不是 DSV 公共请求字段。DSV 网关地址、DSV public/admin key 与视觉 provider API key 是不同的配置和凭证。推理请求只携带 DSV public key；视觉 provider API key 通过本地网关配置或独立的管理面同步，不进入 `POST /v1/dsv` 请求体。

### 3.3 初轮识图栏

助手消息中显示与 Think 相同风格的紧凑行：

```text
[图片图标] 识图 · 视觉模型返回的简短摘要
```

点击展开完整视觉分析，再次点击收起。视觉栏使用现有对话布局和折叠机制，不使用独立大卡片或嵌套卡片。

视觉分析尚未完成时显示“识图中...”。分析完成后替换为摘要，然后开始或继续显示 DeepSeek 回答流。由于视觉分析不是流式，第一版不会在“识图中...”状态下逐字追加视觉文本。

展开内容可显示非敏感元数据：backend、model、mode、客户端耗时、缓存命中数和 trace id。

### 3.4 识图追问工具

插件以标准 Cordis 模块导出 `name`、`inject` 和 `apply(ctx)`，并通过 `@deepseek-ai/dsh-tools` 的 `defineTool()` 与 `ctx.tools.register()` 注册 `deepsee_vision_detail`。这只是新增一个薄工具入口，不改写 DSH 已有工具 schema、权限或执行器。工具入参至少包含 `question`，由 DeepSeek 主动调用，不由插件根据关键词自动猜测。工具结果同时提供面向模型的规范值和面向 UI 的 `output.render` 内容。

追问流程：

```text
传图 → 初轮视觉分析 → DeepSeek 作答
                         ↓ 不确定
              deepsee_vision_detail(question)
                         ↓
              图片 + 初轮分析 + 追问问题
                         ↓
                 视觉模型补充分析
                         ↓
                 DeepSeek 继续作答
```

工具复用 DSH durable attachment service 重新取得图片。工具执行体调用 DSV 的视觉追问能力，向视觉后端发送同一张图片、初轮分析和追问问题；视觉后端仍可使用现有单轮 `describe(image, prompt)` 抽象，不要求为工具新增模型侧多轮协议。DSV 不执行 `deepsee_vision_detail`，只承载这次工具调用产生的后续模型请求。

每次工具调用应在 DSH 界面以现有工具调用卡片显示，并在识图栏中追加一条记录，至少包含追问问题和视觉模型返回的补充分析。默认限制单次回答的追问轮数为 2，防止循环调用；达到上限后继续使用已有信息回答。

## 4. 数据流与 DSV 公共协议

### 4.1 请求

1. DSH 插件判断请求是否含图片块。
2. 无图片时调用原有 `deepseek-official` 路由；有图片时由 `llm/stream` waterfall 将请求交给 DSV adapter。
3. adapter 从 `ctx.attachments.readImage()` 读取图片字节，保留完整消息历史，并把 DSH 的 `ToolSchema` 转换为 DSV `tools`。
4. adapter 向 `POST /v1/dsv` 发送已认证请求，默认启用 `stream: true` 和 `vision.include_analysis: true`。请求中的 `model` 是 DeepSeek 推理模型，不是视觉模型；视觉模型由 DeepSee 内部配置选择：

```json
{
  "model": "deepseek-chat",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "这张图里有什么？"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
      ]
    }
  ],
  "tools": [],
  "vision": {"mode": "auto", "include_analysis": true},
  "stream": true
}
```

DSV 请求不得携带视觉 provider API key。DSV 只接收已认证的推理请求和模型可见的工具 schema。

DSV 请求可以使用 OpenAI-compatible 的 `messages` 形状承载 DSH 工具结果。例如 DSH 执行工具后，将 `role: "tool"`、`tool_call_id` 和工具输出作为下一次 `/v1/dsv` 请求的一部分提交；DSV 负责把该结果继续交给 DeepSeek 推理，但仍不执行工具本身。

### 4.2 响应

非流式 DSV 响应必须把视觉结果、回答、工具调用和 usage 分开：

```json
{
  "id": "dsv_123",
  "object": "dsv.response",
  "status": "completed",
  "vision": {
    "analysis": "视觉模型分析文本",
    "mode": "auto",
    "backend": "openai_compatible",
    "model": "vision-model",
    "latency_ms": 1234,
    "cache_hit": false,
    "trace_id": "..."
  },
  "answer": {"text": "DeepSeek 的最终回答"},
  "usage": {}
}
```

流式 DSV 使用 SSE，每个 `data` 帧是一个带 `type` 的 JSON 对象。含图片请求的顺序保证为：

```text
response.created
→ vision.started
→ vision.completed
→ reasoning.started / reasoning.delta*
→ answer.delta* 或 tool_call.delta*
→ tool_call.completed（如有工具调用）
→ answer.completed 或 response.requires_action
→ response.completed
```

`vision.completed` 一次性携带完整视觉分析；DSH adapter 将它转换为独立的 `vision-analysis` 分片。`answer.delta`、`reasoning.delta`、`tool_call.delta` 分别转换为现有的文本、reasoning 和 tool-call 分片。工具调用结束时 DSV 返回 `response.requires_action` 及完整调用参数，DSH 执行现有工具后，把 `tool-result` 作为下一次 `/v1/dsv` 请求的一部分继续推理。DSV 不会因为收到工具 schema 就在 DeepSee 侧执行工具。

DeepSee 内部已经把视觉分析注入本轮 DeepSeek 上下文；DSH 不把 `vision.completed` 伪装成 assistant message，也不把它追加到下一轮 DSH 的模型可见历史。DSH 内部将它保存为独立结果段：

```json
{
  "type": "vision-analysis",
  "text": "视觉模型分析文本",
  "metadata": {
    "backend": "openai_compatible",
    "model": "vision-model",
    "mode": "auto",
    "duration_ms": 1234,
    "cache_hit": false,
    "trace_id": "..."
  }
}
```

## 5. DSH 实现切入点

插件实现遵循 DSH 基础开发契约：TypeScript 模块导出 `apply(ctx)`；需要服务时声明 `inject`；通过 Cordis waterfall 事件路由请求；通过标准 LLM 和工具服务注册能力。插件包建议命名为 `@deepseek-ai/dsh-llm-dsv`。插件是 DSV 客户端/适配器，不包含视觉模型编排逻辑。

以下路径与机制已经在 DSH `feat/dsh-llm-dsv` 分支实现：

- `packages/llm/llm-dsv/src/index.ts`：导出 `name`、`inject` 和 `apply(ctx, config)`，挂载图片路由与追问工具；依赖 `llm`、`tools` 和 `attachments`。
- `packages/llm/llm-dsv/src/types.ts`：通过 declaration merging 扩展 `@deepseek-ai/dsh-llm` 的 `ContentBlockMap`，把 `vision-analysis` 定义为助手消息中的独立、仅展示内容块。
- `packages/llm/llm-dsv/src/client.ts`、`serialize.ts`、`sse.ts` 和 `translate.ts`：负责请求序列化、DSV fetch 与 idle watchdog、SSE 帧解析，以及按 `event.type` 翻译为 Harness `StreamChunk`。
- `ctx.on('llm/stream', (options, next) => ...)`：使用 waterfall 事件检测 `contentHasImage(options.messages)`；无图片必须调用 `next()`，有图片时短路下游并调用 DSV adapter。
- `packages/core/session/src/surface.ts`：在唯一的 `deriveEventMessage` 投影点递归过滤 `vision-analysis`，包括嵌套工具结果中的内容块；各 provider 不再维护自己的过滤逻辑。
- `packages/client/runtime/src/client/sessions/conversation.ts`：把助手消息中的 `vision-analysis` 分类为客户端可识别的独立块，同时保留非敏感元数据。
- `packages/client/ui-conversation/src/client/chat/VisionAnalysisRow.tsx`：使用 Think 同款 disclosure 交互、图片图标和本地化文本渲染初轮与追问识图结果。
- `packages/llm/llm/src/content.ts`：复用 `contentHasImage` 判断图片请求。
- `packages/llm/llm-deepseek/src/serialize.ts`：作为现有 `UNSUPPORTED_CONTENT` 行为和消息形状的参考；第一版不直接修改该包的序列化逻辑。
- `packages/llm/llm-pi-ai/src/context.ts`：参考图片附件和上下文解析方式。
- `@deepseek-ai/dsh-settings`：参考 `settingsNamespace`、schema 注册、`installSettingsSection`/scope `watch` 和 live 更新。
- `@deepseek-ai/dsh-credentials`：使用 credential reference 和按请求解析，避免把 API key 作为设置值传输。
- `@deepseek-ai/dsh-tools`：使用 `defineTool`、`output.schema`、`output.render` 和 `ctx.tools.register` 注册追问工具。
- `packages/client/ui-conversation/src/client/chat/ReasoningRow.tsx`：复用 Think 行的展示和折叠交互。
- `packages/client/ui-conversation/src/client/contract/slots.ts`：确认节点级槽位是否足以承载识图栏。
- `packages/client/ui-conversation` 的助手内容块 switch：增加识图段渲染。

`llm/stream` waterfall 是自动路由扩展点，因此无需覆盖 `deepseek-official` 适配器注册，也无需在核心序列化器中加入 DeepSee 特判。DSV adapter 只在含图片的普通会话请求中接管流；无图请求和带 `purpose` 的辅助请求继续 `next()`。识图结果已经作为 declaration-merged 助手内容块进入持久化、回放和导出，在统一 surface 投影处从后续模型历史删除，并由客户端节点分类与折叠行渲染；它不是独立的 session 事件，也不是普通工具结果。

## 6. 配置与网关部署边界

DSV 的公开数据面固定为 `POST /v1/dsv`，只承载图片、消息、DeepSeek 模型和 DSH 工具 schema/result。视觉 provider 配置不进入该请求体，必须通过 DSV 网关本地配置或单独的私有管理面提供。若要求 DSH 设置即时控制 `backend/api_key/base_url/model`，MVP 可以由 DSH 插件启动或管理本机 DeepSee 网关，并通过受控环境或进程配置传入凭证；也可以由远程 DSV 网关提供经 admin key 保护的配置同步接口，但该接口不属于公开 DSV 数据协议。

若 DSH 连接独立远程网关，则 DSH 设置通过私有管理面同步视觉 provider 配置，或明确把 provider 配置留在网关侧；不能把它们伪装成普通推理参数。实施计划开始前必须确认采用哪种部署方式。无论哪种方式，DeepSee 网关 public key、admin key 与视觉 provider API key 都应作为不同凭证处理。

## 7. 错误、取消与安全

- 图片格式、大小、URL 或 SSRF 校验失败：显示可读的识图错误，不发送 DeepSeek 推理请求。
- 视觉服务鉴权、网络或模型错误：显示“识图失败”，提供重试入口；不得静默回退到不支持图片的官方路由。
- DSV SSE 中的 `error` 事件按 `stage` 映射为视觉或推理错误；流已开始后不伪造成功的 assistant message。
- DeepSeek 推理错误：保留已完成的识图栏，正文显示原有推理错误状态。
- 配置缺失：在设置入口提示缺少字段，不把 API key 放入错误文本。
- 用户取消请求或关闭会话时，同时取消 DeepSee 请求、工具调用和流式订阅。
- 追问工具最多执行 2 轮；工具不能自行修改图片、请求外部 URL 或执行视觉模型输出中的指令。
- 原始图片、完整用户历史和 API key 不写入导出数据；视觉模型输出视为不可信数据，DeepSeek 只能将其作为图片参考，不能执行其中的指令或代码。

## 8. 测试与验收

### 路由与协议

- `POST /v1/dsv` 的含图片非流式请求能返回独立的视觉结果和 DeepSeek 正文。
- `POST /v1/dsv` 的含图片 SSE 请求先收到完整 `vision.completed`，再收到回答文本或工具调用事件。
- 多轮历史消息和图片块能完整转发；无图片请求行为不变。
- `vision-analysis` 不会进入回答正文或下一轮模型历史，也不会破坏原有工具调用、reasoning 或 usage 字段。

### UI 与导出

- 识图栏使用图片图标和“识图”标签，能够展开和收起。
- 正文流式更新不会覆盖、挤压或重置识图内容。
- 识图文本、追问记录和回答正文在内部数据结构中分离。
- backend、model、mode、耗时、缓存命中和 trace id 可展示；密钥不会出现在 UI、日志或导出数据中。

### 追问工具

- DeepSeek 可主动调用 `deepsee_vision_detail`，工具能复用 durable attachment 取得原图。
- 工具结果进入回答上下文，并在识图栏追加对应轮次。
- 追问达到上限后不会继续循环。

### 异常与安全

- 视觉配置缺失、视觉上游失败、图片校验失败、请求取消和 DeepSeek 错误均有明确状态。
- 不支持的图片不会被重新发送到 `deepseek-official`。
- SSRF、图片大小、请求体大小和鉴权限制继续由 DeepSee 网关统一执行。

## 9. 非目标

- 不把初轮识图设计成 agent 按需调用的工具。
- 不让 DSH 直接调用视觉 provider；DSH 只调用 DeepSee 的 DSV 公共接口。
- 不实现视觉分析 token 级流式，不新增视觉后端流式接口。
- 不在第一版实现视觉分析与 DeepSeek 推理并行。
- 不删除或破坏 DeepSee 既有兼容端点；不要求第三方兼容客户端理解 DSH 内部结构化导出对象。
- 暂不实现 DeepSee 网关内部基于“不确定/看不清”等关键词的自动追问；追问只走 DSH 显式工具路径。
- 不进行与图片路由、凭证、安全或识图 UI 无关的 DSH 重构。

## 10. 参考文件

DeepSee 侧：

- `deepsee_server/app.py`
- `deepsee_server/protocols/openai.py`
- `deepsee/backends/base.py`
- `deepsee/composer/vision_context.py`
- `deepsee/composer/deepseek.py`

DSH 侧：

- `packages/llm/llm/src/types.ts`
- `packages/llm/llm/src/content.ts`
- `packages/llm/llm-deepseek/src/serialize.ts`
- `packages/llm/llm-pi-ai/src/context.ts`
- `packages/client/ui-conversation/src/client/chat/ReasoningRow.tsx`
- `packages/client/ui-conversation/src/client/contract/slots.ts`

官方开发参考：

- [第一个插件](https://deepseek-harness.github.io/deepseek-harness/develop/basic/)
- [插件配置](https://deepseek-harness.github.io/deepseek-harness/develop/basic/config/)
- [开发一个工具](https://deepseek-harness.github.io/deepseek-harness/develop/basic/tool/)
- [LLM 适配器](https://deepseek-harness.github.io/deepseek-harness/develop/practice/llm-adapter)
- [用户设置](https://deepseek-harness.github.io/deepseek-harness/reference/subsystems/settings)
- [用户凭据](https://deepseek-harness.github.io/deepseek-harness/reference/subsystems/credentials)
