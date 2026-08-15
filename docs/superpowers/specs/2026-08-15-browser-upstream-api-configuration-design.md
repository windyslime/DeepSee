# 浏览器配置上游 API 设计

## 目标

允许用户在 DeepSee Web 设置页配置完整的 DeepSeek 与视觉服务连接信息，配置由
本机网关持久化；用户确认后重启受支持的网关进程，并在新进程恢复后分别验证两个
上游连接。

## 已确认需求

- 配置在关闭浏览器和重启网关后继续保留。
- DeepSeek 配置包含 API Key、Base URL 和模型。
- 视觉服务配置包含 Backend、API Key、Base URL 和模型。
- 保存配置后必须重启网关，新配置才进入验证流程。
- 点击确认后必须分别验证 DeepSeek 和视觉服务连接。
- 受 launchd 管理且显式允许浏览器重启时自动重启；其他运行方式提示手动重启。
- 连接验证失败时保留新配置和错误结果，不自动回滚；旧文件保留为本地备份。

## 非目标

- 不在 DSH 中保存视觉供应商密钥。
- 不把上游密钥返回给浏览器或写入浏览器存储。
- 不实现跨平台系统密钥链集成。
- 不自动回滚连接验证失败的配置。
- 不实现跨重启永久 LaunchAgent 安装器。
- 不修改 DSH 的 `llm-dsv` 协议或路由。

## 架构

### 受管配置存储

新增后端受管配置文件：

```text
~/.config/deepsee/upstream.json
```

文件包含完整连接束，由专用配置存储模块负责解析、schema 校验、原子替换、备份和
权限。父目录保持 `0700`，文件与备份保持 `0600`。写入流程先在同目录创建临时
文件并 `fsync`，再以 `os.replace` 提交；任何失败都不得破坏当前文件。

有效配置优先级为：

```text
进程环境变量 > upstream.json > deepsee.toml > 默认值
```

环境变量覆盖的字段在脱敏读取响应中标记为 `source: "env"` 和
`writable: false`。浏览器不得声称写入已覆盖字段会改变有效配置。

配置文件 schema：

```json
{
  "version": 1,
  "deepseek": {
    "api_key": "secret",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat"
  },
  "vision": {
    "backend": "openai_compatible",
    "api_key": "secret",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen-vl-max"
  }
}
```

DSV v1 只接受 `openai_compatible` 视觉 Backend；浏览器设置仍使用现有 Backend
枚举，但保存用于 DSV 的配置时必须通过该约束。

### 配置加载

DeepSee 库原有 `deepsee.toml` 与环境变量行为保持不变。网关新增组合加载入口，
把受管文件映射为低于环境变量、高于 TOML 的候选值，再调用现有强类型配置校验。
推理请求和连接验证使用相同的有效配置解析路径，避免“设置页验证的配置”和“实际
请求配置”分叉。

### 重启控制

网关新增显式 CLI 参数：

```text
--allow-browser-restart
```

只有同时满足以下条件时，Admin API 才报告 `restartSupported: true`：

- 进程以该参数启动；
- 运行环境存在 launchd 提供的 `XPC_SERVICE_NAME`；
- 配置了可用的重启控制器。

保存响应完成后，重启控制器延迟请求当前进程优雅退出。launchd 的 KeepAlive 负责
启动新进程。未满足条件时后端只保存配置，返回 `restartRequired: true` 和
`restartSupported: false`，绝不自行结束无人托管的进程。

`GET /health` 在现有 `status` 外增加进程级 `instanceId`。页面保存前记录旧值，
只有观察到不同的 `instanceId` 才认为重启完成，避免进程退出和拉起过快而漏掉
短暂的离线窗口。现有只读取 `status` 的客户端保持兼容。

## Admin API

所有新端点位于 `/admin/`。启用鉴权时必须提供 `X-DeepSee-Admin-Key`；
`--no-auth` 仅允许现有 loopback 模式。

### `GET /admin/config`

返回脱敏的有效连接信息：

```json
{
  "deepseek": {
    "baseUrl": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "keyConfigured": true,
    "keySource": "managed",
    "keyWritable": true
  },
  "vision": {
    "backend": "openai_compatible",
    "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen-vl-max",
    "keyConfigured": true,
    "keySource": "managed",
    "keyWritable": true
  },
  "restartSupported": true
}
```

