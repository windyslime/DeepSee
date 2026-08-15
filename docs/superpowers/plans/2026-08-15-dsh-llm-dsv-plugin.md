# DSH LLM DSV 插件实施计划（@deepseek-ai/dsh-llm-dsv）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `deepseek-harness` 仓库（`/Users/jerrywu/deepseek-harness`，特性分支 `feat/dsh-llm-dsv`）新增插件包 `@deepseek-ai/dsh-llm-dsv`，实现：含图请求经 `llm/stream` waterfall 自动路由到 DeepSee DSV 端点（`POST /v1/dsv`，SSE），识图结果以 `vision-analysis` 内容块进入助手消息（UI 识图栏、回放、导出），并在统一 model-visible 投影点过滤使其永不进入下一轮模型历史；同时提供 `deepsee_vision_detail` 视觉追问工具。

**Architecture:** 插件通过 `ctx.on('llm/stream', (options, next) => ...)` 拦截：无图或非对话请求（`purpose !== undefined`）调用 `next()`；含图对话请求短路为 DSV 客户端流。DSV 客户端（fetch + eventsource-parser + idleWatchdog）把 DSV SSE 事件翻译为 `StreamChunk`：`vision.completed` → 独立 `vision-analysis` 块（block-start/block-end），reasoning/answer/tool-call 增量映射到现有分片。`vision-analysis` 块经 BlockAssembler/持久化/客户端原样保留（仓库已验证未知块全链路不被拒绝），仅在 `session/surface.ts` 的 `deriveEventMessage`（统一 model-visible 投影点）过滤，使所有 provider 的下一轮请求都天然不含该块。设置走 `settingsNamespace('llm-dsv')` + `installSettingsSection`（live），凭证走 credential-ref 每请求解析；追问工具 `deepsee_vision_detail` 复用 durable attachment 取原图、直连 DSV 做非流式补充分析，上限 2 轮。

**Tech Stack:** TypeScript（ESM）、pnpm workspace、Cordis、`eventsource-parser`、`@deepseek-ai/schemastery`、vitest（per-file 100% 覆盖率门禁）、真实 Loader composition 集成测试。

## Global Constraints

- 包名 `@deepseek-ai/dsh-llm-dsv`，目录 `packages/llm/llm-dsv/`（`packages/*/*` 两级结构，workspace 自动纳入），version 必须等于根版本 `0.1.0-rc.5`。
- 无图片请求必须调用 `next()`；不得拦截文本请求、标题生成、compaction 等 `purpose` 请求。
- DSV 请求只携带 DSV public key（`Authorization: Bearer <key>`），绝不携带视觉 provider key；请求体不包含任何 provider 凭证。
- `vision.mode` 只允许 `auto | ui | general`；默认 `stream: true`、`vision.include_analysis: true`；请求 `model` 是 DeepSeek 推理模型（透传 `options.model`）。
- `vision-analysis` 块：进入 assistant message（UI/回放/导出），但**绝不**进入下一轮 model-visible 消息——过滤只发生在统一投影点 `deriveEventMessage`，各 provider 序列化器不得各自过滤。
- 原始图片字节不写入会话日志、导出或错误文本（日志/事件只承载文本 + 元数据）。
- 取消必须关闭 HTTP body/reader 和 async iterator，停止产出新分片，不发送虚假 `finish: stop` 或成功 assistant message。
- 错误映射：fetch/传输 → `TRANSPORT`；401/403 → `AUTH`；429 → `RATE_LIMIT`；400 → `INVALID_REQUEST`；5xx → `SERVER`；DSV `error` 事件 `stage=vision` → `VISION_FAILED`，`stage=reasoning` → `REASONING_FAILED`；缺 key → `MISSING_CREDENTIAL`；坏帧 → `MALFORMED_RESPONSE`；流被截断 → `STREAM_CLOSED`。
- `deepsee_vision_detail` 最多追问 2 轮（按会话内已有 `vision-analysis` 块计数），达到上限后继续用已有上下文回答，不无限重试。
- 每个 `packages/*/*` 包必须带 `src/invariant.ts`（注册自身包名）、`README.md` + `README.zh.md` + `README.i18n.yaml`、`tests/**/*.spec.ts`，per-file 100% 覆盖率。
- 新包必须手工登记进 `tsconfig.host.json` 的 `references`；`exports` 必须含 `./invariant` 与 `./src/*`；`@deepseek-ai/cordis` 与 `@deepseek-ai/dsh-invariants` 必须同时是 peer + dev 依赖。
- 流协议义务（`docs/cookbook/adding-an-llm-adapter.md` + `llm/src/invariant.ts` 强制）：`usage` 必须在 `finish` 前、`finish` 后不得有任何 chunk；`finish` 前不得有未闭合块（error/aborted finish 除外）；块 index 按首见顺序分配且复用；`block-end` 的 `block.type` 必须等于 `block-start` 的 `blockType`；流必须以 terminal `finish` 结束。

---

### Task 1: 包骨架与仓库登记

**Files:**
- Create: `packages/llm/llm-dsv/package.json`
- Create: `packages/llm/llm-dsv/tsconfig.json`
- Create: `packages/llm/llm-dsv/src/index.ts`（最小 apply，Task 4 填充）
- Create: `packages/llm/llm-dsv/src/invariant.ts`
- Create: `packages/llm/llm-dsv/README.md`、`README.zh.md`、`README.i18n.yaml`
- Create: `packages/llm/llm-dsv/tests/skeleton.spec.ts`
- Modify: `tsconfig.host.json`（references 增加 llm-dsv）

**Interfaces:**
- Produces: 可挂载的 `llm-dsv` 插件（`name`/`inject`/`apply`）与包级 invariant 伴随插件，满足 `verify-package-invariants`、`constraints` 门禁。

- [ ] **Step 1: 创建特性分支**

```bash
cd /Users/jerrywu/deepseek-harness
git checkout -b feat/dsh-llm-dsv
git status --short   # 期望：空（干净分支）
```

- [ ] **Step 2: 写 package.json（镜像 `packages/llm/llm-deepseek/package.json`）**

`packages/llm/llm-dsv/package.json`：

```json
{
  "name": "@deepseek-ai/dsh-llm-dsv",
  "description": "DeepSee Vision (DSV) LLM route for the DeepSeek Harness: image requests stream through the DSV gateway with an independent vision-analysis block",
  "version": "0.1.0-rc.5",
  "publishConfig": { "access": "public" },
  "repository": {
    "type": "git",
    "url": "git+https://github.com/deepseek-ai/deepseek-harness.git",
    "directory": "packages/llm/llm-dsv"
  },
  "type": "module",
  "main": "lib/index.js",
  "types": "lib/types/index.d.ts",
  "exports": {
    ".": { "types": "./lib/types/index.d.ts", "default": "./lib/index.js" },
    "./invariant": { "types": "./lib/types/invariant.d.ts", "default": "./lib/invariant.js" },
    "./src/*": "./src/*",
    "./package.json": "./package.json"
  },
  "files": ["lib/index.js", "lib/invariant.js", "lib/types/**/*.d.ts"],
  "license": "MIT",
  "peerDependencies": {
    "@deepseek-ai/cordis": "workspace:^",
    "@deepseek-ai/dsh-llm": "workspace:^",
    "@deepseek-ai/dsh-attachment": "workspace:^",
    "@deepseek-ai/dsh-credentials": "workspace:^",
    "@deepseek-ai/dsh-invariants": "workspace:^",
    "@deepseek-ai/dsh-launch-environment": "workspace:^",
    "@deepseek-ai/dsh-session": "workspace:^",
    "@deepseek-ai/dsh-settings": "workspace:^",
    "@deepseek-ai/dsh-timeout": "workspace:^",
    "@deepseek-ai/dsh-tools": "workspace:^"
  },
  "dependencies": {
    "eventsource-parser": "^3.1.0",
    "@deepseek-ai/schemastery": "workspace:^"
  },
  "devDependencies": {
    "@deepseek-ai/cordis": "workspace:^",
    "@deepseek-ai/dsh-llm": "workspace:^",
    "@deepseek-ai/dsh-attachment": "workspace:^",
    "@deepseek-ai/dsh-credentials": "workspace:^",
    "@deepseek-ai/dsh-credentials-local": "workspace:^",
    "@deepseek-ai/dsh-invariants": "workspace:^",
    "@deepseek-ai/dsh-launch-environment": "workspace:^",
    "@deepseek-ai/dsh-llm-deepseek": "workspace:^",
    "@deepseek-ai/dsh-session": "workspace:^",
    "@deepseek-ai/dsh-settings": "workspace:^",
    "@deepseek-ai/dsh-settings-file": "workspace:^",
    "@deepseek-ai/dsh-timeout": "workspace:^",
    "@deepseek-ai/dsh-tools": "workspace:^"
  }
}
```

- [ ] **Step 3: 写 tsconfig.json（references 参照 llm-deepseek 的 tsconfig.json，去掉未用项）**

`packages/llm/llm-dsv/tsconfig.json`：

```json
{
  "extends": "../../../tsconfig.base.json",
  "compilerOptions": { "rootDir": "src", "outDir": "lib/types" },
  "include": ["src"],
  "references": [
    { "path": "../../../vendor/cordis" },
    { "path": "../../../vendor/schemastery" },
    { "path": "../../llm/llm" },
    { "path": "../../attachment/attachment" },
    { "path": "../../credentials/credentials" },
    { "path": "../../core/session" },
    { "path": "../../core/tools" },
    { "path": "../../settings/settings" },
    { "path": "../../util/launch-environment" },
    { "path": "../../util/timeout" },
    { "path": "../../runtime-diagnostics/invariants" }
  ]
}
```

- [ ] **Step 4: 写最小插件入口 + invariant 伴随插件**

`packages/llm/llm-dsv/src/index.ts`：

```ts
/**
 * Route image-bearing LLM requests to the DeepSee Vision (DSV) gateway and
 * surface the vision analysis as an independent `vision-analysis` content
 * block. Full routing lands in a later task; this file currently only
 * declares the package entry.
 * @module @deepseek-ai/dsh-llm-dsv
 */

import type { Context } from '@deepseek-ai/cordis'

/** Plugin name used by cordis.yml rows. */
export const name = 'llm-dsv'
/** Hard dependencies: the plugin routes the LLM seam and registers a tool. */
export const inject = ['llm', 'tools', 'attachments']

/**
 * Mount the DSV route and the vision follow-up tool.
 * @param ctx - Cordis context.
 */
export function apply(ctx: Context): void {
  // Routing, settings, credentials, and the follow-up tool are added by
  // later tasks; keeping a body here satisfies the package entry contract.
  void ctx
}
```

`packages/llm/llm-dsv/src/invariant.ts`（镜像 llm-deepseek/src/invariant.ts）：

```ts
/**
 * Package-owned invariant companion for `@deepseek-ai/dsh-llm-dsv`.
 * @module @deepseek-ai/dsh-llm-dsv/invariant
 */

/* jscpd:ignore-start */
import type { Context } from '@deepseek-ai/cordis'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-llm-dsv'

/** Cordis companion plugin name. */
export const name = 'llm-dsv-invariant'
/** Service required before the companion can reserve package ownership. */
export const inject = ['invariants']

/**
 * No runtime invariant: every stream constraint this package must honor is
 * already enforced by the `@deepseek-ai/dsh-llm` invariant companion.
 */
const install: InvariantInstaller = () => {}

/**
 * Register this package's invariant companion.
 * @param ctx - Cordis context carrying the invariant service.
 * @returns the installed registration's disposer after setup succeeds.
 */
export const apply = (ctx: Context): Promise<() => void> =>
  Promise.resolve(ctx.invariants.register(PACKAGE_NAME, install))
/* jscpd:ignore-end */
```

- [ ] **Step 5: 写骨架冒烟测试**

`packages/llm/llm-dsv/tests/skeleton.spec.ts`：

```ts
import { describe, expect, it } from 'vitest'
import * as LlmDsv from '@deepseek-ai/dsh-llm-dsv'

describe('dsh-llm-dsv package entry', () => {
  it('exports the plugin identity', () => {
    expect(LlmDsv.name).toBe('llm-dsv')
    expect(LlmDsv.inject).toEqual(['llm', 'tools', 'attachments'])
    expect(typeof LlmDsv.apply).toBe('function')
  })
})
```

- [ ] **Step 6: 登记 tsconfig.host.json**

在 `tsconfig.host.json` 的 `references` 数组中，紧邻 `./packages/llm/llm-deepseek` 条目（约第 202 行）之后加入：

```json
{ "path": "./packages/llm/llm-dsv" }
```

- [ ] **Step 7: 写 README 三件套**

`README.md` 写插件用途、DSV 部署边界（连接已运行的 DSV 网关；视觉 provider 配置留在网关侧；只发 DSV public key）、设置字段表、`cordis.yml` 挂载示例；`README.zh.md` 为完整中文对照（两文件内容一致，仅语言不同）；`README.i18n.yaml` 首先生成空记录，末尾统一用 `pnpm run verify-translation-pairing --write packages/llm/llm-dsv/README.md` 生成（若脚本不支持未配对文件则手写两条 sha 占位并在 Task 10 重刷）。

- [ ] **Step 8: 运行验证**

```bash
cd /Users/jerrywu/deepseek-harness
pnpm install
pnpm exec vitest run packages/llm/llm-dsv -t 'package entry'     # 期望：PASS
pnpm run constraints                                             # 期望：通过（含 workspace 约束）
pnpm run verify-package-invariants                               # 期望：通过
pnpm exec tsc -b tsconfig.host.json --pretty false               # 期望：无错误
```

- [ ] **Step 9: Commit**

```bash
git add packages/llm/llm-dsv tsconfig.host.json pnpm-lock.yaml
git commit -m "feat(llm-dsv): scaffold dsh-llm-dsv package and register it in the host build"
```

---

### Task 2: vision-analysis 类型与 DSV 消息序列化

**Files:**
- Create: `packages/llm/llm-dsv/src/types.ts`
- Create: `packages/llm/llm-dsv/src/config.ts`
- Create: `packages/llm/llm-dsv/src/serialize.ts`
- Test: `packages/llm/llm-dsv/tests/serialize.spec.ts`
- Modify: `packages/llm/llm-dsv/src/index.ts`（re-export 类型，供测试与工具使用）

**Interfaces:**
- Produces: `VisionAnalysisBlock`（`type: 'vision-analysis'`，`text` + `metadata{backend,model,mode,durationMs,cacheHit,traceId?}`）；`declare module '@deepseek-ai/dsh-llm' { interface ContentBlockMap { 'vision-analysis': VisionAnalysisBlock } }`。
- Produces: `settingsNamespace('llm-dsv')` 的 `Config`（schemastery schema）、`ResolvedDsvOptions{baseURL, apiKeyRef, mode}`、`resolveDsvOptions(config)`、`DEFAULT_BASE_URL = 'http://127.0.0.1:8712'`、`DEFAULT_API_KEY_ENV = 'DEEPSEE_DSV_API_KEY'`。
- Produces: `serializeRequest(request: GenerateOptions, options: ResolvedDsvOptions, attachments: AttachmentStore): Promise<DsvRequestBody>`；`DsvRequestBody` 字段 `{model, stream: true, messages, tools?, vision: {mode, include_analysis: true}}`。
- Consumes: `ImageAttachmentRef`/`StoredImageAttachment`/`AttachmentStore`（`@deepseek-ai/dsh-attachment`）；`GenerateOptions`/`Message`/`ContentBlock`（`@deepseek-ai/dsh-llm`）；`credentialRef`/`CredentialRef`（`@deepseek-ai/dsh-credentials`）；`settingsNamespace`（`@deepseek-ai/dsh-settings`）。

- [ ] **Step 1: 写失败测试（类型 + 序列化）**

`packages/llm/llm-dsv/tests/serialize.spec.ts`（要点：系统消息前置、文本/图片 base64 转换、assistant 文本+reasoning+tool_calls、tool-result 展开为 `role:'tool'`、vision-analysis 块被跳过、无内容 user 消息跳过、图片读取失败与超限错误码）：

