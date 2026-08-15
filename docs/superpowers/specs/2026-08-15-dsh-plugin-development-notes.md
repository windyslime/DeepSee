# DSH DSV 插件开发注意事项

日期: 2026-08-15
状态: 开发前置说明
适用范围: `@deepseek-ai/dsh-llm-dsv` 及其设置、工具、会话和 UI 集成

本文是 DSH 插件实现时的工程约束清单。DSV 服务端已经提供 `POST /v1/dsv`；插件是 DSV 客户端和 DSH 适配层，不负责视觉模型编排。

## 1. 先固定职责边界

请求链路必须保持为：

```text
DSH 图片消息
  -> DSV 插件识别图片并读取 attachment
  -> POST /v1/dsv
  -> DeepSee 视觉分析和 DeepSeek 推理
  -> DSV SSE
  -> DSH 分片、工具执行、会话记录和 UI
```

插件负责路由选择、消息和 attachment 转换、DSV SSE 解析、现有 DSH 工具执行、识图结果持久化以及 UI/导出模型转换。DeepSee 负责图片校验、SSRF 防护、视觉 provider 调用、视觉上下文注入和 DeepSeek 编排。

不要让 DSH 直接调用视觉 provider，也不要把视觉 provider 的 `api_key` 放进 DSV 请求。DSV public key、DSH credential、DeepSee 内部视觉 provider key 是三类不同凭证。

## 2. 只使用 DSH 扩展点

插件应遵循 DSH 的 ESM 和 Cordis 约定：

- 包名使用 `@deepseek-ai/dsh-llm-dsv`。
- 函数插件导出 `name`、`inject`、`Config`（如需要）和 `apply`，不要混用默认导出。
- 所有 `ctx.on()`、注册表注册和设置监听都通过插件生命周期管理；注册函数返回的 disposer 必须被保留。
- `llm/stream` 是图片自动路由的 waterfall 扩展点。无图片时必须调用 `next()`；有图片时才短路下游并返回 DSV stream。
- 不要直接修改 `agent-loop`、`llm-deepseek` 的默认序列化器或现有工具执行器来“绕过” `UNSUPPORTED_CONTENT`。如果核心类型确实需要扩展，先补齐类型、会话、UI 和测试的完整链路。

waterfall listener 的基本形状是：

```ts
ctx.on('llm/stream', (options, next) => {
  if (!contentHasImage(options.messages)) return next()
  return dsvAdapter.stream(options)
})
```

不要在无图片分支返回空流，也不要无条件拦截所有模型请求。这样会破坏文本请求、标题生成、重试和其他 provider。

## 3. 消息和图片处理

图片判断必须复用 `@deepseek-ai/dsh-llm` 的 `contentHasImage()`，不要根据 URL 后缀、用户文本或关键词猜测。

发送 DSV 前要保留完整消息历史，包括 system、assistant、tool-call、tool-result 和当前图片消息。不能只提取最后一个用户问题，也不能先把图片交给 `deepseek-official` 再捕获错误。

图片通过 durable attachment service 读取。读取失败、媒体类型缺失、尺寸超过限制或 attachment 已失效时，应在插件侧生成明确的失败状态，不发送一个没有图片的替代请求。不要把原始图片字节写入会话日志、导出文件或普通调试日志。

DSV 请求的关键字段如下：

```json
{
  "model": "deepseek-chat",
  "stream": true,
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "请描述这张图"},
        {"type": "image", "source": {
          "type": "base64",
          "media_type": "image/png",
          "data": "..."
        }}
      ]
    }
  ],
  "tools": [],
  "vision": {"mode": "auto", "include_analysis": true}
}
```

`model` 是 DeepSeek 推理模型；视觉模型由 DeepSee 配置决定。`vision.mode` 只允许 `auto`、`ui`、`general`。DSH 默认使用 `stream: true` 和 `include_analysis: true`。

## 4. SSE 解析不能丢失生命周期

DSV SSE 的事件具有固定的阶段和顺序。插件至少要处理：