响应结构没有 API Key 值字段。

### `POST /admin/config`

请求提交两个完整连接束。每个密钥使用显式操作，避免空字符串语义不清：

```json
{
  "deepseek": {
    "baseUrl": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "key": { "action": "keep" }
  },
  "vision": {
    "backend": "openai_compatible",
    "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen-vl-max",
    "key": { "action": "replace", "value": "secret" }
  }
}
```

密钥操作为 `keep`、`replace` 或 `remove`。未配置密钥时不能 `keep`；环境变量覆盖
时不能 `replace` 或 `remove`。字段、URL、Backend、密钥操作和完整候选配置在写盘
前全部校验。成功响应只返回：

```json
{
  "saved": true,
  "restartRequired": true,
  "restartSupported": true
}
```

响应发送完成后才允许退出进程。

### `POST /admin/config/verify`

新进程使用当前有效配置执行两项独立、最小化的真实请求：

- DeepSeek：最小非流式文本 completion。
- 视觉服务：内置小尺寸测试 PNG 的最小非流式视觉 completion。

两项验证都执行，即使第一项失败。结果只包含 provider、成功状态、耗时和稳定错误
类别；不得包含请求头、响应正文、密钥或上游可能回显的敏感文本。

```json
{
  "deepseek": { "ok": true, "latencyMs": 184 },
  "vision": {
    "ok": false,
    "latencyMs": 312,
    "error": { "code": "AUTH", "message": "认证失败" }
  }
}
```

验证会产生最小上游调用与少量 token 消耗。

## 浏览器体验

现有设置页增加“DeepSeek”和“视觉服务”配置区。每区使用 URL 输入、模型输入和
密码型 API Key 输入；视觉 Backend 使用选择控件。已保存密钥只显示“已配置”，
新输入表示替换，并有明确删除操作。

确认按钮文案为“保存、重启并验证”。提交期间禁用重复提交，状态机为：

```text
editing -> saving -> restarting -> verifying -> success | error
```

流程：

1. 浏览器检查必填项和 URL 基本格式。
2. 调用保存端点。
3. 自动重启可用时轮询 `/health`，等待 `instanceId` 改变，最长 30 秒。
4. 自动重启不可用时显示手动重启状态；页面继续轮询，发现网关恢复后进入验证。
5. 调用验证端点，独立展示 DeepSeek 和视觉服务状态。
6. 验证失败时保留表单的非密机信息；密码输入在提交后清空。

浏览器继续只在当前会话保存网关 public/admin key。上游密钥不进入 React 全局状态、
`sessionStorage`、URL、日志或错误消息；只存在于密码输入的局部表单状态和单次请求
正文中。

## 错误处理

- 请求 schema 或候选配置非法：`400 invalid_request_error`，不写盘。
- Admin key 缺失或错误：沿用现有 `401 authentication_error`。
- 环境变量覆盖导致字段只读：`409 configuration_conflict`，点名字段但不返回值。
- 原子写入失败：`500 configuration_write_error`，旧文件保持有效。
- 自动重启不可用：保存成功，返回手动重启状态。
- 30 秒未恢复：显示“配置已保存，网关重启超时”，不自动回滚。
- 验证失败：HTTP 请求本身成功并返回逐 provider 结果；配置保留以便修正。

## 测试策略

后端测试覆盖：

- 缺失、合法和损坏受管文件；
- 原子写入、备份、目录 `0700` 和文件 `0600`；
- 环境变量优先级与只读冲突；
- Admin API 脱敏、鉴权和 schema；
- 候选配置失败不写盘；
- 重启仅在显式允许和 launchd 环境下发生，且在响应完成后发生；
- DeepSeek 与视觉验证都执行、结果独立、错误脱敏；
- 推理与验证读取同一有效配置。

前端测试覆盖：

- 脱敏配置加载和密钥操作；
- 客户端字段校验；
- 保存、下线检测、恢复检测和验证状态迁移；
- 自动重启不可用时的手动等待；
- 连接验证部分失败；
- 提交后清空密钥输入；
- 浏览器存储不包含上游密钥；
- 重复提交与超时行为。

实现完成后运行后端 pytest、前端 Vitest、TypeScript lint/build，并在
`127.0.0.1:5173` 实际验证设置页。当前 `com.deepsee.gateway` 任务将增加
`--allow-browser-restart` 后重启；操作指南同步更新。
