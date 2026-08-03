# DeepSee Desktop 设计文档(应用层)

日期:2026-08-03
状态:规划中(GUI 为下一阶段)

## 1. 仓库定位

DeepSee 生态分两个仓库,分开 git 控制:

- **DeepSee 核心库**(~/Documents/DeepSee):核心库 + OpenAI 兼容服务端
  (`deepsee_server` 包、`deepsee-server` 命令、`pip install deepsee[server]`)。
  服务端设计见核心库仓库 `docs/superpowers/specs/2026-08-03-deepsee-server-design.md`。
- **DeepSee Desktop**(本仓库):桌面 GUI 应用层(Tauri 2 + React + Codewhale 封装)。

## 2. GUI 目标

Codex 风格桌面应用:

- 聊天界面(文本 + 图片拖拽/粘贴)
- 内嵌 Codewhale 作为对话引擎(`exec` 非交互调用,后续 `sessions`/`resume` 补会话)
- 带图对话:图片 → DeepSee 视觉自动路由(UI 元素地图 / 通用描述)→ 分析拼进 prompt
  → Codewhale 回复

## 3. 架构

```
┌──────────────────────────────────────────┐
│ DeepSee Desktop(Tauri 2 应用)            │
│ ┌─────────────┐ IPC ┌─────────────────┐  │
│ │ React 前端   │◄───►│ Rust 主进程      │  │
│ │ 聊天/图片/设置 │     │ · spawn codewhale│  │
│ └─────────────┘     │ · 调核心库服务    │  │
│                     └────────┬────────┘  │
└──────────────────────────────┼───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
     Codewhale CLI(exec)  DeepSee Server     用户工作目录
                            (127.0.0.1:8712)   (可配置 cwd)
```

## 4. 界面(三区)

会话侧栏 / 聊天区(markdown 渲染回复)/ 输入框(图片缩略图、拖拽、⌘V 粘贴)。

## 5. 交互流程

- 无图:文本 → codewhale exec → 回复
- 有图:图片字节 → DeepSee Server 视觉分析 → 分析 + 指令拼 prompt → codewhale exec
- 视觉分析失败:提示错误,可"不带图直接问"

## 6. 配置

设置面板:DeepSeek(api_key/base_url/model)、视觉模型(backend/api_key/base_url/model)、
工作目录、重试次数;生成 `deepsee.toml` 供服务使用。端口等由服务配置
(`[server]` 段 / 环境变量 / 命令行)。

## 7. 后续阶段(按序)

1. Tauri 2 + React 壳子(三区布局、设置面板)
2. Codewhale 封装(exec 调用、会话持久化、流式 serve)
3. 服务进程托管(开机自启、托盘、配置面板)
