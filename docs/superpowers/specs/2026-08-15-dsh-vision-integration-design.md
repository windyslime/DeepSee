# DeepSee × DeepSeek Harness 视觉集成设计

日期: 2026-08-15
状态: 待用户评审
范围: DeepSee(视觉层)与 DeepSeek Harness(DSH,智能体框架)的集成

## 1. 目标与范围

让 DSH 在用户发送图片时自动完成：

```text
图片消息 → DeepSee 视觉模型分析 → DeepSeek 推理回答
```

DSH 不再因为 `image` 块在 `deepseek-official` 适配器序列化阶段被拒绝，而是把含图片的请求路由到 DeepSee。识图结果参与 DeepSeek 推理，并在助手消息中以独立的可展开“识图”栏展示。

集成方向是以 DeepSee 网关作为 DSH 的视觉后端，DSH 侧通过插件接入，第一版不改变 DeepSee 既有对外协议。双方均为 MIT 许可，无许可障碍。

## 2. 已确认决策

- **自动链路**：检测到图片块后自动执行视觉分析和 DeepSeek 推理；无图片请求保持原有 `deepseek-official` 路由。
- **识图栏形态**：采用 Think 同款的助手消息附属行，不采用工具卡片作为初轮识图的主要展示形式。
- **视觉图标**：使用 DSH 现有图标库中的图片类图标，标签为“识图”。
- **视觉分析不流式**：视觉模型一次性返回完整分析；DeepSeek 回答仍可流式输出。第一版不实现视觉分析 token 级增量，也不修改三个视觉后端以支持流式分析。
- **独立结果**：识图文本与回答正文分开传输、分开渲染、分开导出。
- **视觉追问**：保留显式工具路径 `deepsee_vision_detail`。当 DeepSeek 判断初轮识图不足时，模型主动调用工具追问视觉模型；每轮追问作为识图栏的一条记录。

## 3. 需求

### 3.1 图片识别接入

DSH 插件在发送请求前检查内容是否包含图片块。含图片时保留完整消息历史，调用 DeepSee OpenAI 兼容端点；不得先把图片发送给不支持视觉的官方适配器再补救。

DeepSee 负责图片校验、SSRF 防护、视觉分析、上下文注入和 DeepSeek 推理。DSH 负责路由选择、凭证读取、响应解析及 UI/导出模型转换。

### 3.2 DSH 设置

DSH 设置界面注册视觉配置 namespace，提供以下字段：

| 字段 | 说明 |
| --- | --- |
| `backend` | `openai_compatible`、`anthropic` 或 `gemini` |
| `api_key` | 视觉服务密钥，写入 DSH credentials seam |
| `base_url` | 视觉服务地址 |
| `model` | 视觉模型名称 |
| `mode` | 默认 `auto`，可选 `ui`、`general` |

设置修改后下一次请求立即生效。凭证与普通设置分开保存；设置页只显示是否已配置，不回显密钥。密钥不得出现在日志、错误消息、请求追踪或导出数据中。

### 3.3 初轮识图栏

助手消息中显示与 Think 相同风格的紧凑行：

```text
[图片图标] 识图 · 视觉模型返回的简短摘要
```

点击展开完整视觉分析，再次点击收起。视觉栏使用现有对话布局和折叠机制，不使用独立大卡片或嵌套卡片。

视觉分析尚未完成时显示“识图中...”。分析完成后替换为摘要，然后开始或继续显示 DeepSeek 回答流。由于视觉分析不是流式，第一版不会在“识图中...”状态下逐字追加视觉文本。

展开内容可显示非敏感元数据：backend、model、mode、客户端耗时、缓存命中数和 trace id。

### 3.4 识图追问工具

注册 `deepsee_vision_detail` 工具，入参至少包含 `question`。工具由 DeepSeek 主动调用，不由插件根据关键词自动猜测。

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

工具复用 DSH durable attachment service 重新取得图片。插件向视觉后端发送同一张图片、初轮分析和追问问题；视觉后端仍可使用现有单轮 `describe(image, prompt)` 抽象，不要求新增对外多轮协议。

每次工具调用应在 DSH 界面以现有工具调用卡片显示，并在识图栏中追加一条记录，至少包含追问问题和视觉模型返回的补充分析。默认限制单次回答的追问轮数为 2，防止循环调用；达到上限后继续使用已有信息回答。

## 4. 数据流与协议