```ts
import { describe, expect, it, vi } from 'vitest'
import { AttachmentStore, type ImageAttachmentRef, type StoredImageAttachment } from '@deepseek-ai/dsh-attachment'
import { createAssistantMessage, createToolResultMessage, createUserMessage, LlmError } from '@deepseek-ai/dsh-llm'
import type { GenerateOptions } from '@deepseek-ai/dsh-llm'
import { CallId } from '@deepseek-ai/dsh-llm'
import { resolveDsvOptions, type Config } from '../src/config.ts'
import { serializeRequest } from '../src/serialize.ts'

const BASE64_PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47]).toString('base64')

const ref = (id: string): ImageAttachmentRef => ({
  attachmentId: id as ImageAttachmentRef['attachmentId'],
  mediaType: 'image/png',
  bytes: 4,
  width: 1,
  height: 1,
})

class FakeAttachments extends AttachmentStore {
  readonly imageLimits = { maxImageBytes: 1024, maxImagesPerMessage: 4, maxMessageImageBytes: 4096, maxImagePixels: 4096, mediaTypes: ['image/png'] }
  validateImage(): Promise<void> { return Promise.resolve() }
  saveImage(): Promise<ImageAttachmentRef> { throw new Error('unused') }
  readImage(r: ImageAttachmentRef): Promise<StoredImageAttachment> {
    return Promise.resolve({ ref: r, data: Buffer.from([0x89, 0x50, 0x4e, 0x47]) })
  }
}

function options(overrides: Partial<GenerateOptions> = {}): GenerateOptions {
  return {
    provider: 'deepseek-official',
    model: 'deepseek-v4-flash',
    messages: [],
    ...overrides,
  }
}

describe('serializeRequest', () => {
  const resolved = resolveDsvOptions({ mode: 'auto' })

  it('prefixes system and converts user text + image blocks to DSV content', async () => {
    const message = createUserMessage({
      content: [
        { type: 'text', text: '这张图里有什么?' },
        { type: 'image', attachment: ref('img-1') },
      ],
      source: { kind: 'user' },
    })
    const body = await serializeRequest(options({ system: 'sys', messages: [message] }), resolved, new FakeAttachments())
    expect(body.model).toBe('deepseek-v4-flash')
    expect(body.stream).toBe(true)
    expect(body.vision).toEqual({ mode: 'auto', include_analysis: true })
    expect(body.messages).toEqual([
      { role: 'system', content: 'sys' },
      {
        role: 'user',
        content: [
          { type: 'text', text: '这张图里有什么?' },
          { type: 'image', source: { type: 'base64', media_type: 'image/png', data: BASE64_PNG } },
        ],
      },
    ])
  })

  it('serializes assistant reasoning and tool calls, dropping vision-analysis blocks', async () => {
    const assistant = createAssistantMessage({
      content: [
        { type: 'vision-analysis', text: '图中有一只猫', metadata: { backend: 'openai_compatible', model: 'qwen-vl-max', mode: 'auto', durationMs: 1, cacheHit: false } },
        { type: 'reasoning', text: '先看图' },
        { type: 'text', text: '这是一只猫' },
        { type: 'tool-call', id: CallId('call-1'), name: 'deepsee_vision_detail', arguments: '{"question":"什么颜色?"}' },
      ],
      source: { provider: 'deepseek-official', model: 'deepseek-v4-flash' },
    })
    const body = await serializeRequest(options({ messages: [assistant] }), resolved, new FakeAttachments())
    expect(body.messages).toEqual([{
      role: 'assistant',
      content: '这是一只猫',
      reasoning_content: '先看图',
      tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'deepsee_vision_detail', arguments: '{"question":"什么颜色?"}' } }],
    }])
  })

  it('expands tool results into role:tool messages', async () => {
    const result = createToolResultMessage({ callId: CallId('call-1'), content: [{ type: 'text', text: '黑色' }], isError: false })
    const body = await serializeRequest(options({ messages: [result] }), resolved, new FakeAttachments())
    expect(body.messages).toEqual([{ role: 'tool', tool_call_id: 'call-1', content: '黑色' }])
  })

  it('passes tools through and skips messages with nothing serializable', async () => {
    const body = await serializeRequest(
      options({ tools: [{ name: 'deepsee_vision_detail', description: 'd', parameters: {} }] }),
      resolved, new FakeAttachments(),
    )
    expect(body.tools).toEqual([{ name: 'deepsee_vision_detail', description: 'd', parameters: {} }])
  })

  it('fails with IMAGE_READ_FAILED when the attachment cannot be read', async () => {
    const message = createUserMessage({ content: [{ type: 'image', attachment: ref('missing') }], source: { kind: 'user' } })
    const broken = new FakeAttachments()
    broken.readImage = () => Promise.reject(new Error('gone'))
    await expect(serializeRequest(options({ messages: [message] }), resolved, broken))
      .rejects.toMatchObject({ code: 'IMAGE_READ_FAILED' })
  })

  it('fails with IMAGE_TOO_LARGE when bytes exceed the deployment limit', async () => {
    const message = createUserMessage({ content: [{ type: 'image', attachment: ref('big') }], source: { kind: 'user' } })
    const oversized = new FakeAttachments()
    oversized.readImage = (r) => Promise.resolve({ ref: { ...r, bytes: 2048 }, data: Buffer.from([1]) })
    await expect(serializeRequest(options({ messages: [message] }), resolved, oversized))
      .rejects.toMatchObject({ code: 'IMAGE_TOO_LARGE' })
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pnpm exec vitest run packages/llm/llm-dsv/tests/serialize.spec.ts
```

期望：FAIL（模块不存在 / 导出未定义）。

- [ ] **Step 3: 写 types.ts / config.ts / serialize.ts**

`packages/llm/llm-dsv/src/types.ts`：

```ts
/**
 * The `vision-analysis` content block: one complete vision-model analysis
 * attached to an assistant message. Display-only — the UI renders it as the
 * 识图 row and the unified model-visible projection filters it out, so it
 * never reaches a provider.
 * @module @deepseek-ai/dsh-llm-dsv/types
 */

/** Non-sensitive vision metadata surfaced in the expanded 识图 row. */
export interface VisionAnalysisMetadata {
  backend: string
  model: string
  mode: 'auto' | 'ui' | 'general'
  durationMs: number
  cacheHit: boolean
  traceId?: string
}

/** One complete vision analysis, mapped from DSV `vision.completed`. */
export interface VisionAnalysisBlock {
  type: 'vision-analysis'
  text: string
  metadata: VisionAnalysisMetadata
}

declare module '@deepseek-ai/dsh-llm' {
  interface ContentBlockMap {
    'vision-analysis': VisionAnalysisBlock
  }
}
```

`packages/llm/llm-dsv/src/config.ts`：

```ts
/**
 * User-settings namespace and connection facts for the DSV route. Values
 * resolve per request (live settings + per-request credential), so rotation
 * reaches the next request without restart.
 * @module dsh-llm-dsv/config
 */

import z from '@deepseek-ai/schemastery'
import { credentialRef, type CredentialRef } from '@deepseek-ai/dsh-credentials'
import { settingsNamespace } from '@deepseek-ai/dsh-settings'

/** Settings namespace (lowercase kebab-case). */
export const NS = settingsNamespace('llm-dsv')
/** Default DSV gateway address (deepsee-server default host/port). */
export const DEFAULT_BASE_URL = 'http://127.0.0.1:8712'
/** Default credential reference for the DSV public key. */
export const DEFAULT_API_KEY_ENV = 'DEEPSEE_DSV_API_KEY'

/** DSV vision modes. */
export const VISION_MODES = ['auto', 'ui', 'general'] as const
export type VisionMode = typeof VISION_MODES[number]

/**
 * Plugin config, validated by the same-named schemastery schema and doubling
 * as the `llm-dsv` settings-section shape. `baseURL` is the DSV gateway
 * address and `apiKeyEnv` only a credential reference — the vision provider's
 * own key never enters DSH.
 */
export interface Config {
  /** DeepSee vision provider profile; DSV v1 requires `openai_compatible` (informational). */
  backend?: string
  /** Credential reference (environment-variable name) for the DSV public key. */
  apiKeyEnv?: string
  /** DSV gateway base URL; `/v1/dsv` is appended. */
  baseURL?: string
  /** Advisory vision model label (gateway-side configuration; informational). */
  model?: string
  /** Vision analysis mode sent in DSV requests. */
  mode?: VisionMode
}

export const Config: z<Config> = z.object({
  backend: z.string().default('openai_compatible'),
  apiKeyEnv: z.string().role('credential-ref').default(DEFAULT_API_KEY_ENV),
  baseURL: z.string().default(DEFAULT_BASE_URL),
  model: z.string(),
  mode: z.union(VISION_MODES).default('auto'),
})

/** One resolution's complete request facts (endpoint + credential reference travel together). */
export interface ResolvedDsvOptions {
  baseURL: string
  apiKeyRef: CredentialRef
  mode: VisionMode
}

/**
 * The one explicit resolve step from raw config to validated connection
 * facts; called once per request.
 * @param config - raw plugin config or resolved settings snapshot.
 * @returns validated DSV connection facts.
 */
export function resolveDsvOptions(config: Config): ResolvedDsvOptions {
  return {
    baseURL: config.baseURL ?? DEFAULT_BASE_URL,
    apiKeyRef: credentialRef(config.apiKeyEnv ?? DEFAULT_API_KEY_ENV),
    mode: config.mode ?? 'auto',
  }
}
```

`packages/llm/llm-dsv/src/serialize.ts`：

```ts
/**
 * Serialize DSH Messages into the DSV wire shape: OpenAI-compatible messages
 * with DSV base64 image blocks. `vision-analysis` blocks are display-only
 * and never serialized; image bytes come from the durable attachment store
 * and never enter the session log, export, or error text.
 * @module dsh-llm-dsv/serialize
 */

import type { AttachmentStore, ImageAttachmentRef, StoredImageAttachment } from '@deepseek-ai/dsh-attachment'
import { LlmError } from '@deepseek-ai/dsh-llm'
import type { ContentBlock, GenerateOptions, Message } from '@deepseek-ai/dsh-llm'
import type { ResolvedDsvOptions } from './config.ts'

/** Stable code: a durable image attachment could not be read. */
export const IMAGE_READ_FAILED_CODE = 'IMAGE_READ_FAILED'
/** Stable code: image bytes exceed the deployment image limit. */
export const IMAGE_TOO_LARGE_CODE = 'IMAGE_TOO_LARGE'

/** One DSV wire content block. */
export type DsvContentBlock =
  | { type: 'text'; text: string }
  | { type: 'image'; source: { type: 'base64'; media_type: string; data: string } }

/** One DSV wire message. */
export type DsvWireMessage =
  | { role: 'system' | 'user' | 'assistant'; content: string | DsvContentBlock[]; reasoning_content?: string; tool_calls?: unknown[] }
  | { role: 'tool'; tool_call_id: string; content: string }

/** The full DSV request body. */
export interface DsvRequestBody {
  model: string
  stream: true
  messages: DsvWireMessage[]
  tools?: GenerateOptions['tools']
  vision: { mode: ResolvedDsvOptions['mode']; include_analysis: true }
}

/** Join the text blocks of a message. */
function flattenText(blocks: readonly ContentBlock[]): string {
  return blocks.filter(block => block.type === 'text').map(block => block.text).join('')
}

/** Read one durable image and encode it as a DSV base64 block. */
async function imageBlock(block: Extract<ContentBlock, { type: 'image' }>, attachments: AttachmentStore, signal?: AbortSignal): Promise<DsvContentBlock> {
  let stored: StoredImageAttachment
  try {
    stored = await attachments.readImage(block.attachment, signal)
  } catch (error) {
    throw new LlmError('llm-dsv: failed to read the image attachment', IMAGE_READ_FAILED_CODE, { cause: error })
  }
  if (stored.ref.bytes > attachments.imageLimits.maxImageBytes) {
    throw new LlmError('llm-dsv: image exceeds the deployment size limit', IMAGE_TOO_LARGE_CODE)
  }
  return {
    type: 'image',
    source: { type: 'base64', media_type: stored.ref.mediaType, data: Buffer.from(stored.data).toString('base64') },
  }
}

/** Serialize one user message's text + image blocks. */
async function userContent(blocks: readonly ContentBlock[], attachments: AttachmentStore, signal?: AbortSignal): Promise<DsvContentBlock[]> {
  const content: DsvContentBlock[] = []
  for (const block of blocks) {
    switch (block.type) {
      case 'text':
        content.push({ type: 'text', text: block.text })
        break
      case 'image':
        content.push(await imageBlock(block, attachments, signal))
        break
      default:
        // vision-analysis and other merge-extensible blocks are not user wire vocabulary.
        break
    }
  }
  return content
}

/** Serialize one assistant message (text + reasoning + tool calls). */
function serializeAssistant(message: Message): DsvWireMessage {
  const text = flattenText(message.content)
  const reasoning = message.content.filter(block => block.type === 'reasoning').map(block => block.text).join('')
  const toolCalls = message.content.filter(block => block.type === 'tool-call').map(block => ({
    id: block.id,
    type: 'function' as const,
    function: { name: block.name, arguments: block.arguments },
  }))
  return {
    role: 'assistant',
    content: text,
    ...toolCalls.length > 0 && reasoning.length > 0 ? { reasoning_content: reasoning } : {},
    ...toolCalls.length > 0 ? { tool_calls: toolCalls } : {},
  }
}

/**
 * Serialize the conversation. Tool results become standalone `role: 'tool'`
 * messages; user text + images become a DSV content array. Messages with
 * nothing serializable (e.g. display-only blocks) are skipped.
 */
export async function serializeMessages(
  messages: readonly Message[],
  attachments: AttachmentStore,
  signal?: AbortSignal,
): Promise<DsvWireMessage[]> {
  const wire: DsvWireMessage[] = []
  for (const message of messages) {
    if (message.role === 'system') {
      wire.push({ role: 'system', content: flattenText(message.content) })
      continue
    }
    if (message.role === 'assistant') {
      wire.push(serializeAssistant(message))
      continue
    }
    const toolResults = message.content.filter(block => block.type === 'tool-result')
    const content = await userContent(message.content, attachments, signal)
    if (content.length > 0 || toolResults.length === 0) {
      wire.push({ role: 'user', content })
    }
    for (const result of toolResults) {
      wire.push({
        role: 'tool',
        tool_call_id: result.toolCallId,
        content: flattenText(result.content) || '(no output)',
      })
    }
  }
  return wire
}

/**
 * Build the full DSV request body: full message history, the reasoning
 * model id, tool schemas, and the vision envelope.
 */
export async function serializeRequest(
  request: GenerateOptions,
  options: ResolvedDsvOptions,
  attachments: AttachmentStore,
): Promise<DsvRequestBody> {
  const messages: DsvWireMessage[] = []
  if (request.system !== undefined) messages.push({ role: 'system', content: request.system })
  messages.push(...await serializeMessages(request.messages, attachments, request.signal))
  return {
    model: request.model,
    stream: true,
    messages,
    ...request.tools !== undefined && request.tools.length > 0 ? { tools: request.tools } : {},
    vision: { mode: options.mode, include_analysis: true },
  }
}
```

`packages/llm/llm-dsv/src/index.ts` 顶部追加 re-export：

```ts
export {
  DEFAULT_API_KEY_ENV,
  DEFAULT_BASE_URL,
  NS,
  VISION_MODES,
  Config,
  resolveDsvOptions,
} from './config.ts'
export type { Config, ResolvedDsvOptions, VisionMode } from './config.ts'
export { IMAGE_READ_FAILED_CODE, IMAGE_TOO_LARGE_CODE, serializeRequest } from './serialize.ts'
export type { DsvContentBlock, DsvRequestBody, DsvWireMessage } from './serialize.ts'
export type { VisionAnalysisBlock, VisionAnalysisMetadata } from './types.ts'
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pnpm exec vitest run packages/llm/llm-dsv/tests/serialize.spec.ts
```

期望：PASS。若 `Buffer` 类型缺失，在测试文件头加 `import { Buffer } from 'node:buffer'`（host 测试平面允许 node 导入）。

- [ ] **Step 5: Commit**

```bash
git add packages/llm/llm-dsv
git commit -m "feat(llm-dsv): add vision-analysis block type, settings config, and DSV request serialization"
```

---

### Task 3: DSV SSE 解析与分片翻译

**Files:**
- Create: `packages/llm/llm-dsv/src/sse.ts`
- Create: `packages/llm/llm-dsv/src/translate.ts`
- Test: `packages/llm/llm-dsv/tests/sse.spec.ts`
- Test: `packages/llm/llm-dsv/tests/translate.spec.ts`