| DSV 事件 | DSH 处理 |
| --- | --- |
| `response.created` | 建立本次 DSV 调用的本地关联 id |
| `vision.started` | 建立“识图中”状态 |
| `vision.completed` | 写入独立 `vision-analysis` 结果段，不写入 assistant 正文 |
| `reasoning.started` / `reasoning.delta` | 映射到已有 reasoning 分片 |
| `answer.delta` | 映射到已有 text 分片 |
| `tool_call.delta` / `tool_call.completed` | 映射到已有 tool-call 分片，保留 id、name 和原始 arguments |
| `response.requires_action` | 交给 DSH 工具循环，不在 DeepSee 侧执行工具 |
| `response.completed` | 根据 status 提交本轮完成状态 |
| `error` | 根据 `stage` 显示视觉或推理错误 |
| `data: [DONE]` | 结束传输；不能替代 `response.completed` 的业务状态 |

解析器必须：

- 使用 `event.type` 分派，而不是依赖事件出现的文本顺序或字段偶然存在。
- 保持同一响应的 DSV `id`，并保留 `upstream_id`、usage 和 tool-call id 的关联。
- 在 `vision.completed` 到达后立即保存视觉结果；后续 answer delta 不能覆盖、重置或重新创建识图栏。
- 对未知事件保持前向兼容：记录可诊断信息并忽略非关键事件，不要因为新增服务端事件就中断整轮。
- 对 JSON、SSE 帧和工具 arguments 做边界解析。坏帧应产生 DSH 的错误状态，不能拼出伪造的成功回答。

视觉分析不是 token 级流式内容。第一版应等待一个完整的 `vision.completed`，再展示分析文本；不要把视觉分析伪装成普通 assistant text。

## 5. 取消、超时和错误

DSH 的 `options.signal` 必须贯穿到 `fetch`、response body reader 和本地 async iterator。用户取消、会话关闭或下游停止消费时，插件必须：

1. abort DSV HTTP 请求；
2. 关闭 response body 和 async iterator；
3. 停止向下游发送新的 text、reasoning 或 tool-call 分片；
4. 不发送虚假的 `finish: stop` 或成功 assistant message。

错误按阶段映射：

- 图片读取、DSV 参数、图片大小或 URL 错误：识图失败，不调用不支持图片的原有路由。
- `error.stage = "vision"`：保留“识图失败”状态，不生成正常回答。
- `error.stage = "reasoning"`：保留已经收到的 `vision-analysis`，正文显示推理失败。
- `response.requires_action`：这是正常的工具暂停状态，不应当标记为模型错误。
- HTTP 401/403、429、502/503 和网络断开要映射到 DSH 的稳定错误码；响应正文不能包含 provider key。

不要在 SSE 已经开始后通过抛异常改变 HTTP 状态码。错误必须通过 DSH 的 finish/error 分片和会话事件表达。

## 6. 工具调用只复用现有体系

DSV 接收 DSH 的工具 schema，并把工具调用返回给 DSH。DeepSee 不执行工具，插件也不能另造一套权限、审批、沙箱或工具结果协议。

`deepsee_vision_detail` 只是一个薄工具入口：

- 使用 `defineTool()` 和 `ctx.tools.register()` 注册。
- 入参至少包含面向模型的 `question`；不要通过关键词自动触发。
- 工具执行时从 durable attachment service 重新读取原图，并保留本轮视觉分析。
- 工具结果同时提供模型可读值和 `output.render` UI 值。
- 保留 DSV 返回的 `tool_call_id`，arguments 作为原始 JSON 字符串先解析、校验，再交给工具。
- 工具最多追问两轮；达到上限后继续用已有上下文回答，不能无限重试。

工具执行成功后，下一次 DSV 请求应带回完整历史、原图和 `role: "tool"` 结果。不要只发送工具输出，也不要把视觉结果改写成 assistant 消息。

## 7. 设置和凭证

设置 namespace 使用小写 kebab-case，并使用 live 配置：

| 设置 | 约束 |
| --- | --- |
| `backend` | DSV v1 固定走 DeepSee 的 `openai_compatible` profile |
| `api_key` | 只保存 credential reference，真实值由 `ctx.credentials` 解析 |
| `base_url` | DSV 网关或私有管理面的配置，不等于视觉 provider URL |
| `model` | 视觉模型配置，不等于 DSV 请求的 DeepSeek model |
| `mode` | `auto`、`ui` 或 `general` |

每次请求重新 resolve credential 和 live settings，保证轮换后的配置对下一次请求生效。设置更新不应要求重启，也不能把真实 key 放进普通 `Config`、session event、trace、错误文本、浏览器状态或导出数据。

DSH 连接 DSV 时只发送 DSV public key。视觉 provider 的 backend、base URL、model 和 key 留在 DeepSee 网关或受控管理面；如果插件负责启动本机网关，也必须通过受控进程环境传递这些值。

