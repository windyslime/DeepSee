# DSH 接入 DeepSee 视觉能力设计

日期: 2026-08-15
状态: 待用户评审

## 1. 目标

让 DeepSeek Harness (DSH) 在用户发送图片时，自动完成：

```text
图片消息 → DeepSee 视觉模型分析 → DeepSeek 推理回答
```

用户不需要手动调用视觉工具，也不需要修改 DeepSee 配置文件。DSH 中的助手消息显示一个与 Think 样式一致的可展开“识图”栏，展开后查看视觉模型的完整分析结果。

## 2. 已确认决策

- 识图链路全程自动。只要请求包含图片块，就由 DSH 插件路由到 DeepSee；无图片请求保持原有 `deepseek-official` 路由。
- 识图栏采用 Think 同款的助手消息附属行，而不是工具卡片。
- 图标使用 DSH 现有图标库中的图片类图标，标签使用“识图”。
- 识图分析一次性返回，不做视觉分析 token 级流式。DeepSee 回答仍然可以正常流式输出。
- 识图结果文本与回答正文分开传输、分开渲染。
- 第一版不修改 DeepSee 既有对外协议；复用 `vision_analysis` 扩展字段和现有响应头。

## 3. 用户体验

助手消息中，在回答正文附近显示：

```text
[图片图标] 识图 · 视觉模型返回的简短摘要
```

点击该行后展开完整视觉分析；再次点击收起。栏的字体、间距、折叠交互和 Think 保持一致，不使用独立大卡片或嵌套卡片。

视觉分析尚未完成时显示“识图中...”。视觉分析完成后替换为摘要，随后开始或继续显示 DeepSeek 的回答流。由于视觉分析不是流式，第一版不会在“识图中...”状态下逐字追加视觉文本。

识图栏可在展开内容中显示以下非敏感元数据：backend、model、mode、客户端耗时、缓存命中数和 trace id。API key、完整请求头和原始图片不得显示或写入日志。

## 4. 数据流

1. DSH 插件检查用户消息是否包含图片块。
2. 无图片时，沿用 DSH 原有 DeepSeek 官方路由。
3. 有图片时，插件保留完整消息历史，并将请求发送到 DeepSee OpenAI 兼容端点 `/v1/chat/completions`。
4. 插件设置 `X-DeepSee-Include-Vision: 1`，视觉模式默认使用 `X-DeepSee-Vision-Mode: auto`。
5. DeepSee 对图片执行视觉分析，把分析结果注入 DeepSeek 请求上下文。
6. 非流式响应从 `choices[0].message.vision_analysis` 读取识图文本。
7. 流式响应从回答文本开始前的首个视觉扩展 chunk 读取 `choices[0].delta.vision_analysis`，之后的 chunk 按原有方式拼接回答正文。
8. 插件用 `X-DeepSee-Vision-Cache-Hits` 和 `X-DeepSee-Trace-Id` 响应头补充元数据；耗时由插件本地计时。

插件不得把图片请求先发送给不支持图片的 `deepseek-official` 路由，再尝试补救；视觉路由应在发送上游请求前确定。

## 5. DSH 设置与凭证

DSH 设置界面提供视觉服务配置：

| 字段 | 说明 |
| --- | --- |
| `backend` | `openai_compatible`、`anthropic` 或 `gemini` |
| `api_key` | 视觉服务密钥，写入 DSH credentials seam |
| `base_url` | 视觉服务地址 |
| `model` | 视觉模型名称 |
| `mode` | 默认 `auto`，可选 `ui`、`general` |

凭证与普通设置分开保存；读取设置时只返回是否已配置，不回显密钥。日志、错误消息和导出数据均不得包含密钥。

### 网关部署边界

当前 DeepSee 网关从自身的 TOML/环境变量读取视觉配置。若要求 DSH 设置即时控制上述四个视觉字段，同时不增加新的 DeepSee 配置协议，MVP 应由 DSH 插件启动或管理本机 DeepSee 网关，并将凭证通过受控环境或进程配置传入。

若连接的是独立远程网关，则 DSH 设置只能控制连接信息；远程网关的视觉 provider 配置仍需在网关侧管理。这一部署方式必须在实施计划开始前确认。

## 6. 错误和降级

- 图片格式、大小、URL 或 SSRF 校验失败：显示可读的识图错误，不发送 DeepSeek 推理请求。
- 视觉服务鉴权、网络或模型错误：显示“识图失败”，保留重试入口；不得静默回退到不支持图片的官方路由。
- DeepSeek 推理错误：识图栏仍可保留，正文显示原有推理错误状态。
- DSH 未开启或未完成视觉配置：在设置入口提示缺少字段，不把 API key 放入错误文本。
- 用户取消请求或关闭会话时，同时取消 DeepSee 请求并清理流式订阅。

## 7. 导出格式

DSH 内部将识图结果建模为独立段，不把它拼入回答正文：

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

现有 DeepSee 协议中的 `vision_analysis` 字符串保持不变；上述对象是 DSH 内部导出/渲染模型，避免破坏普通 OpenAI 客户端兼容性。

## 8. 测试与验收

- 含一张图片的非流式请求能显示识图栏和 DeepSeek 正文。
- 含一张图片的流式请求先收到完整视觉分析，再收到回答文本 chunk。
- 多轮历史消息和图片块能完整转发，原有文本请求行为不变。
- 识图栏可展开、收起，正文流式更新不会覆盖或挤压识图内容。
- backend、model、mode、耗时、缓存命中和 trace id 可展示；密钥不会出现在 UI、日志或导出数据中。
- 视觉配置缺失、视觉上游失败、图片校验失败和用户取消均有明确状态。
- 未配置视觉能力时，普通无图请求仍走原有 DeepSeek 路由。
- 不实现视觉分析 token 级流式，不新增视觉后端流式接口，不改变 DeepSee 对外协议。

## 9. 非目标

- 不把识图设计成 agent 按需调用的工具。
- 不在第一版实现视觉分析与 DeepSeek 推理并行。
- 不修改 DeepSee 的三种视觉后端 API 以支持流式分析。
- 不要求第三方客户端理解 DSH 内部的结构化导出对象。