**Interfaces:**
- Produces: `parseSse(stream, onComment?)` → `AsyncGenerator<string>`（同 llm-deepseek 语义：`[DONE]` 终值、EOF 无 `[DONE]` 抛 `LlmError('STREAM_CLOSED')`）。
- Produces: `translateDsv(payloads: AsyncIterable<string>): AsyncGenerator<StreamChunk>`——DSV 事件 → StreamChunk 的单一翻译器（按 `event.type` 分派）：
  - `response.created` → 无输出（建立本地关联）。
  - `vision.started` → `block-start{index:0, blockType:'vision-analysis'}`。
  - `vision.completed` → `block-end{index:0, block: VisionAnalysisBlock}`（`vision.analysis` → `text`；`mode/backend/model/latency_ms/cache_hit/trace_id` → metadata）。
  - `reasoning.started`/`reasoning.delta` → `block-start` + `reasoning-delta`。
  - `answer.delta` → `block-start` + `text-delta`。
  - `tool_call.delta` → `block-start` + `tool-call-delta`（`id`/`name` 取自首帧，`argumentsDelta` 取原始增量）。
  - `tool_call.completed` → 记录权威合并调用（`id`/`name`/完整 `arguments`），块延迟到收尾闭合。
  - `response.requires_action` → 记录 `{kind:'tool-calls'}` finish。
  - `response.completed` → 冲刷全部未闭合块（text/reasoning/tool-call 用权威数据）、`usage`（DeepSeek 形状 → disjoint `TokenUsage`）、`finish`（status `completed`→stop / `requires_action`→tool-calls / `failed`→error），随后返回。
  - `error` → `stage='vision'` 时先以空文本闭合 vision 块（若未闭合）再 `finish{kind:'error', code:'VISION_FAILED'}`；`stage='reasoning'` 时 `finish{kind:'error', code:'REASONING_FAILED'}`；输出 finish 后停止消费。
  - `[DONE]` 且尚未 finish（缺少 `response.completed`）→ `finish{kind:'error', code:'MALFORMED_RESPONSE'}`。
  - JSON 解析失败 → throw `LlmError('MALFORMED_RESPONSE')`。
- Consumes: `StreamChunk`/`LlmError`/`CallId`（`@deepseek-ai/dsh-llm`）。

- [ ] **Step 1: 写失败测试**

`packages/llm/llm-dsv/tests/sse.spec.ts`（镜像 llm-deepseek 的 sse.spec.ts 写法：流分片重组、`[DONE]`、EOF 截断）：

```ts
import { describe, expect, it } from 'vitest'
import { LlmError } from '@deepseek-ai/dsh-llm'
import { parseSse, DONE } from '../src/sse.ts'

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk))
      controller.close()
    },
  })
}

async function collect(chunks: string[]): Promise<string[]> {
  const payloads: string[] = []
  for await (const payload of parseSse(streamOf(chunks))) payloads.push(payload)
  return payloads
}

describe('parseSse', () => {
  it('reassembles split frames and yields data payloads', async () => {
    expect(await collect(['data: {"type":"vision.started"}\n\n', 'data: [DO', 'NE]\n\n']))
      .toEqual(['{"type":"vision.started"}', DONE])
  })

  it('skips comment lines and joins multi-data fields', async () => {
    expect(await collect([': keepalive\ndata: {"type":"a"}\ndata: {"type":"b"}\n\n', `data: ${DONE}\n\n`]))
      .toEqual(['{"type":"a"}\n{"type":"b"}', DONE])
  })

  it('throws STREAM_CLOSED on EOF without [DONE]', async () => {
    await expect(collect(['data: {"type":"a"}\n\n'])).rejects.toMatchObject({ code: 'STREAM_CLOSED' })
  })
})
```

`packages/llm/llm-dsv/tests/translate.spec.ts`：

```ts
import { describe, expect, it } from 'vitest'
import type { StreamChunk } from '@deepseek-ai/dsh-llm'
import { translateDsv } from '../src/translate.ts'

async function translate(frames: unknown[]): Promise<StreamChunk[]> {
  const chunks: StreamChunk[] = []
  for await (const chunk of translateDsv((async function* () {
    for (const frame of frames) yield JSON.stringify(frame)
  })())) chunks.push(chunk)
  return chunks
}

const VISION = {
  analysis: '图中有一只猫', mode: 'auto', backend: 'openai_compatible', model: 'qwen-vl-max',
  latency_ms: 321, cache_hit: false, trace_id: 'trace-1',
}

describe('translateDsv', () => {
  it('emits the vision block before answer deltas, then usage and a stop finish', async () => {
    const chunks = await translate([
      { type: 'response.created', id: 'dsv_1' },
      { type: 'vision.started', id: 'dsv_1' },
      { type: 'vision.completed', id: 'dsv_1', vision: VISION },
      { type: 'reasoning.started', id: 'dsv_1' },
      { type: 'reasoning.delta', id: 'dsv_1', text: '先看图' },
      { type: 'answer.delta', id: 'dsv_1', text: '这是一只猫' },
      { type: 'answer.completed', id: 'dsv_1', text: '这是一只猫', reasoning: '先看图' },
      { type: 'response.completed', id: 'dsv_1', status: 'completed', usage: { prompt_tokens: 10, completion_tokens: 5, prompt_tokens_details: { cached_tokens: 3 } } },
      '[DONE]',
    ])
    expect(chunks[0]).toEqual({ type: 'block-start', index: 0, blockType: 'vision-analysis' })
    expect(chunks[1]).toEqual({
      type: 'block-end', index: 0,
      block: {
        type: 'vision-analysis', text: '图中有一只猫',
        metadata: { backend: 'openai_compatible', model: 'qwen-vl-max', mode: 'auto', durationMs: 321, cacheHit: false, traceId: 'trace-1' },
      },
    })
    expect(chunks.some(chunk => chunk.type === 'text-delta' && chunk.text === '这是一只猫')).toBe(true)
    expect(chunks.at(-2)).toEqual({ type: 'usage', usage: { inputTokens: 7, outputTokens: 5, cacheReadTokens: 3 } })
    expect(chunks.at(-1)).toEqual({ type: 'finish', reason: { kind: 'stop' } })
  })

  it('maps tool_call deltas and requires_action to tool-call blocks and a tool-calls finish', async () => {
    const chunks = await translate([
      { type: 'vision.started', id: 'dsv_2' },
      { type: 'vision.completed', id: 'dsv_2', vision: VISION },
      { type: 'tool_call.delta', id: 'dsv_2', index: 0, delta: { index: 0, id: 'call-9', type: 'function', function: { name: 'deepsee_vision_detail', arguments: '{"que' } }, tool_call: { id: 'call-9', type: 'function', function: { name: 'deepsee_vision_detail', arguments: '{"que' } } },
      { type: 'tool_call.delta', id: 'dsv_2', index: 0, delta: { index: 0, function: { arguments: 'stion":"什么颜色?"}' } }, tool_call: { id: 'call-9', type: 'function', function: { name: 'deepsee_vision_detail', arguments: '{"question":"什么颜色?"}' } } },
      { type: 'tool_call.completed', id: 'dsv_2', index: 0, tool_call: { id: 'call-9', type: 'function', function: { name: 'deepsee_vision_detail', arguments: '{"question":"什么颜色?"}' } } },
      { type: 'response.requires_action', id: 'dsv_2', tool_calls: [{ id: 'call-9', type: 'function', function: { name: 'deepsee_vision_detail', arguments: '{"question":"什么颜色?"}' } }] },
      { type: 'response.completed', id: 'dsv_2', status: 'requires_action', usage: {} },
      '[DONE]',
    ])
    const toolDeltas = chunks.filter(chunk => chunk.type === 'tool-call-delta')
    expect(toolDeltas).toEqual([
      { type: 'tool-call-delta', index: 1, id: 'call-9', name: 'deepsee_vision_detail', argumentsDelta: '{"que' },
      { type: 'tool-call-delta', index: 1, id: 'call-9', argumentsDelta: 'stion":"什么颜色?"}' },
    ])
    const end = chunks.find(chunk => chunk.type === 'block-end' && chunk.block.type === 'tool-call')
    expect(end).toEqual({ type: 'block-end', index: 1, block: { type: 'tool-call', id: 'call-9', name: 'deepsee_vision_detail', arguments: '{"question":"什么颜色?"}' } })
    expect(chunks.at(-1)).toEqual({ type: 'finish', reason: { kind: 'tool-calls' } })
  })

  it('maps a vision-stage error to an empty vision block and a VISION_FAILED finish', async () => {
    const chunks = await translate([
      { type: 'vision.started', id: 'dsv_3' },
      { type: 'error', id: 'dsv_3', stage: 'vision', error: { message: '图片校验失败', type: 'invalid_request_error' } },
      { type: 'response.completed', id: 'dsv_3', status: 'failed', usage: {} },
      '[DONE]',
    ])
    expect(chunks.at(-1)).toEqual({
      type: 'finish',
      reason: { kind: 'error', failure: { message: '图片校验失败', code: 'VISION_FAILED' } },
    })
  })

  it('maps a reasoning-stage error to a REASONING_FAILED finish after the vision block', async () => {
    const chunks = await translate([
      { type: 'vision.started', id: 'dsv_4' },
      { type: 'vision.completed', id: 'dsv_4', vision: VISION },
      { type: 'error', id: 'dsv_4', stage: 'reasoning', error: { message: '上游 502', type: 'upstream_error' } },
      { type: 'response.completed', id: 'dsv_4', status: 'failed', usage: {} },
      '[DONE]',
    ])
    expect(chunks.find(chunk => chunk.type === 'block-end')?.block).toMatchObject({ type: 'vision-analysis', text: '图中有一只猫' })
    expect(chunks.at(-1)).toEqual({
      type: 'finish',
      reason: { kind: 'error', failure: { message: '上游 502', code: 'REASONING_FAILED' } },
    })
  })

  it('treats [DONE] without response.completed as MALFORMED_RESPONSE', async () => {
    const chunks = await translate([
      { type: 'vision.started', id: 'dsv_5' },
      { type: 'vision.completed', id: 'dsv_5', vision: VISION },
      '[DONE]',
    ])
    expect(chunks.at(-1)).toMatchObject({ type: 'finish', reason: { kind: 'error', failure: { code: 'MALFORMED_RESPONSE' } } })
  })

  it('throws MALFORMED_RESPONSE on a bad JSON frame', async () => {
    await expect(async () => {
      for await (const _ of translateDsv((async function* () { yield 'not-json' })())) { /* noop */ }
    }).rejects.toMatchObject({ code: 'MALFORMED_RESPONSE' })
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pnpm exec vitest run packages/llm/llm-dsv/tests/sse.spec.ts packages/llm/llm-dsv/tests/translate.spec.ts
```

期望：FAIL（模块不存在）。

- [ ] **Step 3: 写 sse.ts（镜像 llm-deepseek/src/sse.ts）**

`packages/llm/llm-dsv/src/sse.ts`：

```ts
/**
 * Decode a DSV SSE byte stream into event `data` payloads. Framing is
 * `eventsource-parser`'s; the literal `[DONE]` is yielded so the caller owns
 * final flushing, and EOF before it throws `LlmError('STREAM_CLOSED')`.
 * @module dsh-llm-dsv/sse
 */

import { EventSourceParserStream } from 'eventsource-parser/stream'
import { LlmError } from '@deepseek-ai/dsh-llm'

/** The terminal payload DSV sends after the last event. */
export const DONE = '[DONE]'

/**
 * Parse an SSE byte stream into data payloads.
 * @param stream - raw SSE bytes; reads may split anywhere.
 * @param onComment - optional transport-activity callback.
 * @returns each event's data payload, `[DONE]` last.
 */
export async function* parseSse(
  stream: ReadableStream<BufferSource>,
  onComment?: (comment: string) => void,
): AsyncGenerator<string> {
  const events = stream
    .pipeThrough(new TextDecoderStream())
    .pipeThrough(new EventSourceParserStream({ onComment }))
  for await (const { data } of events) {
    yield data
    if (data === DONE) return
  }
  throw new LlmError('DSV SSE stream ended without [DONE]', 'STREAM_CLOSED')
}
```

- [ ] **Step 4: 写 translate.ts**

`packages/llm/llm-dsv/src/translate.ts`：