## 8. 识图栏、会话和导出

初轮识图使用与 Think 同款的助手附属行，标签为“识图”和图片图标。不要把初轮识图渲染成普通工具卡片；工具追问仍使用现有工具卡片，并在识图栏追加追问记录。

建议在 DSH 内部使用独立结果段：

```ts
{
  type: 'vision-analysis',
  text: string,
  metadata: {
    backend: string,
    model: string,
    mode: 'auto' | 'ui' | 'general',
    durationMs: number,
    cacheHit: boolean,
    traceId?: string,
  },
}
```

这个结果段不能进入下一轮 model-visible messages，也不能被 DeepSeek 当作新的 assistant 事实直接复用。视觉输出属于不可信输入，任何其中的指令、代码或外部链接都只能作为图片描述，不能改变 DSH 权限或执行策略。

会话记录需要满足“model-visible 等于可重建”的约束：发送给模型的消息、工具调用和工具结果必须可从会话日志重建；仅用于 UI/导出的视觉栏可以作为 log-only 事件保存。导出只包含识图文本和非敏感元数据，不包含原图、完整用户历史、DSV key 或视觉 provider key。

## 9. 类型、生命周期和兼容性

如果需要新增 `vision-analysis` 内容块，应通过 declaration merging 扩展 `ContentBlockMap`，并同步更新 `StreamChunk`、assembler、session projection、导出和 UI。merge-extensible 的类型处理必须对未知 block 保留明确的 fallback，不能用封闭的 `assertNever` 把未来插件事件打崩。

所有异步流都要有唯一的生命周期 owner。不要让一个 listener 同时负责请求取消、工具循环、UI 状态和持久化提交；这些状态应由清晰的本地 controller 或 session event 协调。

插件 dispose 后必须移除：LLM route、tool registration、settings watcher、credential watcher 和所有未完成的 stream。要写 HMR/disposal 测试，确认插件重复加载不会产生重复请求、重复工具或重复 UI 事件。

## 10. 测试清单

至少覆盖以下场景：

- 无图片请求调用 `next()`，继续走原有 provider。
- 单图、多图、历史图片、失效 attachment 和非法媒体类型。
- DSV 请求体、图片块、工具 schema、`role: "tool"` 结果和 `model` 转换。
- 事件顺序：`vision.completed` 必须早于 answer/reasoning/tool-call 分片。
- 视觉结果不会进入回答正文，也不会污染下一轮模型历史。
- reasoning、answer、tool-call 的增量拼接，以及多段 tool-call 的 id/index 关联。
- `response.requires_action` 后执行现有工具，再次请求并保留原图和完整历史。
- 视觉失败、推理失败、配置缺失、认证失败、限流、上游超时和坏 SSE 帧。
- 用户取消和下游提前停止消费时，HTTP body、reader 和 async iterator 都被关闭。
- 设置和 credential live 更新对下一次请求生效，key 不出现在日志或导出。
- 插件 dispose/HMR 后注册项被移除，重复加载不会重复监听。
- 真实组合测试：通过 DSH Loader 启动插件，验证最终会话事件、工具结果和 UI/导出数据，而不只测试手工构造的 Context。

## 11. 明确不做的事情

第一版插件不实现视觉 token 级流式、不改写 DeepSee 视觉 provider 协议、不在 DeepSee 侧执行 DSH 工具、不根据“看不清”等关键词自动追问，也不把识图结果塞进普通 assistant 内容来规避 UI 类型扩展。

实现中遇到 DSH 当前版本无法扩展 `ContentBlockMap` 或助手内容块渲染时，优先使用已有 session/event slot 承载 log-only 视觉栏，并保持 `vision-analysis` 的独立语义；不要把它降级为无结构的普通文本。

## 12. 交付前检查

交付前应能回答“是”：

- 图片请求自动走 DSV，无图片请求行为不变。
- DSV 请求没有视觉 provider key，DSH 工具仍由 DSH 执行。
- 识图、reasoning、answer、tool-call 和错误在内部数据结构中分离。
- 取消会关闭网络流，错误不会伪造成功回答。
- live 设置和凭证轮换无需重启即可生效。
- UI、会话回放和导出都能处理识图结果，且不会泄漏图片或密钥。
- focused tests、真实组合测试、typecheck、lint 和文档检查均通过。