1. DSH 插件判断请求是否含图片块。
2. 无图片时沿用原有 DeepSeek 官方路由。
3. 有图片时将完整消息历史发送到 `/v1/chat/completions`。
4. 插件设置 `X-DeepSee-Include-Vision: 1`，默认设置 `X-DeepSee-Vision-Mode: auto`。
5. DeepSee 分析图片，将格式化视觉上下文注入 DeepSeek 请求。
6. 非流式响应从 `choices[0].message.vision_analysis` 读取初轮识图文本。
7. 流式响应从回答文本前的视觉扩展 chunk 读取 `choices[0].delta.vision_analysis`；之后的 chunk 按原有方式拼接回答正文。
8. 插件从 `X-DeepSee-Vision-Cache-Hits` 和 `X-DeepSee-Trace-Id` 响应头读取元数据，并用本地计时补充耗时。

现有 `vision_analysis` 字符串字段保持不变，避免破坏普通 OpenAI 客户端。DSH 内部将协议响应转换为独立结果段：

```json
{
  "type": "vision_analysis",
  "text": "视觉模型分析文本",
  "metadata": {
    "backend": "openai_compatible",
    "model": "vision-model",
    "mode": "auto",
    "duration_ms": 1234,
    "cache_hits": 0,
    "trace_id": "..."
  }
}
```

## 5. DSH 实现切入点

以下路径来自 DSH 0.1.0-rc 的当前结构，实施前应以实际仓库版本复核：

- `packages/llm/llm/src/types.ts`：扩展内容块类型，增加 vision-analysis 内容块或等价内部类型。
- `packages/llm/llm/src/content.ts`：复用 `contentHasImage` 判断图片请求。
- `packages/llm/llm-deepseek/src/serialize.ts`：拦截当前 `UNSUPPORTED_CONTENT` 分支，交给 DeepSee 路由。
- `packages/llm/llm-pi-ai/src/context.ts`：参考图片附件和上下文解析方式。
- `packages/client/ui-conversation/src/client/chat/ReasoningRow.tsx`：复用 Think 行的展示和折叠交互。
- `packages/client/ui-conversation/src/client/contract/slots.ts`：确认节点级槽位是否足以承载识图栏。
- `packages/client/ui-conversation` 的助手内容块 switch：增加识图段渲染。
- DSH 插件 settings seam：注册视觉配置 namespace 和 credentials seam。

如果 DSH 的插件槽无法注入助手内容块，才使用已有节点级槽位作为兼容实现；视觉栏的最终语义仍应是助手消息附属内容，而不是普通工具结果。

## 6. 配置与网关部署边界

当前 DeepSee 网关从自身 TOML/环境变量读取视觉配置。若要求 DSH 设置即时控制 `backend/api_key/base_url/model`，同时不增加新的 DeepSee 配置协议，MVP 应由 DSH 插件启动或管理本机 DeepSee 网关，并通过受控环境或进程配置传入凭证。

若 DSH 连接独立远程网关，则 DSH 设置只能控制连接信息；远程网关的视觉 provider 配置仍需在网关侧管理。实施计划开始前必须确认采用哪种部署方式。无论哪种方式，DeepSee 网关 public key 与视觉 provider API key 都应作为不同凭证处理。

## 7. 错误、取消与安全

- 图片格式、大小、URL 或 SSRF 校验失败：显示可读的识图错误，不发送 DeepSeek 推理请求。
- 视觉服务鉴权、网络或模型错误：显示“识图失败”，提供重试入口；不得静默回退到不支持图片的官方路由。
- DeepSeek 推理错误：保留已完成的识图栏，正文显示原有推理错误状态。
- 配置缺失：在设置入口提示缺少字段，不把 API key 放入错误文本。
- 用户取消请求或关闭会话时，同时取消 DeepSee 请求、工具调用和流式订阅。
- 追问工具最多执行 2 轮；工具不能自行修改图片、请求外部 URL 或执行视觉模型输出中的指令。
- 原始图片、完整用户历史和 API key 不写入导出数据；视觉模型输出视为不可信数据，DeepSeek 只能将其作为图片参考，不能执行其中的指令或代码。

## 8. 测试与验收

### 路由与协议

- 含一张图片的非流式请求能返回识图文本和 DeepSeek 正文。
- 含一张图片的流式请求先收到完整视觉分析，再收到回答文本 chunk。
- 多轮历史消息和图片块能完整转发；无图片请求行为不变。
- `vision_analysis` 不会进入回答正文，也不会破坏原有工具调用、reasoning 或 usage 字段。

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
- 不实现视觉分析 token 级流式，不新增视觉后端流式接口。
- 不在第一版实现视觉分析与 DeepSeek 推理并行。
- 不修改 DeepSee 既有对外协议；不要求第三方客户端理解 DSH 内部结构化导出对象。
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