```ts
/**
 * Translate DSV SSE event frames into harness StreamChunks. Dispatch is by
 * `event.type` only; unknown event types are ignored for forward
 * compatibility. `vision.completed` closes the vision block before any
 * reasoning/answer/tool-call delta; block-ends for text/reasoning/tool-call
 * are deferred to `response.completed` and built from the authoritative
 * merged tool-call data. `[DONE]` alone is never a business success.
 * @module dsh-llm-dsv/translate
 */

import { CallId, LlmError } from '@deepseek-ai/dsh-llm'
import type { ContentBlock, FinishReason, StreamChunk, TokenUsage } from '@deepseek-ai/dsh-llm'
import type { VisionAnalysisBlock, VisionAnalysisMetadata } from './types.ts'
import { DONE } from './sse.ts'

/** Stable code for a vision-stage DSV failure. */
export const VISION_FAILED_CODE = 'VISION_FAILED'
/** Stable code for a reasoning-stage DSV failure. */
export const REASONING_FAILED_CODE = 'REASONING_FAILED'
/** Stable code for a protocol violation in the DSV stream. */
export const MALFORMED_RESPONSE_CODE = 'MALFORMED_RESPONSE'

/** One DSV event frame (subset this translator consumes). */
interface DsvFrame {
  type?: string
  id?: string
  stage?: string
  status?: string
  text?: string
  vision?: {
    analysis?: string
    mode?: string
    backend?: string
    model?: string
    latency_ms?: number
    cache_hit?: boolean
    trace_id?: string
  }
  delta?: {
    index?: number
    id?: string
    function?: { name?: string; arguments?: string }
  }
  tool_call?: { id?: string; type?: string; function?: { name?: string; arguments?: string } }
  error?: { message?: string }
  usage?: Record<string, unknown>
}

/** One open block under assembly. */
interface OpenBlock {
  index: number
  kind: 'text' | 'reasoning' | 'tool-call'
  text: string
  callId?: string
  name?: string
}

/** Map a DeepSeek-shaped usage object to disjoint harness counts. */
export function mapUsage(usage: Record<string, unknown>): TokenUsage {
  const prompt = typeof usage.prompt_tokens === 'number' ? usage.prompt_tokens : 0
  const completion = typeof usage.completion_tokens === 'number' ? usage.completion_tokens : 0
  const details = usage.prompt_tokens_details as { cached_tokens?: number } | undefined
  const cacheRead = details?.cached_tokens
  return {
    inputTokens: prompt - (cacheRead ?? 0),
    outputTokens: completion,
    ...cacheRead !== undefined ? { cacheReadTokens: cacheRead } : {},
  }
}

function visionMetadata(vision: NonNullable<DsvFrame['vision']>): VisionAnalysisMetadata {
  return {
    backend: vision.backend ?? '',
    model: vision.model ?? '',
    mode: vision.mode === 'ui' || vision.mode === 'general' ? vision.mode : 'auto',
    durationMs: vision.latency_ms ?? 0,
    cacheHit: vision.cache_hit ?? false,
    ...vision.trace_id !== undefined ? { traceId: vision.trace_id } : {},
  }
}

/**
 * Consume DSV SSE data payloads and yield StreamChunks.
 * @param payloads - SSE data payloads, `[DONE]`-terminated.
 * @returns vision/reasoning/text/tool-call chunks plus a terminal finish.
 */
export async function* translateDsv(payloads: AsyncIterable<string>): AsyncGenerator<StreamChunk> {
  let nextIndex = 1
  let visionOpen = false
  let visionClosed = false
  let textBlock: OpenBlock | undefined
  let reasoningBlock: OpenBlock | undefined
  const toolBlocks = new Map<number, OpenBlock>()
  const toolOrder: OpenBlock[] = []
  const authoritativeCalls = new Map<number, { id: string; name: string; arguments: string }>()
  let pendingFinish: FinishReason | undefined
  let pendingUsage: TokenUsage | undefined
  let finished = false

  function open(kind: OpenBlock['kind']): OpenBlock {
    const block: OpenBlock = { index: nextIndex++, kind, text: '' }
    if (kind === 'tool-call') toolBlocks.set(block.index, block)
    toolOrder.push(block)
    return block
  }

  function closeBlock(block: OpenBlock): ContentBlock {
    switch (block.kind) {
      case 'text': return { type: 'text', text: block.text }
      case 'reasoning': return { type: 'reasoning', text: block.text }
      case 'tool-call': {
        const authoritative = authoritativeCalls.get(block.index)
        return {
          type: 'tool-call',
          id: CallId(authoritative?.id ?? block.callId ?? ''),
          name: authoritative?.name ?? block.name ?? '',
          arguments: authoritative?.arguments ?? block.text,
        }
      }
    }
  }

  function flushBlocks(): void {
    for (const block of toolOrder) {
      yield { type: 'block-end', index: block.index, block: closeBlock(block) }
    }
  }

  function finish(reason: FinishReason): void {
    finished = true
    if (pendingUsage !== undefined) {
      yield { type: 'usage', usage: pendingUsage }
      pendingUsage = undefined
    }
    yield { type: 'finish', reason }
  }

  for await (const payload of payloads) {
    if (finished) return
    if (payload === DONE) {
      if (!finished) finish({ kind: 'error', failure: { message: 'DSV stream ended before response.completed', code: MALFORMED_RESPONSE_CODE } })
      return
    }
    let frame: DsvFrame
    try {
      frame = JSON.parse(payload) as DsvFrame
    } catch {
      throw new LlmError(`DSV stream carried a malformed frame: ${payload.slice(0, 80)}`, MALFORMED_RESPONSE_CODE)
    }
    switch (frame.type) {
      case 'response.created':
        // Local association id; nothing to emit.
        break
      case 'vision.started':
        if (!visionOpen && !visionClosed) {
          visionOpen = true
          yield { type: 'block-start', index: 0, blockType: 'vision-analysis' }
        }
        break
      case 'vision.completed': {
        if (visionOpen && !visionClosed && frame.vision !== undefined) {
          const block: VisionAnalysisBlock = { type: 'vision-analysis', text: frame.vision.analysis ?? '', metadata: visionMetadata(frame.vision) }
          visionOpen = false
          visionClosed = true
          yield { type: 'block-end', index: 0, block }
        }
        break
      }
      case 'reasoning.started':
        if (reasoningBlock === undefined) {
          reasoningBlock = open('reasoning')
          yield { type: 'block-start', index: reasoningBlock.index, blockType: 'reasoning' }
        }
        break
      case 'reasoning.delta':
        if (reasoningBlock === undefined) {
          reasoningBlock = open('reasoning')
          yield { type: 'block-start', index: reasoningBlock.index, blockType: 'reasoning' }
        }
        if (typeof frame.text === 'string' && frame.text.length > 0) {
          reasoningBlock.text += frame.text
          yield { type: 'reasoning-delta', index: reasoningBlock.index, text: frame.text }
        }
        break
      case 'answer.delta':
        if (textBlock === undefined) {
          textBlock = open('text')
          yield { type: 'block-start', index: textBlock.index, blockType: 'text' }
        }
        if (typeof frame.text === 'string' && frame.text.length > 0) {
          textBlock.text += frame.text
          yield { type: 'text-delta', index: textBlock.index, text: frame.text }
        }
        break
      case 'tool_call.delta': {
        const rawIndex = typeof frame.delta?.index === 'number' ? frame.delta.index : 0
        let block = [...toolBlocks.values()].find(candidate => candidate.callId === frame.delta?.id)
        if (block === undefined) {
          const byIndex = [...toolBlocks.values()].find(candidate => candidate.callId === undefined)
          block = byIndex ?? open('tool-call')
          block.callId = frame.delta?.id ?? frame.tool_call?.id
          if (block.callId !== undefined) block.callId = String(block.callId)
          yield { type: 'block-start', index: block.index, blockType: 'tool-call' }
        }
        const name = frame.delta?.function?.name
        if (typeof name === 'string' && name.length > 0) block.name = name
        const argumentsDelta = frame.delta?.function?.arguments
        if (typeof argumentsDelta === 'string' && argumentsDelta.length > 0) {
          block.text += argumentsDelta
        }
        void rawIndex
        yield {
          type: 'tool-call-delta',
          index: block.index,
          id: CallId(String(block.callId ?? '')),
          ...block.name !== undefined ? { name: block.name } : {},
          argumentsDelta: argumentsDelta ?? '',
        }
        break
      }
      case 'tool_call.completed': {
        const merged = frame.tool_call
        if (merged !== undefined && typeof merged.id === 'string') {
          const target = [...toolBlocks.values()].find(candidate => candidate.callId === merged.id)
          if (target !== undefined) {
            authoritativeCalls.set(target.index, {
              id: merged.id,
              name: merged.function?.name ?? '',
              arguments: merged.function?.arguments ?? target.text,
            })
          }
        }
        break
      }
      case 'response.requires_action':
        pendingFinish = { kind: 'tool-calls' }
        break
      case 'answer.completed':
        break
      case 'response.completed': {
        if (frame.status === 'failed') {
          finish({ kind: 'error', failure: { message: 'DSV response failed', code: pendingErrorCode ?? REASONING_FAILED_CODE } })
          return
        }
        if (pendingUsage === undefined && frame.usage !== undefined) pendingUsage = mapUsage(frame.usage)
        flushBlocks()
        finish(pendingFinish ?? { kind: 'stop' })
        return
      }
      case 'error': {
        const message = frame.error?.message ?? 'DSV upstream error'
        if (frame.stage === 'vision' && visionOpen && !visionClosed) {
          const block: VisionAnalysisBlock = { type: 'vision-analysis', text: '', metadata: { backend: '', model: '', mode: 'auto', durationMs: 0, cacheHit: false } }
          visionOpen = false
          visionClosed = true
          yield { type: 'block-end', index: 0, block }
        }
        const code = frame.stage === 'vision' ? VISION_FAILED_CODE : REASONING_FAILED_CODE
        finish({ kind: 'error', failure: { message, code } })
        return
      }
      default:
        // Unknown event types are forward-compatible: ignore, keep streaming.
        break
    }
  }
  if (!finished) finish({ kind: 'error', failure: { message: 'DSV stream ended without a terminal finish', code: MALFORMED_RESPONSE_CODE } })
}
```

> 注：`pendingErrorCode` 在实现时改为直接使用 stage 映射（上面 `error` case 已先行 finish 并 return，`response.completed` 的 failed 分支只在未收到 error 事件时兜底，code 用 `REASONING_FAILED_CODE`）。

- [ ] **Step 5: 运行测试确认通过**

```bash
pnpm exec vitest run packages/llm/llm-dsv/tests/sse.spec.ts packages/llm/llm-dsv/tests/translate.spec.ts
```

期望：PASS。若工具块 index 关联与测试期望不符，以「id 关联优先、index 兜底、首见顺序分配」为准调整测试或实现，保证 `validateStream` 语法（block-start 不重复、delta 有开放块、block-end type 匹配）通过。

- [ ] **Step 6: Commit**

```bash
git add packages/llm/llm-dsv
git commit -m "feat(llm-dsv): parse DSV SSE and translate events into harness stream chunks"
```

---

### Task 4: DSV 客户端与 llm/stream 路由

**Files:**
- Create: `packages/llm/llm-dsv/src/client.ts`
- Modify: `packages/llm/llm-dsv/src/index.ts`（完整 apply：路由 + 设置 + 凭证 + 工具注册占位）
- Test: `packages/llm/llm-dsv/tests/mock-server.ts`
- Test: `packages/llm/llm-dsv/tests/client.spec.ts`
- Test: `packages/llm/llm-dsv/tests/route.spec.ts`

**Interfaces:**
- Produces: `DsvClient`（构造参数 `{ options(): ResolvedDsvOptions; resolveApiKey(): Promise<string>; streamIdleTimeoutMs: number }`）：`stream(request: GenerateOptions, attachments: AttachmentStore): AsyncGenerator<StreamChunk>`；`request(request: GenerateOptions, attachments: AttachmentStore, stream: boolean): AsyncGenerator<StreamChunk>`（内部，供工具复用）。
- Produces: `apply(ctx, config)` 完整逻辑：`installSettingsSection(ctx, NS, Config, config, { setSource })`；每请求 `resolveDsvOptions(current())` + `resolveApiKey()`（credentials → launchEnvironment 回退，`assertUsableApiKey`，缺 key 抛 `MISSING_CREDENTIAL`）；`ctx.on('llm/stream', handler)`，`handler` 在 `options.purpose !== undefined || !contentHasImage(options.messages)` 时 `return next()`，否则 `return client.stream(options, ctx.attachments)`。
- Consumes: `contentHasImage`/`LlmError`/`assertUsableApiKey`（`@deepseek-ai/dsh-llm`）；`idleWatchdog`/`timeoutOf`（`@deepseek-ai/dsh-timeout`）；`launchEnvironmentOf`（`@deepseek-ai/dsh-launch-environment`）；`installSettingsSection`（`@deepseek-ai/dsh-settings`）。

- [ ] **Step 1: 写失败测试（mock server + client + route）**

`packages/llm/llm-dsv/tests/mock-server.ts`（镜像 llm-deepseek/tests/mock-server.ts，行为事件为 DSV 帧字符串）：

```ts
import { createServer } from 'node:http'
import type { IncomingMessage, Server, ServerResponse } from 'node:http'

/** One scripted behavior for the next request the mock server receives. */
export type Behavior =
  | { kind: 'sse'; events: string[]; delayMs?: number }
  | { kind: 'json'; body: unknown; status?: number }
  | { kind: 'http-error'; status: number; body: string }
  | { kind: 'close-early'; events: string[] }

export interface MockServer {
  url: string
  requests: unknown[]
  headers: IncomingMessage['headers'][]
  script: Behavior[]
  close(): Promise<void>
}

const servers: Server[] = []

export async function closeMockServers(): Promise<void> {
  await Promise.all(servers.splice(0).map(server => new Promise(resolve => server.close(() => resolve()))))
}

function frame(payload: unknown): string {
  return `data: ${typeof payload === 'string' ? payload : JSON.stringify(payload)}\n\n`
}

/** A minimal complete DSV vision + answer stream. */
export function dsvVisionStream(analysis = '图中有一只猫', answer = '这是一只猫'): string[] {
  return [
    frame({ type: 'response.created', id: 'dsv_1' }),
    frame({ type: 'vision.started', id: 'dsv_1' }),
    frame({ type: 'vision.completed', id: 'dsv_1', vision: { analysis, mode: 'auto', backend: 'openai_compatible', model: 'qwen-vl-max', latency_ms: 12, cache_hit: false, trace_id: 't-1' } }),
    frame({ type: 'reasoning.started', id: 'dsv_1' }),
    frame({ type: 'reasoning.delta', id: 'dsv_1', text: '看图' }),
    frame({ type: 'answer.delta', id: 'dsv_1', text: answer }),
    frame({ type: 'answer.completed', id: 'dsv_1', text: answer, reasoning: '看图' }),
    frame({ type: 'response.completed', id: 'dsv_1', status: 'completed', usage: { prompt_tokens: 4, completion_tokens: 2 } }),
    'data: [DONE]\n\n',
  ]
}

/** A non-stream DSV vision envelope, used by the follow-up tool. */
export function dsvVisionEnvelope(analysis = '补充分析'): unknown {
  return {
    id: 'dsv_tool_1', object: 'dsv.response', status: 'completed',
    vision: { analysis, mode: 'auto', backend: 'openai_compatible', model: 'qwen-vl-max', latency_ms: 5, cache_hit: false },
    answer: { text: '' }, usage: {},
  }
}

/** Local DSV stand-in: replays scripted behaviors per request. */
export async function mockServer(script: Behavior[]): Promise<MockServer> {
  const requests: unknown[] = []
  const headers: IncomingMessage['headers'][] = []
  const server = createServer((request: IncomingMessage, response: ServerResponse) => {
    let body = ''
    request.on('data', (chunk: Buffer) => { body += chunk.toString('utf8') })
    request.on('end', () => {
      requests.push(body.length > 0 ? JSON.parse(body) : null)
      headers.push(request.headers)
      const behavior = script.shift()
      if (!behavior) { response.writeHead(500).end('mock script exhausted'); return }
      if (behavior.kind === 'http-error') {
        response.writeHead(behavior.status, { 'content-type': 'application/json' }).end(behavior.body)
        return
      }
      if (behavior.kind === 'json') {
        response.writeHead(behavior.status ?? 200, { 'content-type': 'application/json' }).end(JSON.stringify(behavior.body))
        return
      }
      response.writeHead(200, { 'content-type': 'text/event-stream' })
      const write = (index: number): void => {
        if (index >= behavior.events.length) {
          if (behavior.kind === 'sse') response.end()
          else response.destroy()
          return
        }
        response.write(behavior.events[index])
        setTimeout(() => { write(index + 1) }, behavior.kind === 'sse' ? behavior.delayMs ?? 0 : 5)
      }
      write(0)
    })
  })
  servers.push(server)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('no port')
  return {
    url: `http://127.0.0.1:${address.port}`,
    requests, headers, script,
    close: () => new Promise(resolve => server.close(() => resolve())),
  }
}
```

`packages/llm/llm-dsv/tests/client.spec.ts`（镜像 llm-deepseek adapter.spec.ts 的装配方式）：

```ts
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import { AttachmentStore, type ImageAttachmentRef } from '@deepseek-ai/dsh-attachment'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import { LlmRuntime } from '@deepseek-ai/dsh-llm'
import { DsvClient } from '../src/client.ts'
import { resolveDsvOptions } from '../src/config.ts'
import { closeMockServers, dsvVisionStream, mockServer } from './mock-server.ts'

class FakeAttachments extends AttachmentStore {
  readonly imageLimits = { maxImageBytes: 1024, maxImagesPerMessage: 4, maxMessageImageBytes: 4096, maxImagePixels: 4096, mediaTypes: ['image/png'] }
  validateImage(): Promise<void> { return Promise.resolve() }
  saveImage(): Promise<ImageAttachmentRef> { throw new Error('unused') }
  readImage(ref: ImageAttachmentRef) {
    return Promise.resolve({ ref, data: Buffer.from([0x89, 0x50, 0x4e, 0x47]) })
  }
}

let testHome: string
beforeEach(() => {
  testHome = mkdtempSync(join(tmpdir(), 'dsh-llm-dsv-'))
  vi.stubEnv('DSH_HOME', testHome)
})
afterEach(async () => {
  await closeMockServers()
  vi.unstubAllEnvs()
  rmSync(testHome, { recursive: true, force: true })
})

describe('DsvClient', () => {
  it('posts the serialized request with the DSV public key and streams chunks', async () => {
    const server = await mockServer([{ kind: 'sse', events: dsvVisionStream() }])
    const client = new DsvClient({
      options: () => resolveDsvOptions({ baseURL: server.url, mode: 'auto' }),
      resolveApiKey: async () => 'dsv-public-key',
      streamIdleTimeoutMs: 30_000,
    })
    const message = createUserMessage({
      content: [
        { type: 'text', text: '这张图里有什么?' },
        { type: 'image', attachment: { attachmentId: 'a-1' as ImageAttachmentRef['attachmentId'], mediaType: 'image/png', bytes: 4, width: 1, height: 1 } },
      ],
      source: { kind: 'user' },
    })
    const chunks: unknown[] = []
    for await (const chunk of client.stream(
      { provider: 'deepseek-official', model: 'deepseek-v4-flash', messages: [message], sessionId: 's-1' as never },
      new FakeAttachments(),
    )) chunks.push(chunk)
    expect(server.requests).toHaveLength(1)
    const body = server.requests[0] as { model: string; vision: { mode: string; include_analysis: boolean }; messages: unknown[] }
    expect(body.model).toBe('deepseek-v4-flash')
    expect(body.vision).toEqual({ mode: 'auto', include_analysis: true })
    expect(server.headers[0]?.authorization).toBe('Bearer dsv-public-key')
    expect(chunks.at(-1)).toEqual({ type: 'finish', reason: { kind: 'stop' } })
  })

  it('maps HTTP 401 to an AUTH LlmError', async () => {
    const server = await mockServer([{ kind: 'http-error', status: 401, body: '{"error":{"message":"invalid key"}}' }])
    const client = new DsvClient({
      options: () => resolveDsvOptions({ baseURL: server.url }),
      resolveApiKey: async () => 'bad',
      streamIdleTimeoutMs: 30_000,
    })
    await expect(async () => {
      for await (const _ of client.stream({ provider: 'p', model: 'm', messages: [] }, new FakeAttachments())) { /* noop */ }
    }).rejects.toMatchObject({ code: 'AUTH' })
  })

  it('throws MISSING_CREDENTIAL without a key', async () => {
    const client = new DsvClient({
      options: () => resolveDsvOptions({ baseURL: 'http://127.0.0.1:9' }),
      resolveApiKey: async () => { throw new Error('never called') },
      streamIdleTimeoutMs: 30_000,
    })
    const noKey = new DsvClient({
      options: () => resolveDsvOptions({ baseURL: 'http://127.0.0.1:9' }),
      resolveApiKey: async () => { throw Object.assign(new Error('no key'), { code: 'MISSING_CREDENTIAL' }) },
      streamIdleTimeoutMs: 30_000,
    })
    void client
    await expect(async () => {
      for await (const _ of noKey.stream({ provider: 'p', model: 'm', messages: [] }, new FakeAttachments())) { /* noop */ }
    }).rejects.toMatchObject({ code: 'MISSING_CREDENTIAL' })
  })
})

void LlmRuntime
void credentialRef
void Context
```

`packages/llm/llm-dsv/tests/route.spec.ts`：

```ts
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import { BlockAssembler, createUserMessage, LlmError } from '@deepseek-ai/dsh-llm'
import { settingsNamespace } from '@deepseek-ai/dsh-settings'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import * as LlmDsv from '@deepseek-ai/dsh-llm-dsv'
import { mockServer, closeMockServers, dsvVisionStream } from './mock-server.ts'

const NS = settingsNamespace('llm-dsv')
const KEY_REF = credentialRef('DEEPSEE_DSV_API_KEY')

let testHome: string
let ctx: Context | undefined

beforeEach(() => {
  testHome = mkdtempSync(join(tmpdir(), 'dsh-llm-dsv-route-'))
  vi.stubEnv('DSH_HOME', testHome)
})
afterEach(async () => {
  await ctx?.fiber.dispose()
  ctx = undefined
  await closeMockServers()
  vi.unstubAllEnvs()
  rmSync(testHome, { recursive: true, force: true })
})

async function harness(baseURL: string, config: object = {}, plugins: unknown[] = []) {
  vi.stubEnv('DEEPSEE_DSV_API_KEY', 'dsv-key')
  ctx = new Context()
  for (const plugin of plugins) await ctx.plugin(plugin as never)
  await ctx.plugin(LlmDsv, { baseURL, ...config })
  return ctx
}

describe('llm/stream routing', () => {
  it('calls next() for image-free requests and never opens a DSV connection', async () => {
    const server = await mockServer([])
    const context = await harness(server.url)
    await expect(async () => {
      for await (const _ of context.llm.stream({ provider: 'deepseek-official', model: 'm', messages: [] })) { /* noop */ }
    }).rejects.toMatchObject({ code: 'NO_ADAPTER' })  // next() 走原适配器路径，未注册 adapter
    expect(server.requests).toHaveLength(0)
  })

  it('short-circuits image requests to the DSV gateway and assembles the vision block', async () => {
    const server = await mockServer([{ kind: 'sse', events: dsvVisionStream('图中有一只猫', '这是一只猫') }])
    const context = await harness(server.url)
    const message = createUserMessage({
      content: [{ type: 'image', attachment: { attachmentId: 'a-1' as never, mediaType: 'image/png', bytes: 4, width: 1, height: 1 } }],
      source: { kind: 'user' },
    })
    const assembler = new BlockAssembler()
    for await (const chunk of context.llm.stream({ provider: 'deepseek-official', model: 'deepseek-v4-flash', messages: [message] })) {
      assembler.push(chunk)
    }
    const blocks = assembler.blocks()
    expect(blocks[0]).toMatchObject({ type: 'vision-analysis', text: '图中有一只猫' })
    expect(blocks[1]).toEqual({ type: 'reasoning', text: '看图' })
    expect(blocks[2]).toEqual({ type: 'text', text: '这是一只猫' })
    expect(assembler.finish).toEqual({ kind: 'stop' })
    expect(server.requests).toHaveLength(1)
  })

  it('passes auxiliary purposes through to next()', async () => {
    const server = await mockServer([])
    const context = await harness(server.url)
    const message = createUserMessage({
      content: [{ type: 'image', attachment: { attachmentId: 'a-1' as never, mediaType: 'image/png', bytes: 4, width: 1, height: 1 } }],
      source: { kind: 'user' },
    })
    await expect(async () => {
      for await (const _ of context.llm.stream({ provider: 'deepseek-official', model: 'm', messages: [message], purpose: 'session-title' })) { /* noop */ }
    }).rejects.toMatchObject({ code: 'NO_ADAPTER' })
    expect(server.requests).toHaveLength(0)
  })

  it('aborts the HTTP request and yields no success chunks when cancelled', async () => {
    const server = await mockServer([{ kind: 'sse', events: dsvVisionStream(), delayMs: 50 }])
    const context = await harness(server.url)
    const controller = new AbortController()
    const message = createUserMessage({
      content: [{ type: 'image', attachment: { attachmentId: 'a-1' as never, mediaType: 'image/png', bytes: 4, width: 1, height: 1 } }],
      source: { kind: 'user' },
    })
    const stream = context.llm.stream({ provider: 'deepseek-official', model: 'm', messages: [message], signal: controller.signal })
    const iterator = stream[Symbol.asyncIterator]()
    const first = await iterator.next()
    expect(first.done).toBe(false)
    controller.abort()
    await expect(iterator.next()).rejects.toMatchObject({ code: 'ABORTED' })
  })
})
```

> 注：`route.spec.ts` 依赖 Task 5 的 `installSettingsSection` 与凭证解析，若本任务先行实现 index.ts 中不含设置的部分，则路由测试可先以 `resolveDsvOptions(config)` + 环境变量（`launchEnvironmentOf` 回退）跑通；`MISSING_CREDENTIAL` 断言位置随实现调整。图片附件测试需在 harness 中挂真实/桩 `attachments` 服务（`AttachmentStore` 子类，见 client.spec.ts 的 FakeAttachments），若 `inject: ['attachments']` 使插件等待服务，测试需先 `await ctx.plugin(FakeAttachments)`。

- [ ] **Step 2: 运行测试确认失败**

```bash
pnpm exec vitest run packages/llm/llm-dsv/tests/client.spec.ts packages/llm/llm-dsv/tests/route.spec.ts
```

期望：FAIL（client.ts 不存在）。

- [ ] **Step 3: 写 client.ts（镜像 llm-deepseek adapter.ts 的流模式）**

`packages/llm/llm-dsv/src/client.ts`：

```ts
/**
 * DSV gateway client: one fetch + SSE stream per request, mirroring the
 * deepseek adapter's transport pattern (per-request resolution, fused
 * AbortSignal, idle watchdog, error mapping). The client only consumes the
 * public DSV protocol; vision provider keys never appear here.
 * @module dsh-llm-dsv/client
 */

import type { AttachmentStore } from '@deepseek-ai/dsh-attachment'
import { LlmError } from '@deepseek-ai/dsh-llm'
import type { GenerateOptions, StreamChunk } from '@deepseek-ai/dsh-llm'
import { idleWatchdog, timeoutOf } from '@deepseek-ai/dsh-timeout'
import type { ResolvedDsvOptions } from './config.ts'
import { serializeRequest } from './serialize.ts'
import { parseSse } from './sse.ts'
import { translateDsv } from './translate.ts'

/** Default maximum idle interval while one DSV stream read is outstanding. */
export const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 300_000
const STREAM_IDLE_TIMEOUT_CODE = 'DSV_STREAM_IDLE_TIMEOUT'
/** Stable code for a missing DSV public key. */
export const MISSING_CREDENTIAL_CODE = 'MISSING_CREDENTIAL'

/** Map a DSV HTTP status to a stable provider-neutral code. */
export function httpErrorCode(status: number): string {
  if (status === 401 || status === 403) return 'AUTH'
  if (status === 429) return 'RATE_LIMIT'
  if (status === 400) return 'INVALID_REQUEST'
  if (status >= 500) return 'SERVER'
  return `HTTP_${status}`
}

/** Constructor options for {@link DsvClient}. */
export interface DsvClientOptions {
  /** Current validated connection facts; called once per operation. */
  options: () => ResolvedDsvOptions
  /** Resolve the DSV public key for one request; throws `MISSING_CREDENTIAL` when unset. */
  resolveApiKey: () => Promise<string>
  /** Maximum provider idle time while one stream read is outstanding. */
  streamIdleTimeoutMs: number
}

/**
 * Transport-only DSV client. `stream` drives the full harness stream;
 * `request` exposes the raw translated chunks for non-stream callers
 * (the follow-up tool passes `stream: false`).
 */
export class DsvClient {
  constructor(private readonly config: DsvClientOptions) {}

  /**
   * Stream one DSV call as harness chunks.
   * @param request - the harness request (messages may carry images).
   * @param attachments - durable byte resolver for image references.
   * @returns the translated chunk stream.
   */
  async * stream(request: GenerateOptions, attachments: AttachmentStore): AsyncGenerator<StreamChunk> {
    const resolved = this.config.options()
    const apiKey = await this.config.resolveApiKey()
    const consumer = new AbortController()
    const upstream = request.signal === undefined
      ? consumer.signal
      : AbortSignal.any([request.signal, consumer.signal])
    using watchdog = idleWatchdog(upstream, this.config.streamIdleTimeoutMs, STREAM_IDLE_TIMEOUT_CODE)
    const iterator = this.request(request, resolved, apiKey, attachments, watchdog.signal, true)[Symbol.asyncIterator]()
    let exhausted = false
    try {
      while (true) {
        const result = await watchdog.next(iterator)
        if (result.done) { exhausted = true; return }
        yield result.value
      }
    } catch (error: unknown) {
      if (timeoutOf(watchdog.signal, STREAM_IDLE_TIMEOUT_CODE) !== undefined) {
        throw new LlmError(`DSV stream idle timeout after ${this.config.streamIdleTimeoutMs}ms`, 'TIMEOUT', { cause: error })
      }
      if (request.signal?.aborted) {
        throw new LlmError('DSV request aborted by caller', 'ABORTED', { cause: error })
      }
      if (error instanceof LlmError) throw error
      throw new LlmError(`DSV request to ${resolved.baseURL} failed`, 'TRANSPORT', { cause: error })
    } finally {
      consumer.abort('DSV stream consumer stopped')
      if (!exhausted && iterator.return !== undefined) {
        try {
          await iterator.return()
        } catch {
          // The consumer controller already owns termination.
        }
      }
    }
  }

  /**
   * One DSV HTTP exchange, translated into chunks. Non-stream responses are
   * surfaced as a single synthetic `vision.completed`/finish sequence.
   * @internal
   */
  private async * request(
    request: GenerateOptions,
    resolved: ResolvedDsvOptions,
    apiKey: string,
    attachments: AttachmentStore,
    signal: AbortSignal,
    stream: boolean,
  ): AsyncGenerator<StreamChunk> {
    const body = await serializeRequest(request, resolved, attachments)
    const response = await fetch(`${resolved.baseURL}/v1/dsv`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({ ...body, stream }),
      signal,
    })
    if (!response.ok) {
      // Error text never echoes secrets: extract only the server's error.message.
      const raw = await response.text().catch(() => '')
      let message = `DSV request failed with status ${response.status}`
      try {
        const parsed = JSON.parse(raw) as { error?: { message?: string } }
        if (typeof parsed.error?.message === 'string' && parsed.error.message.length > 0) {
          message = parsed.error.message
        }
      } catch {
        // Non-JSON error bodies fall back to the status message.
      }
      throw new LlmError(message, httpErrorCode(response.status), { status: response.status })
    }
    if (response.body === null) {
      throw new LlmError('DSV response has no body', 'MALFORMED_RESPONSE')
    }
    if (stream) {
      yield * translateDsv(parseSse(response.body))
    } else {
      // Non-stream envelope: emit one vision block then a stop finish.
      const envelope = await response.json() as {
        vision?: { analysis?: string; mode?: string; backend?: string; model?: string; latency_ms?: number; cache_hit?: boolean; trace_id?: string }
        answer?: { text?: string }
        error?: { stage?: string; message?: string }
      }
      if (envelope.error !== undefined) {
        throw new LlmError(envelope.error.message ?? 'DSV request failed', 'VISION_FAILED')
      }
      yield { type: 'block-start', index: 0, blockType: 'vision-analysis' }
      yield {
        type: 'block-end', index: 0,
        block: {
          type: 'vision-analysis',
          text: envelope.vision?.analysis ?? '',
          metadata: {
            backend: envelope.vision?.backend ?? '',
            model: envelope.vision?.model ?? '',
            mode: envelope.vision?.mode === 'ui' || envelope.vision?.mode === 'general' ? envelope.vision.mode : 'auto',
            durationMs: envelope.vision?.latency_ms ?? 0,
            cacheHit: envelope.vision?.cache_hit ?? false,
            ...envelope.vision?.trace_id !== undefined ? { traceId: envelope.vision.trace_id } : {},
          },
        },
      }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
}
```

- [ ] **Step 4: 完成 index.ts 的 apply**

`packages/llm/llm-dsv/src/index.ts` 完整内容（替换骨架 body）：

```ts
/**
 * Route image-bearing LLM requests to the DeepSee Vision (DSV) gateway.
 * Connection facts (gateway URL, public key reference, mode) resolve per
 * request from live settings and the credential seam, so rotation reaches
 * the next request without restart; an in-flight stream keeps the facts it
 * started with. Image-free requests and auxiliary purposes fall through to
 * the original provider via `next()`.
 * @module @deepseek-ai/dsh-llm-dsv
 */

import type { Context } from '@deepseek-ai/cordis'
import { assertUsableApiKey, contentHasImage, LlmError } from '@deepseek-ai/dsh-llm'
import type { GenerateOptions, StreamChunk } from '@deepseek-ai/dsh-llm'
import { launchEnvironmentOf } from '@deepseek-ai/dsh-launch-environment'
import { installSettingsSection } from '@deepseek-ai/dsh-settings'
import { DsvClient, DEFAULT_STREAM_IDLE_TIMEOUT_MS, MISSING_CREDENTIAL_CODE } from './client.ts'
import { Config, NS, resolveDsvOptions, type Config as ConfigType } from './config.ts'

export {
  DEFAULT_API_KEY_ENV,
  DEFAULT_BASE_URL,
  DEFAULT_STREAM_IDLE_TIMEOUT_MS,
  MISSING_CREDENTIAL_CODE,
  NS,
  VISION_MODES,
  Config,
  httpErrorCode,
  resolveDsvOptions,
  serializeRequest,
} from './config.ts'
export type { Config as DsvConfig, ResolvedDsvOptions, VisionMode } from './config.ts'
export type { VisionAnalysisBlock, VisionAnalysisMetadata } from './types.ts'

/** Plugin name used by cordis.yml rows. */
export const name = 'llm-dsv'
/** Hard dependencies: the LLM seam to route, the tool registry, and the attachment store for image bytes. */
export const inject = ['llm', 'tools', 'attachments']

export { DsvClient } from './client.ts'

/**
 * Mount the DSV route and register the vision follow-up tool.
 * @param ctx - Cordis context.
 * @param config - entry-config; the user settings layer overrides it live.
 */
export function apply(ctx: Context, config: ConfigType): void {
  let current: () => ConfigType = () => config
  installSettingsSection(ctx, NS, Config, config, {
    setSource: (source) => { current = source },
  })

  const resolveApiKey = async (): Promise<string> => {
    const ref = resolveDsvOptions(current()).apiKeyRef
    const credentials = ctx.get('credentials')
    if (credentials !== undefined) {
      const hit = await credentials.resolve(ref)
      if (hit !== undefined) return assertUsableApiKey(hit.value, 'llm-dsv', ref)
    }
    const ambient = launchEnvironmentOf(ctx).get(ref)
    if (ambient !== undefined && ambient.value.length > 0) return ambient.value
    throw new LlmError(
      `llm-dsv: no DSV public key; store ${ref} through the credentials service, or export ${ref} in the launching environment`,
      MISSING_CREDENTIAL_CODE,
    )
  }

  const client = new DsvClient({
    options: () => resolveDsvOptions(current()),
    resolveApiKey,
    streamIdleTimeoutMs: DEFAULT_STREAM_IDLE_TIMEOUT_MS,
  })

  ctx.on('llm/stream', (options: GenerateOptions, next: () => AsyncIterable<StreamChunk>): AsyncIterable<StreamChunk> => {
    if (options.purpose !== undefined || !contentHasImage(options.messages)) return next()
    return client.stream(options, ctx.attachments)
  })

  // The deepsee_vision_detail follow-up tool registers in a later task.
}
```

> 注：`exports` 中 `httpErrorCode` 等如与 index 顶部 re-export 冲突，以实际导出清单为准（Task 2 Step 3 的 re-export 行与本步合并去重）。`DsvClient.request` 的 `stream:false` 路径供 Task 5 工具使用；Task 5 会为工具暴露一个便捷方法（如 `visionDetail(...)`）。

- [ ] **Step 5: 运行测试确认通过**

```bash
pnpm exec vitest run packages/llm/llm-dsv/tests/client.spec.ts packages/llm/llm-dsv/tests/route.spec.ts
```

期望：PASS（route.spec 中附件服务需以 FakeAttachments 桩挂载，见 Step 1 注）。

- [ ] **Step 6: Commit**

```bash
git add packages/llm/llm-dsv
git commit -m "feat(llm-dsv): add DSV client and route image requests through the llm/stream waterfall"
```

---

### Task 5: deepsee_vision_detail 工具

**Files:**
- Create: `packages/llm/llm-dsv/src/tool.ts`
- Modify: `packages/llm/llm-dsv/src/index.ts`（apply 内注册工具）
- Test: `packages/llm/llm-dsv/tests/tool.spec.ts`

**Interfaces:**
- Produces: `registerVisionDetailTool(ctx, client, options)`：用 `defineTool` + `ctx.tools.register` 注册 `deepsee_vision_detail`（参数 `{ question: string }`；canonical 输出 `{ question, analysis, exhausted }`；`output.render` 生成模型可见文本；工具从 `exec.agent.session.events` 扫描最近的图片引用与已有 `vision-analysis` 块，复用 durable attachment 取原图，直连 DSV 非流式补充分析；会话内 `vision-analysis` 块 ≥ 2 时 `exhausted: true` 不再调用 DSV）。
- Consumes: `defineTool`/`ToolDefinition`（`@deepseek-ai/dsh-tools`）；`Session`/`SessionEvent` 类型（`@deepseek-ai/dsh-session`）；`DsvClient`；`ResolvedDsvOptions`。

- [ ] **Step 1: 写失败测试**

`packages/llm/llm-dsv/tests/tool.spec.ts`：

```ts
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import { AttachmentStore, type ImageAttachmentRef } from '@deepseek-ai/dsh-attachment'
import { createAssistantMessage, createUserMessage } from '@deepseek-ai/dsh-llm'
import { Session } from '@deepseek-ai/dsh-session'
import { defineVisionDetailTool, MAX_VISION_FOLLOWUPS } from '../src/tool.ts'
import { DsvClient } from '../src/client.ts'
import { resolveDsvOptions } from '../src/config.ts'
import { closeMockServers, dsvVisionEnvelope, mockServer } from './mock-server.ts'

class FakeAttachments extends AttachmentStore {
  readonly imageLimits = { maxImageBytes: 1024, maxImagesPerMessage: 4, maxMessageImageBytes: 4096, maxImagePixels: 4096, mediaTypes: ['image/png'] }
  validateImage(): Promise<void> { return Promise.resolve() }
  saveImage(): Promise<ImageAttachmentRef> { throw new Error('unused') }
  readImage(ref: ImageAttachmentRef) { return Promise.resolve({ ref, data: Buffer.from([0x89, 0x50, 0x4e, 0x47]) }) }
}

let testHome: string
beforeEach(() => { testHome = mkdtempSync(join(tmpdir(), 'dsh-llm-dsv-tool-')) })
afterEach(async () => { await closeMockServers(); vi.unstubAllEnvs(); rmSync(testHome, { recursive: true, force: true }) })

const IMAGE_REF: ImageAttachmentRef = { attachmentId: 'a-1' as ImageAttachmentRef['attachmentId'], mediaType: 'image/png', bytes: 4, width: 1, height: 1 }

function sessionWith(events: unknown[]): Session {
  return Session.create('sess-1' as never, events as never)
}

describe('deepsee_vision_detail', () => {
  it('calls DSV with the original image and the initial analysis, returning the supplementary analysis', async () => {
    const server = await mockServer([{ kind: 'json', body: dsvVisionEnvelope('补充分析：黑猫') }])
    const client = new DsvClient({
      options: () => resolveDsvOptions({ baseURL: server.url }),
      resolveApiKey: async () => 'key',
      streamIdleTimeoutMs: 30_000,
    })
    const tool = defineVisionDetailTool({ options: () => resolveDsvOptions({ baseURL: server.url }), client, attachments: new FakeAttachments() })
    const session = sessionWith([
      { type: 'user/message', seq: 0, time: 1, data: { content: [{ type: 'image', attachment: IMAGE_REF }], role: 'user', source: { kind: 'user' } } },
      { type: 'assistant/message', seq: 1, time: 2, data: { message: { content: [
        { type: 'vision-analysis', text: '图中有一只猫', metadata: { backend: 'b', model: 'm', mode: 'auto', durationMs: 1, cacheHit: false } },
      ], role: 'assistant', source: { kind: 'model', provider: 'p', model: 'm' } } } },
    ])
    const agent = { session }
    const value = await tool.execute({ question: '什么颜色?' }, { agent } as never)
    expect(value).toEqual({ question: '什么颜色?', analysis: '补充分析：黑猫', exhausted: false })
    const body = server.requests[0] as { stream: boolean; messages: { role: string; content: { type: string }[] }[]; vision: { include_analysis: boolean } }
    expect(body.stream).toBe(false)
    expect(body.messages[0]?.content).toContainEqual({ type: 'image', source: { type: 'base64', media_type: 'image/png', data: Buffer.from([0x89, 0x50, 0x4e, 0x47]).toString('base64') } })
    expect(body.messages[0]?.content[0]).toMatchObject({ type: 'text' })
    expect(body.vision).toEqual({ mode: 'auto', include_analysis: true })
  })

  it('returns exhausted without calling DSV once the follow-up limit is reached', async () => {
    const server = await mockServer([])
    const tool = defineVisionDetailTool({
      options: () => resolveDsvOptions({ baseURL: server.url }),
      client: new DsvClient({ options: () => resolveDsvOptions({ baseURL: server.url }), resolveApiKey: async () => 'k', streamIdleTimeoutMs: 30_000 }),
      attachments: new FakeAttachments(),
    })
    const visionBlock = { type: 'vision-analysis', text: 'x', metadata: { backend: 'b', model: 'm', mode: 'auto', durationMs: 1, cacheHit: false } }
    const events = Array.from({ length: MAX_VISION_FOLLOWUPS }, (_, index) => ({
      type: 'assistant/message', seq: index, time: index + 1,
      data: { message: { content: [visionBlock], role: 'assistant', source: { kind: 'model', provider: 'p', model: 'm' } } },
    }))
    const session = sessionWith(events)
    const value = await tool.execute({ question: '再问' }, { agent: { session } } as never)
    expect(value).toEqual({ question: '再问', analysis: '', exhausted: true })
    expect(server.requests).toHaveLength(0)
  })

  it('throws when no agent context is available', async () => {
    const tool = defineVisionDetailTool({
      options: () => resolveDsvOptions({}),
      client: new DsvClient({ options: () => resolveDsvOptions({}), resolveApiKey: async () => 'k', streamIdleTimeoutMs: 30_000 }),
      attachments: new FakeAttachments(),
    })
    await expect(tool.execute({ question: 'q' }, {} as never)).rejects.toThrow(/agent/)
  })

  it('renders model-facing text from the canonical value', () => {
    const tool = defineVisionDetailTool({
      options: () => resolveDsvOptions({}),
      client: new DsvClient({ options: () => resolveDsvOptions({}), resolveApiKey: async () => 'k', streamIdleTimeoutMs: 30_000 }),
      attachments: new FakeAttachments(),
    })
    const blocks = tool.output.render({ question: '颜色?' }, { question: '颜色?', analysis: '黑色', exhausted: false })
    expect(blocks[0]).toMatchObject({ type: 'text' })
    expect((blocks[0] as { text: string }).text).toContain('黑色')
  })
})

void createAssistantMessage
void createUserMessage
void Context
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pnpm exec vitest run packages/llm/llm-dsv/tests/tool.spec.ts
```

期望：FAIL（tool.ts 不存在）。

- [ ] **Step 3: 写 tool.ts**

`packages/llm/llm-dsv/src/tool.ts`：

```ts
/**
 * `deepsee_vision_detail`: the model-requested follow-up path. The tool
 * re-reads the original image from the durable attachment service, sends it
 * with the initial vision analysis and the follow-up question to the DSV
 * gateway (non-stream), and returns the supplementary analysis. It never
 * triggers on keywords, never executes DSV-side tools, and stops after
 * {@link MAX_VISION_FOLLOWUPS} rounds per session.
 * @module dsh-llm-dsv/tool
 */

import type { AttachmentStore, ImageAttachmentRef } from '@deepseek-ai/dsh-attachment'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import type { DsvClient } from './client.ts'
import type { ResolvedDsvOptions } from './config.ts'
import type { VisionAnalysisBlock } from './types.ts'

/** Maximum vision follow-up rounds per session (spec: 最多追问两轮). */
export const MAX_VISION_FOLLOWUPS = 2

/** Read the image refs of one user message's direct content. */
function imageRefsOf(content: readonly { type: string; attachment?: ImageAttachmentRef }[]): ImageAttachmentRef[] {
  return content
    .filter(block => block.type === 'image' && block.attachment !== undefined)
    .map(block => block.attachment as ImageAttachmentRef)
}

/** Scan the session log for the latest image refs, the latest analysis, and the follow-up count. */
function scanSession(events: readonly SessionEvent[]): { imageRefs: ImageAttachmentRef[]; initialAnalysis: string; rounds: number } {
  let imageRefs: ImageAttachmentRef[] = []
  let initialAnalysis = ''
  let rounds = 0
  for (const event of events) {
    if (event.type === 'user/message') {
      const refs = imageRefsOf(event.data.content as readonly { type: string; attachment?: ImageAttachmentRef }[])
      if (refs.length > 0) imageRefs = refs
    } else if (event.type === 'assistant/message') {
      for (const block of event.data.message.content) {
        if (block.type === 'vision-analysis') {
          rounds += 1
          initialAnalysis = (block as VisionAnalysisBlock).text
        }
      }
    }
  }
  return { imageRefs, initialAnalysis, rounds }
}

/** Tool construction inputs, resolved from the plugin fiber. */
export interface VisionDetailToolInput {
  options: () => ResolvedDsvOptions
  client: DsvClient
  attachments: AttachmentStore
}

/**
 * Build the `deepsee_vision_detail` tool definition.
 * @param input - live settings thunk, DSV client, and the attachment store.
 * @returns the tool definition, ready for `ctx.tools.register`.
 */
export function defineVisionDetailTool(input: VisionDetailToolInput): ToolDefinition {
  return defineTool({
    name: 'deepsee_vision_detail',
    description: '对当前会话中的图片发起一次视觉追问：提交一个针对图片内容的具体问题，返回视觉模型对该问题的补充分析。仅在初轮识图信息不足时使用；同一轮对话最多追问两次。',
    parameters: {
      question: { type: 'string', required: true, description: '针对图片内容的具体追问问题' },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          question: { type: 'string' },
          analysis: { type: 'string' },
          exhausted: { type: 'boolean' },
        },
        required: ['question', 'analysis', 'exhausted'],
      },
      render: (args, value) => [{
        type: 'text',
        text: value.exhausted
          ? `视觉追问已达上限（${MAX_VISION_FOLLOWUPS} 轮），请基于已有上下文回答。`
          : `视觉补充分析（追问：${value.question}）：\n${value.analysis}`,
      }],
    },
    async execute(args, exec) {
      const { agent } = exec
      if (agent === undefined) {
        throw new Error('deepsee_vision_detail: 当前调用没有 agent 上下文，无法读取会话图片')
      }
      const session: Session = agent.session
      const { imageRefs, initialAnalysis, rounds } = scanSession(session.events)
      if (rounds >= MAX_VISION_FOLLOWUPS) {
        return { question: args.question, analysis: '', exhausted: true }
      }
      const attachments = input.attachments
      const stored = []
      for (const ref of imageRefs) {
        stored.push(await attachments.readImage(ref, exec.signal))
      }
      if (stored.length === 0) {
        throw new Error('deepsee_vision_detail: 会话中没有可用的图片附件')
      }
      const resolved = input.options()
      const envelope = await input.client.visionDetail({
        question: args.question,
        initialAnalysis,
        images: stored.map(item => ({ data: item.data, mediaType: item.ref.mediaType })),
        mode: resolved.mode,
        signal: exec.signal,
      })
      return { question: args.question, analysis: envelope.analysis, exhausted: false }
    },
  })
}
```

`client.ts` 增加非流式便捷方法（供工具使用）：

```ts
/** One vision follow-up request payload. */
export interface VisionDetailRequest {
  question: string
  initialAnalysis: string
  images: { data: Uint8Array; mediaType: string }[]
  mode: ResolvedDsvOptions['mode']
  signal?: AbortSignal
}

/** Vision follow-up result. */
export interface VisionDetailResult { analysis: string }

// 在 DsvClient 类内新增：
async visionDetail(request: VisionDetailRequest): Promise<VisionDetailResult> {
  const resolved = this.config.options()
  const apiKey = await this.config.resolveApiKey()
  const body = {
    stream: false,
    messages: [{
      role: 'user',
      content: [
        { type: 'text', text: `初始视觉分析：\n${request.initialAnalysis}\n\n追问问题：${request.question}` },
        ...request.images.map(image => ({
          type: 'image',
          source: { type: 'base64', media_type: image.mediaType, data: Buffer.from(image.data).toString('base64') },
        })),
      ],
    }],
    vision: { mode: request.mode, include_analysis: true },
  }
  const response = await fetch(`${resolved.baseURL}/v1/dsv`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${apiKey}` },
    body: JSON.stringify(body),
    signal: request.signal,
  })
  if (!response.ok) {
    const raw = await response.text().catch(() => '')
    let message = `DSV request failed with status ${response.status}`
    try {
      const parsed = JSON.parse(raw) as { error?: { message?: string } }
      if (typeof parsed.error?.message === 'string' && parsed.error.message.length > 0) message = parsed.error.message
    } catch { /* fall back to the status message */ }
    throw new LlmError(message, httpErrorCode(response.status), { status: response.status })
  }
  const envelope = await response.json() as {
    vision?: { analysis?: string }
    answer?: { text?: string }
    error?: { message?: string }
  }
  if (envelope.error !== undefined) {
    throw new LlmError(envelope.error.message ?? 'DSV request failed', 'VISION_FAILED')
  }
  return { analysis: envelope.vision?.analysis ?? envelope.answer?.text ?? '' }
}
```

- [ ] **Step 4: index.ts 注册工具**

`packages/llm/llm-dsv/src/index.ts` 的 apply 末尾追加：

```ts
  ctx.tools.register(defineVisionDetailTool({
    options: () => resolveDsvOptions(current()),
    client,
    attachments: ctx.attachments,
  }))
```

并在文件顶部 import `defineVisionDetailTool`；`exports`/re-export 增加 `defineVisionDetailTool` 与 `MAX_VISION_FOLLOWUPS`。

- [ ] **Step 5: 运行测试确认通过**

```bash
pnpm exec vitest run packages/llm/llm-dsv/tests/tool.spec.ts
```

期望：PASS。`Session.create` 的事件参数形状以 `packages/core/session/src/index.ts:482` 为准；若 `session.events` 类型不含 `data.content` 的宽松形状，在 `scanSession` 内做最小结构化读取（只取 `type`/`content`/`attachment`/`text` 叶子字段，不做整对象拷贝）。

- [ ] **Step 6: Commit**

```bash
git add packages/llm/llm-dsv
git commit -m "feat(llm-dsv): add the deepsee_vision_detail follow-up tool with a two-round limit"
```

---

### Task 6: 统一 model-visible 投影过滤（core/session）

**Files:**
- Modify: `packages/core/session/src/surface.ts`（`deriveEventMessage` 的 `assistant/message` 分支）
- Test: `packages/core/session/tests/`（新增或扩展现有 surface 测试）

**Interfaces:**
- Produces: `deriveEventMessage` 对 assistant/message 的投影规则：`vision-analysis` 块被过滤；过滤后为空 → `null`；无 vision 块时保持原共享冻结消息（不新建对象）。
- Consumes: 无新增依赖（用 `Object.freeze` 保持 browser-safe，不引入 dsh-llm 运行时值导入）。

- [ ] **Step 1: 写失败测试**

在 `packages/core/session/tests/` 下新建 `derive-vision-filter.spec.ts`（若已有 surface 测试文件则并入）：

```ts
import { describe, expect, it } from 'vitest'
import { createMessage, type Message } from '@deepseek-ai/dsh-llm'
import { deriveEventMessage } from '../src/surface.ts'

const visionBlock = {
  type: 'vision-analysis',
  text: '图中有一只猫',
  metadata: { backend: 'openai_compatible', model: 'qwen-vl-max', mode: 'auto', durationMs: 1, cacheHit: false },
}

function assistantEvent(message: Message) {
  return {
    type: 'assistant/message', seq: 1, time: 1,
    data: { turn: 0, step: 0, message },
    surfaceOp: 'append', sourceEventSeqs: [0],
  } as const
}

describe('deriveEventMessage assistant projection', () => {
  it('filters vision-analysis blocks out of the model-visible message', () => {
    const message = createMessage({
      role: 'assistant',
      content: [visionBlock, { type: 'text', text: '这是一只猫' }],
      source: { kind: 'model', provider: 'p', model: 'm' },
    } as never)
    const derived = deriveEventMessage(assistantEvent(message))
    expect(derived?.content).toEqual([{ type: 'text', text: '这是一只猫' }])
  })

  it('keeps the original shared message when no vision blocks exist', () => {
    const message = createMessage({
      role: 'assistant',
      content: [{ type: 'text', text: 'hi' }],
      source: { kind: 'model', provider: 'p', model: 'm' },
    } as never)
    expect(deriveEventMessage(assistantEvent(message))).toBe(message)
  })

  it('derives null when only vision blocks remain after filtering', () => {
    const message = createMessage({
      role: 'assistant',
      content: [visionBlock],
      source: { kind: 'model', provider: 'p', model: 'm' },
    } as never)
    expect(deriveEventMessage(assistantEvent(message))).toBeNull()
  })

  it('leaves tool-call blocks intact', () => {
    const message = createMessage({
      role: 'assistant',
      content: [visionBlock, { type: 'tool-call', id: 'c-1', name: 'x', arguments: '{}' }],
      source: { kind: 'model', provider: 'p', model: 'm' },
    } as never)
    expect(deriveEventMessage(assistantEvent(message))?.content).toEqual([
      { type: 'tool-call', id: 'c-1', name: 'x', arguments: '{}' },
    ])
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pnpm exec vitest run packages/core/session/tests/derive-vision-filter.spec.ts
```

期望：FAIL（第一个用例得到含 vision-analysis 的 content）。

- [ ] **Step 3: 修改 surface.ts**

`packages/core/session/src/surface.ts` 的 `deriveEventMessage` 中 `assistant/message` 分支（现为第 99-105 行）替换为：

```ts
    case 'assistant/message': {
      // Skip an empty-content assistant/message: it exists only to host a
      // max-tokens step's usage and must not inject a content-less assistant
      // turn into the provider transcript.
      if (event.data.message.content.length === 0) return null
      // Vision-analysis blocks are display-only (识图 row, replay, export):
      // the unified model-visible projection drops them here so no provider
      // serializer needs its own rule and they can never leak into a next
      // request's transcript. Filtered content is frozen; the shared block
      // objects are already frozen.
      const content = event.data.message.content.filter(block => block.type !== 'vision-analysis')
      if (content.length === 0) return null
      if (content.length === event.data.message.content.length) return event.data.message
      return Object.freeze({ ...event.data.message, content: Object.freeze(content) })
    }
```

- [ ] **Step 4: 运行测试确认通过 + 全量 session 测试**

```bash
pnpm exec vitest run packages/core/session/tests/derive-vision-filter.spec.ts   # PASS
pnpm exec vitest run packages/core/session                                       # 期望：既有测试全部通过
```

- [ ] **Step 5: Commit**

```bash
git add packages/core/session
git commit -m "feat(session): filter display-only vision-analysis blocks at the unified model-visible projection"
```

---

### Task 7: 客户端会话装配识别 vision-analysis 块

**Files:**
- Modify: `packages/client/runtime/src/client/sessions/conversation.ts`（`AssistantBlock` 联合 + `toAssistantBlock` case）
- Modify: `packages/client/runtime/src/client/sessions/partial.ts`（`emptyAssistantBlock` case）
- Test: `packages/client/runtime/tests/`（conversation-assembler 或 toAssistantBlock 相关客户端测试）

**Interfaces:**
- Produces: 客户端 `AssistantBlock` 新增 `{ kind: 'vision-analysis'; block: VisionAnalysisBlockView }`；`VisionAnalysisBlockView`（本地结构类型：`{ type: 'vision-analysis'; text: string; metadata: { backend; model; mode; durationMs; cacheHit; traceId? } }`，不依赖 llm-dsv 包）。
- Consumes: `toAssistantBlock` 现分类器；`emptyAssistantBlock`。

- [ ] **Step 1: 写失败测试**

在 `packages/client/runtime/tests/` 新建 `vision-block.client.spec.ts`（参考现有 conversation-assembler 测试写法）：

```ts
import { describe, expect, it } from 'vitest'
import { toAssistantBlocks } from '../src/client/sessions/conversation.ts'

const VISION = {
  type: 'vision-analysis',
  text: '图中有一只猫',
  metadata: { backend: 'openai_compatible', model: 'qwen-vl-max', mode: 'auto', durationMs: 12, cacheHit: false, traceId: 't-1' },
}

describe('toAssistantBlocks vision-analysis', () => {
  it('classifies vision-analysis blocks with text and metadata intact', () => {
    const blocks = toAssistantBlocks([VISION as never, { type: 'text', text: 'hi' }])
    expect(blocks[0]).toEqual({ kind: 'vision-analysis', block: VISION })
    expect(blocks[1]).toEqual({ kind: 'text', text: 'hi' })
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pnpm exec vitest run packages/client/runtime/tests/vision-block.client.spec.ts
```

期望：FAIL（当前落入 `{kind:'other'}`）。

- [ ] **Step 3: 修改 conversation.ts 与 partial.ts**

`conversation.ts`：`AssistantBlock` 联合追加

```ts
  | { kind: 'vision-analysis'; block: VisionAnalysisBlockView }
```

并在文件内定义：

```ts
/** Client view of the display-only vision-analysis block (server block shape, locally typed). */
export interface VisionAnalysisBlockView {
  type: 'vision-analysis'
  text: string
  metadata: {
    backend: string
    model: string
    mode: string
    durationMs: number
    cacheHit: boolean
    traceId?: string
  }
}
```

`toAssistantBlock` 的 switch 在 `default` 之前追加：

```ts
    case 'vision-analysis':
      return { kind: 'vision-analysis', block: block as unknown as VisionAnalysisBlockView }
```

`partial.ts` 的 `emptyAssistantBlock` switch 追加：

```ts
    case 'vision-analysis':
      return { kind: 'vision-analysis', block: { type: 'vision-analysis', text: '', metadata: { backend: '', model: '', mode: 'auto', durationMs: 0, cacheHit: false } } }
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pnpm exec vitest run packages/client/runtime/tests/vision-block.client.spec.ts   # PASS
pnpm exec vitest run packages/client/runtime                                     # 期望：既有测试全部通过
```

- [ ] **Step 5: Commit**

```bash
git add packages/client/runtime
git commit -m "feat(client-runtime): classify vision-analysis blocks in the conversation assembler"
```

---

### Task 8: 识图栏 UI（ui-conversation + ui-primitives 图标）

**Files:**
- Modify: `packages/client/ui-primitives/src/icons/index.tsx`（新增 `IconImageOutline14`）
- Create: `packages/client/ui-conversation/src/client/chat/VisionAnalysisRow.tsx`
- Create: `packages/client/ui-conversation/src/client/chat/VisionAnalysisRow.module.css`
- Modify: `packages/client/ui-conversation/src/client/chat/AssistantMarkdown.tsx`（switch case）
- Modify: `packages/client/ui-conversation/src/client/locales.ts`（zh + en 键）
- Test: `packages/client/ui-conversation/tests/vision-analysis-row.client.spec.tsx`（镜像 `reasoning-row.client.spec.tsx`）

**Interfaces:**
- Produces: `VisionAnalysisRow({ block: VisionAnalysisBlockView; running: boolean; t })`：图片图标 + “识图”标签 + 折叠摘要（首行）；展开显示完整分析文本 + 非敏感元数据（backend/model/mode/durationMs/cacheHit/traceId）；`running` 时显示“识图中…”。
- Consumes: `DisclosureRow`、`IconImageOutline14`（`@deepseek-ai/dsh-client-ui-primitives`）；`ChatViewSlotProps['t']`。

- [ ] **Step 1: 写失败测试**

`packages/client/ui-conversation/tests/vision-analysis-row.client.spec.tsx`：

```tsx
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { makeTranslate } from '../src/client/locales.ts'
import { VisionAnalysisRow } from '../src/client/chat/VisionAnalysisRow.tsx'
import type { VisionAnalysisBlockView } from '@deepseek-ai/dsh-client-runtime/client'

const t = makeTranslate('zh')

const block: VisionAnalysisBlockView = {
  type: 'vision-analysis',
  text: '图中有一只猫，坐在窗台上。\n阳光照在它的毛上。',
  metadata: { backend: 'openai_compatible', model: 'qwen-vl-max', mode: 'auto', durationMs: 321, cacheHit: false, traceId: 't-1' },
}

describe('VisionAnalysisRow', () => {
  it('renders the 识图 label with the first line as summary', () => {
    render(<VisionAnalysisRow block={block} running={false} t={t} />)
    expect(screen.getByText('识图')).toBeDefined()
    expect(screen.getByText('图中有一只猫，坐在窗台上。')).toBeDefined()
  })

  it('shows 识图中… while running', () => {
    render(<VisionAnalysisRow block={{ ...block, text: '' }} running t={t} />)
    expect(screen.getByText('识图中…')).toBeDefined()
  })

  it('expands to show the full analysis and metadata', () => {
    render(<VisionAnalysisRow block={block} running={false} t={t} />)
    const row = screen.getByText('图中有一只猫，坐在窗台上。')
    fireEvent.click(row)
    expect(screen.getByText(/阳光照在它的毛上/)).toBeDefined()
    expect(screen.getByText(/qwen-vl-max/)).toBeDefined()
    expect(screen.getByText(/321/)).toBeDefined()
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pnpm exec vitest run packages/client/ui-conversation/tests/vision-analysis-row.client.spec.tsx
```

期望：FAIL（组件/键/图标不存在）。

- [ ] **Step 3: 新增图标（镜像 IconThinkOutline14 模式）**

`packages/client/ui-primitives/src/icons/index.tsx` 在 IconThinkOutline14 附近新增：

```tsx
/** Image / picture outline icon used by the 识图 row. */
export const IconImageOutline14 = ({ size = 14, className }: IconProps) => (
  <svg viewBox="0 0 14 14" width={size} height={size} className={className} aria-hidden fill="none">
    <rect x="1.5" y="2.5" width="11" height="9" rx="1.5" stroke="currentColor" />
    <circle cx="5" cy="5.5" r="1" stroke="currentColor" />
    <path d="M3 10.5 6 7l2.5 2.5L11 7l1 1.5" stroke="currentColor" />
  </svg>
)
```

（SVG 细节以现有图标风格为准；如有 icons 清单/快照测试，同步更新。）

- [ ] **Step 4: 写 VisionAnalysisRow + CSS**

`packages/client/ui-conversation/src/client/chat/VisionAnalysisRow.tsx`：

```tsx
import { useState } from 'react'
import { DisclosureRow, IconImageOutline14 } from '@deepseek-ai/dsh-client-ui-primitives'
import type { VisionAnalysisBlockView } from '@deepseek-ai/dsh-client-runtime/client'
import type { ChatViewSlotProps } from '../contract/slots.ts'
import a11yCss from './accessibility.module.css'
import css from './VisionAnalysisRow.module.css'

function firstLine(text: string): string {
  const newline = text.indexOf('\n')
  return newline === -1 ? text : text.slice(0, newline)
}

function formatDuration(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

/**
 * Render one vision-analysis block as the 识图 disclosure row: image icon,
 * 识图 label, collapsed first-line summary, and an expanded body with the
 * full analysis text plus non-sensitive metadata.
 */
export function VisionAnalysisRow({ block, running, t }: {
  block: VisionAnalysisBlockView
  running: boolean
  t: ChatViewSlotProps['t']
}) {
  const [expanded, setExpanded] = useState(false)
  const { metadata } = block
  const summary = running ? '' : firstLine(block.text)
  return (
    <div className={css.root} data-state={running ? 'running' : 'ok'}>
      {running && <span className={a11yCss.visuallyHidden}>{t('vision.running')}</span>}
      <DisclosureRow
        rowClassName={css.row}
        leadingClassName={css.leading}
        titleClassName={css.title}
        chevronClassName={css.chevron}
        icon={<IconImageOutline14 size={14} />}
        title={t('vision.label')}
        open={expanded}
        expandable
        expandOnRowClick
        onToggle={() => { setExpanded(value => !value) }}
        collapsedContent={(
          <>
            <span className={css.separator} aria-hidden />
            <span className={css.summary}>{running ? t('vision.running') : summary}</span>
          </>
        )}
      >
        <div className={css.body}>{block.text}</div>
        <dl className={css.metadata}>
          <div><dt>{t('vision.metadata.backend')}</dt><dd>{metadata.backend || '—'}</dd></div>
          <div><dt>{t('vision.metadata.model')}</dt><dd>{metadata.model || '—'}</dd></div>
          <div><dt>{t('vision.metadata.mode')}</dt><dd>{metadata.mode}</dd></div>
          <div><dt>{t('vision.metadata.durationMs')}</dt><dd>{formatDuration(metadata.durationMs)}</dd></div>
          <div><dt>{t('vision.metadata.cacheHit')}</dt><dd>{metadata.cacheHit ? '✓' : '—'}</dd></div>
          {metadata.traceId !== undefined && <div><dt>{t('vision.metadata.traceId')}</dt><dd>{metadata.traceId}</dd></div>}
        </dl>
      </DisclosureRow>
    </div>
  )
}
```

`packages/client/ui-conversation/src/client/chat/VisionAnalysisRow.module.css`（镜像 ReasoningRow.module.css 的关键类）：

```css
.root { display: flex; flex-direction: column; }
.row { display: flex; align-items: baseline; gap: 6px; }
.leading { display: flex; align-items: center; }
.title { font-size: 12px; font-weight: 600; color: var(--dsh-text-secondary); }
.chevron { color: var(--dsh-text-secondary); }
.separator { margin: 0 6px; color: var(--dsh-text-secondary); }
.summary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 420px; font-size: 12px; color: var(--dsh-text-secondary); }
.body { font-size: 13px; line-height: 1.6; margin: 8px 0; }
.metadata { display: grid; grid-template-columns: auto 1fr; gap: 2px 12px; margin: 0; font-size: 11px; color: var(--dsh-text-secondary); }
.metadata dt { opacity: 0.7; }
.metadata dd { margin: 0; }
```

（CSS 变量名以 ui-conversation 现有 module.css 实际使用的 token 为准，实现时核对 `ReasoningRow.module.css`。）

- [ ] **Step 5: AssistantMarkdown switch case**

`AssistantMarkdown.tsx` 的 switch 在 `case 'tool-call'` 之后、`default` 之前追加：

```tsx
      case 'vision-analysis':
        rendered.push(<VisionAnalysisRow key={i} block={block.block} running={streaming && i === last} t={t} />)
        break
```

并在文件顶部 import `VisionAnalysisRow`。

- [ ] **Step 6: locales**

`packages/client/ui-conversation/src/client/locales.ts` 的 zh 与 en 字典各追加：

```ts
  'vision.label': '识图',
  'vision.running': '识图中…',
  'vision.metadata.backend': '后端',
  'vision.metadata.model': '模型',
  'vision.metadata.mode': '模式',
  'vision.metadata.durationMs': '耗时',
  'vision.metadata.cacheHit': '缓存命中',
  'vision.metadata.traceId': 'Trace',
```

```ts
  'vision.label': 'Vision',
  'vision.running': 'Analyzing…',
  'vision.metadata.backend': 'Backend',
  'vision.metadata.model': 'Model',
  'vision.metadata.mode': 'Mode',
  'vision.metadata.durationMs': 'Duration',
  'vision.metadata.cacheHit': 'Cache hit',
  'vision.metadata.traceId': 'Trace',
```

（`makeTranslate('zh')` 的签名以 locales.ts 实际导出为准；如为 `{ zh, en }` 字典对象则改为 `makeTranslate({ zh, en })` 对应用法。）

- [ ] **Step 7: 运行测试确认通过**

```bash
pnpm exec vitest run packages/client/ui-conversation/tests/vision-analysis-row.client.spec.tsx   # PASS
pnpm exec vitest run packages/client/ui-conversation packages/client/ui-primitives                # 既有测试全部通过
```

- [ ] **Step 8: Commit**

```bash
git add packages/client/ui-conversation packages/client/ui-primitives
git commit -m "feat(ui-conversation): render the vision-analysis block as the 识图 disclosure row"
```

---

### Task 9: Loader composition 集成测试

**Files:**
- Test: `packages/llm/llm-dsv/tests/loader-composition.spec.ts`

**Interfaces:**
- 通过真实 Loader + Include 启动测试 cordis.yml（llm + settings-file + credentials-local + llm-dsv + 必要的附件/工具服务），用 DSV mock 验证：无图请求走 `next()`（挂 llm-deepseek + mock 断言不被打扰）、含图请求命中 DSV mock（base64 图片 + vision.mode + tools）、外部编辑 settings.yaml 与凭证文件后下一次请求使用新 baseURL/新 key（live 语义）、`vision-analysis` 块出现在装配结果且 `deriveMessages` 不含该块。
- Consumes: `@deepseek-ai/cordis-plugin-loader`、`@deepseek-ai/cordis-plugin-include`、`@deepseek-ai/dsh-llm`、`@deepseek-ai/dsh-settings-file`、`@deepseek-ai/dsh-credentials-local`、`@deepseek-ai/dsh-llm-deepseek`、`@deepseek-ai/dsh-llm-dsv`、附件与工具服务模块（以实际 composition 所需为准，参照 `llm-deepseek/tests/loader-composition.spec.ts` 的 `loadComposition` 结构）。

- [ ] **Step 1: 写组合测试**

`packages/llm/llm-dsv/tests/loader-composition.spec.ts`（骨架参照 `llm-deepseek/tests/loader-composition.spec.ts`；关键断言）：

```ts
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import Loader from '@deepseek-ai/cordis-plugin-loader'
import Include from '@deepseek-ai/cordis-plugin-include'
import { BlockAssembler, createUserMessage } from '@deepseek-ai/dsh-llm'
import { settingsNamespace } from '@deepseek-ai/dsh-settings'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import LlmRuntime from '@deepseek-ai/dsh-llm'
import FileSettingsProvider from '@deepseek-ai/dsh-settings-file'
import LocalCredentialProvider from '@deepseek-ai/dsh-credentials-local'
import * as LlmDeepSeek from '@deepseek-ai/dsh-llm-deepseek'
import * as LlmDsv from '@deepseek-ai/dsh-llm-dsv'
import { closeMockServers, dsvVisionStream, mockServer } from './mock-server.ts'

const NS = settingsNamespace('llm-dsv')
const KEY_REF = credentialRef('DEEPSEE_DSV_API_KEY')

let root: string | undefined
let context: Context | undefined

afterEach(async () => {
  await context?.fiber.dispose()
  context = undefined
  if (root !== undefined) await rm(root, { recursive: true, force: true })
  root = undefined
  await closeMockServers()
  vi.unstubAllEnvs()
})

async function loadComposition(options: { dsvBaseURL: string; deepseekBaseURL: string }): Promise<{ ctx: Context; settingsPath: string; credentialsPath: string }> {
  root = await mkdtemp(join(tmpdir(), 'dsh-llm-dsv-composition-'))
  vi.stubEnv('DSH_HOME', root)
  const settingsPath = join(root, 'settings.yaml')
  const credentialsPath = join(root, '.credentials.yaml')
  await writeFile(settingsPath, '# personal settings\n')
  await writeFile(credentialsPath, 'DEEPSEE_DSV_API_KEY: dsv-key\nDEEPSEEK_API_KEY: deepseek-key\n', { mode: 0o600 })

  const configPath = join(root, 'cordis.yml')
  await writeFile(configPath, [
    '- id: llm',
    "  name: 'test-llm-service'",
    '- id: settings',
    "  name: '@deepseek-ai/dsh-settings-file'",
    '  config:',
    `    path: ${JSON.stringify(settingsPath)}`,
    '    debounceMs: 10',
    '- id: credentials',
    "  name: '@deepseek-ai/dsh-credentials-local'",
    '  config:',
    `    path: ${JSON.stringify(credentialsPath)}`,
    '    debounceMs: 10',
    '- id: llm-deepseek',
    "  name: '@deepseek-ai/dsh-llm-deepseek'",
    '  config:',
    `    baseURL: ${JSON.stringify(options.deepseekBaseURL)}`,
    '- id: llm-dsv',
    "  name: '@deepseek-ai/dsh-llm-dsv'",
    '  config:',
    `    baseURL: ${JSON.stringify(options.dsvBaseURL)}`,
    '',
  ].join('\n'))

  const ctx = new Context()
  context = ctx
  ctx.baseUrl = pathToFileURL(root).href + '/'
  await ctx.plugin(Loader)
  ctx.loader.builtins.include = Include
  const modules = new Map<string, unknown>([
    ['test-llm-service', LlmRuntime],
    ['@deepseek-ai/dsh-settings-file', FileSettingsProvider],
    ['@deepseek-ai/dsh-credentials-local', LocalCredentialProvider],
    ['@deepseek-ai/dsh-llm-deepseek', LlmDeepSeek],
    ['@deepseek-ai/dsh-llm-dsv', LlmDsv],
  ])
  ctx.loader.internal = {
    version: 'v2',
    async import(specifier: string) {
      if (!modules.has(specifier)) throw new Error(`unexpected Loader import: ${specifier}`)
      return modules.get(specifier)
    },
  } as never
  await ctx.loader.create({ name: 'cordis:include', config: { path: pathToFileURL(configPath).href } })
  await ctx.loader.await()
  return { ctx, settingsPath, credentialsPath }
}

const IMAGE_REF = { attachmentId: 'a-1', mediaType: 'image/png', bytes: 4, width: 1, height: 1 }

describe('dsh-llm-dsv real composition', () => {
  it('routes image requests to DSV and text requests to the original provider', async () => {
    const dsv = await mockServer([{ kind: 'sse', events: dsvVisionStream() }])
    const deepseek = await mockServer([{ kind: 'sse', events: ['data: {"choices":[{"delta":{"content":"文本回答"}}]}\n\n', 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n', 'data: [DONE]\n\n'] }])
    const { ctx } = await loadComposition({ dsvBaseURL: dsv.url, deepseekBaseURL: deepseek.url })

    // 含图请求 → DSV
    const imageMessage = createUserMessage({ content: [{ type: 'image', attachment: IMAGE_REF as never }], source: { kind: 'user' } })
    const assembler = new BlockAssembler()
    for await (const chunk of ctx.llm.stream({ provider: 'deepseek-official', model: 'deepseek-v4-flash', messages: [imageMessage] })) {
      assembler.push(chunk)
    }
    expect(dsv.requests).toHaveLength(1)
    expect((dsv.requests[0] as { vision: { include_analysis: boolean } }).vision.include_analysis).toBe(true)
    expect(assembler.blocks()[0]).toMatchObject({ type: 'vision-analysis' })

    // 无图请求 → deepseek-official
    const textAssembler = new BlockAssembler()
    for await (const chunk of ctx.llm.stream({ provider: 'deepseek-official', model: 'deepseek-v4-flash', messages: [] })) {
      textAssembler.push(chunk)
    }
    expect(deepseek.requests).toHaveLength(1)
    expect(textAssembler.blocks()).toEqual([{ type: 'text', text: '文本回答' }])
  })

  it('applies live settings and credential rotation to the next request', async () => {
    const first = await mockServer([{ kind: 'sse', events: dsvVisionStream() }])
    const second = await mockServer([{ kind: 'sse', events: dsvVisionStream('新分析') }])
    const deepseek = await mockServer([])
    const { ctx, settingsPath, credentialsPath } = await loadComposition({ dsvBaseURL: first.url, deepseekBaseURL: deepseek.url })
    const imageMessage = createUserMessage({ content: [{ type: 'image', attachment: IMAGE_REF as never }], source: { kind: 'user' } })

    const run = async (): Promise<void> => {
      const assembler = new BlockAssembler()
      for await (const chunk of ctx.llm.stream({ provider: 'deepseek-official', model: 'deepseek-v4-flash', messages: [imageMessage] })) {
        assembler.push(chunk)
      }
    }
    await run()
    expect(first.headers[0]?.authorization).toBe('Bearer dsv-key')

    await writeFile(settingsPath, `llm-dsv:\n  baseURL: ${second.url}\n`)
    await writeFile(credentialsPath, 'DEEPSEE_DSV_API_KEY: rotated-key\nDEEPSEEK_API_KEY: deepseek-key\n', { mode: 0o600 })
    await vi.waitFor(async () => {
      expect(await ctx.get('credentials')!.resolve(KEY_REF)).toEqual({ value: 'rotated-key', source: 'file' })
    }, { timeout: 5000 })
    await vi.waitFor(() => {
      expect((ctx.get('settings')!.get(NS) as { baseURL?: string }).baseURL).toBe(second.url)
    }, { timeout: 5000 })

    await run()
    expect(first.requests).toHaveLength(1)
    expect(second.headers[0]?.authorization).toBe('Bearer rotated-key')
  })
})
```

> 注：附件服务（`ctx.attachments`）在真实 composition 中由产品挂载；测试若因 `inject: ['attachments']` 等待服务，需在 cordis.yml 加一行 attachment-local（`@deepseek-ai/dsh-attachment-local`，`config: { root: <tmp> }`，以该包实际配置项为准），并把模块加入 `modules` map。工具注册所需 `tools`/`systemPrompt` 服务同理按需挂载（可参考 `packages/test-support/agent-loop-testkit` 的装配清单）。

- [ ] **Step 2: 运行测试**

```bash
pnpm exec vitest run packages/llm/llm-dsv/tests/loader-composition.spec.ts
```

期望：PASS。若 `llm-dsv` 的 `inject` 硬依赖使插件在缺失服务时无法挂载，调整 composition 行或按插件语义补充服务行（附件/工具服务），并确认「无图请求行为不变」断言仍成立。

- [ ] **Step 3: Commit**

```bash
git add packages/llm/llm-dsv
git commit -m "test(llm-dsv): boot a real Loader composition and verify DSV routing, live settings, and credential rotation"
```

---

### Task 10: 文档、全量验证与收尾

**Files:**
- Modify: `packages/llm/llm-dsv/README.md`、`README.zh.md`、`README.i18n.yaml`（最终版：部署边界、设置表、挂载示例、限制与 v1 决策）
- Modify: `docs/superpowers/specs/2026-08-15-dsh-vision-integration-design.md`（DeepSee 仓库，状态更新为 DSH 插件已实施）
- Modify: `docs/superpowers/specs/2026-08-15-dsh-plugin-development-notes.md`（如需要，标注实现状态）

- [ ] **Step 1: 完善 README（中英对照）**

`README.md` 与 `README.zh.md` 内容对齐，必须覆盖：用途与数据链路图；部署边界（连接已运行的 DSV 网关 `POST /v1/dsv`，默认 `http://127.0.0.1:8712`；视觉 provider 配置留在网关侧；只发送 DSV public key）；设置表（`backend`/`apiKeyEnv`(credential-ref)/`baseURL`/`model`(advisory)/`mode`）；`cordis.yml` 挂载示例；v1 决策与限制（vision 分析非流式；追问上限 2 轮；识图块为 display-only，经统一投影过滤；本地网关管理不在 v1）。随后：

```bash
pnpm run verify-translation-pairing --write packages/llm/llm-dsv/README.md
pnpm run verify-package-readme-model-experience
pnpm run verify-package-readme-limitations
```

- [ ] **Step 2: 全量门禁**

```bash
cd /Users/jerrywu/deepseek-harness
pnpm run typecheck
pnpm run lint
pnpm exec vitest run packages/llm/llm-dsv packages/core/session packages/client/runtime packages/client/ui-conversation
pnpm run constraints
pnpm run verify-package-invariants
pnpm run doc-sync
pnpm run knip
```

期望：全部通过；新文件 per-file 100% 覆盖率由 `pnpm run test:coverage`（或 CI coverage gate）验证：

```bash
pnpm exec vitest run --coverage packages/llm/llm-dsv
```

若个别分支（如异常路径）未达 100%，补齐对应测试，不允许加覆盖率豁免。

- [ ] **Step 3: 更新 DeepSee 仓库设计文档状态**

在 `/Users/jerrywu/Documents/DeepSee/docs/superpowers/specs/2026-08-15-dsh-vision-integration-design.md` 的状态行改为“DSV 服务端与 DSH 插件（`@deepseek-ai/dsh-llm-dsv`）均已实施”，并把「DSH 实现切入点」一节标注已落地条目；提交到 DeepSee 仓库（单独 commit）。

- [ ] **Step 4: 全量测试与最终提交**

```bash
cd /Users/jerrywu/deepseek-harness
pnpm run test          # 全量 vitest
git status --short     # 确认仅预期文件
git add -A
git commit -m "docs(llm-dsv): document deployment boundary, settings, and v1 decisions"
```

- [ ] **Step 5: 分支状态汇报**

```bash
git log --oneline -12
git branch --show-current   # feat/dsh-llm-dsv
```

向用户汇报：分支名、提交列表、如何挂载到 cordis.yml、如何本地端到端验证（启动 `deepsee-server` → 配置 DSV public key → 在 DSH 配置中挂载插件 → 发送图片消息）。

---

## Self-Review

**Spec coverage（`docs/superpowers/specs/2026-08-15-dsh-plugin-development-notes.md`）：**
- §2 扩展点（`llm/stream` waterfall、`next()`、disposer）→ Task 4 ✓
- §3 消息/图片（`contentHasImage` 复用、完整历史、attachment 读取失败/超限的插件侧失败状态、DSV 请求字段）→ Task 2/4 ✓
- §4 SSE 生命周期（`event.type` 分派、vision.completed 早于 answer、独立 vision 块、未知事件前向兼容、坏帧错误）→ Task 3 ✓
- §5 取消/超时/错误（signal 贯穿 fetch/reader/iterator、错误阶段映射、HTTP 错误码、不伪造成功）→ Task 3/4 ✓
- §6 工具（`defineTool`+`ctx.tools.register`、question 入参、durable attachment 重读、tool_call_id/原始 arguments、2 轮上限）→ Task 5 ✓
- §7 设置/凭证（小写 kebab-case、credential-ref、live、每请求 resolve、key 不出日志）→ Task 4/9 ✓
- §8 识图栏/会话/导出（独立结果段、不进 model-visible、log-only 语义、导出不含图片字节）→ Task 6/8 + README ✓（v1 决策：识图块进消息但统一投影过滤）
- §9 类型/生命周期（declaration merging 扩展 ContentBlockMap、未知块 fallback、dispose 移除注册、HMR 测试）→ Task 2/6 + loader composition ✓
- §10 测试清单 → Task 2/3/4/5/9 的用例矩阵 ✓（含无图 next、单图/多图/失效附件/非法媒体类型、事件顺序、视觉不入正文、requires_action 工具循环、取消关闭、live 更新、dispose/HMR、真实 Loader 组合）
- §11 明确不做 → 全部排除 ✓
- §12 交付检查 → Task 10 门禁 ✓

**Placeholder scan：** 无 TBD/TODO；Task 3 Step 4 注与 Task 9 Step 1 注中的“以实际实现为准”属于实现期核对项（仓库 API 细节），非占位符。

**Type consistency：** `VisionAnalysisBlock`（server）与 `VisionAnalysisBlockView`（client）字段一致（text + metadata{backend,model,mode,durationMs,cacheHit,traceId?}）；`ResolvedDsvOptions{baseURL, apiKeyRef, mode}` 贯穿 config/client/tool；`DsvClient.stream/request/visionDetail` 签名在 Task 4/5 间一致；错误码常量在 Task 3/4/5 中命名一致（`VISION_FAILED`/`REASONING_FAILED`/`MALFORMED_RESPONSE`/`STREAM_CLOSED`/`AUTH`/`RATE_LIMIT`/`INVALID_REQUEST`/`SERVER`/`TRANSPORT`/`TIMEOUT`/`ABORTED`/`MISSING_CREDENTIAL`/`IMAGE_READ_FAILED`/`IMAGE_TOO_LARGE`）。
