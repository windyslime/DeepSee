# DeepSee 多协议"看图问答 + 视觉分析可展开"设计

日期: 2026-08-04
状态: 已批准(用户逐项确认设计)

## 背景

`ask_with_image_async` 的组合管线(视觉分析 → DeepSeek 推理)目前只返回最终回答,
视觉分析结果在内部被 `_format_context` 消化后拼进 prompt,外部调用方拿不到。
用户希望:**聊天端点能同时携带"视觉分析"元数据**,未来 GUI 像展开思考过程一样
点击展开查看(视觉分析 = "模型看到了什么")。同时希望 Server 的 API 不止 OpenAI
兼容一种形状,还要能表达 Anthropic messages 与 Gemini generateContent 形状,
三种协议都支持流式与非流式。

## 决策记录(用户确认)

1. **接口归属**:视觉分析挂在聊天端点(`/v1/chat/completions` 等),作为响应的
   附加元数据(方案 A);
2. **支持范围**:服务端协议适配层 —— OpenAI 兼容 / Anthropic messages /
   Gemini generateContent 三种格式(用户选 B);
3. **编码方式**:自定义扩展字段,不映射到各协议原生"思考"位(用户选 2);
4. **兼容程度**:形状兼容即可,**不做**第三方客户端直连的严格对齐
   (官方路径/认证/流式事件格式),"先不用考虑第三方";
5. **流式**:三种协议都支持流式 + 非流式;视觉分析是一次性完整结果,在流式
   响应中作为**前置元数据**出现(推理 chunk 开始前先发出),非流式响应中是普通字段;
6. **"异步"** = 回答逐字流式返回(已确认)。

## 范围

- 库层:`ask_with_image_async` 新增 `include_vision` 参数与 `VisionResult` 返回类型;
- Server 层:新增 `deepsee_server/protocols/` 模块(openai / anthropic / gemini),
  三个聊天路由,统一走现有组合管线;
- 测试:协议解析/编码单测、端点集成测试、错误映射、安全回归;
- 文档:README 增加三协议用法。
- 不做:`/analyze` 保持纯视觉(上一轮 L4 修复,与新功能正交);同步版
  `include_vision`;GUI 本身;第三方客户端直连兼容(认证头、官方路径、
  流式事件格式严格对齐)。

## 设计

### 1. 库层:视觉分析暴露(`deepsee/composer/deepseek.py`)

```python
@dataclass
class VisionResult:
    vision: str                      # 视觉分析文本 = _format_context(vision_result)
    text: str | AsyncIterator[str]   # 非流式:完整回答;流式:chunk 迭代器

async def ask_with_image_async(
    image, question, *, stream=False, config=None, mode="auto",
    include_vision: bool = False,
) -> str | AsyncIterator[str] | VisionResult
```

- `include_vision=False`(默认):返回 `str` / `AsyncIterator[str]`,与现状
  **完全兼容**,现有调用方零改动;
- `include_vision=True`:返回 `VisionResult`。`vision` 是完整的(一次视觉调用
  即得全文,不流式);流式时 `text` 是 chunk 迭代器;
- `vision` 内容与注入 DeepSeek 的上下文一致(描述文本或 UI 元素地图),
  用户展开看到的就是"模型看到了什么";
- 内部实现:组合管线在 `_analyze_image_async` 之后、`_run_deepseek_async` 之前
  已持有 `vision_result`;只需把 `_format_context(vision_result)` 随返回值带出,
  不重复调用视觉后端。

### 2. Server 层:协议适配(`deepsee_server/protocols/`)

新增三个模块,每个含三个纯函数:

```python
# openai.py / anthropic.py / gemini.py
def parse_request(body: dict) -> tuple[str, bytes | str | None]   # (text, image)
def encode_text(answer: str, vision: str) -> dict                  # 非流式响应
async def encode_stream(chunks: AsyncIterator[str], vision: str) -> AsyncIterator[bytes]  # 流式响应
```

**文本提取规则**(三种协议一致):取请求中**最后一个**文本片段作为 `question`;
图片取**最后一个**图片表示(多图请求以最后一张为准,三种协议一致)。
**`model` 字段**:接受任意值,不写死、不强制匹配,按配置执行(与现有
OpenAI 端点行为一致);Anthropic 顶层 `system` 字段与 Gemini `system_instruction`
在形状兼容阶段忽略,不参与组合。

**路由**(形状兼容,路径取各家约定):

- `POST /v1/chat/completions` — 现有端点升级:响应带 `vision_analysis`;
- `POST /v1/messages` — Anthropic messages 形状;
- `POST /v1beta/models/{model}:generateContent` — Gemini 形状
  (用 `stream` 参数切换流式,不单开 `:streamGenerateContent`)。

**图片输入解析**(归一化到现有防护):

| 协议 | 图片表示 | 归一化 |
|---|---|---|
| OpenAI | `messages[].content[].image_url.url`(data URL / http) | 现有 `_extract_image_from_url` |
| Anthropic | `content[]` 的 `{type:"image", source:{type:"base64"\|"url", ...}}` | base64 → bytes;url → http 防护 |
| Gemini | `contents[].parts[]` 的 `{inline_data:{mime_type, data}}` / `{file_data:{file_uri}}` | base64 → bytes;uri → http 防护 |

- data URL / base64:解码后走字节上限检查;
- http(s) URL:走现有 `load_image`(SSRF 防护、字节/像素上限、重定向校验);
- `file://` 与本地路径一律拒绝(与现有服务端入口一致);
- Anthropic `source.type="url"` 与 Gemini `file_data.file_uri` 同样受 SSRF 防护。

### 3. 响应字段位置(自定义扩展字段,决策 3)

**OpenAI**:
- 非流式:`choices[0].message.vision_analysis`(与 `content` 平级);
- 流式:**独立前置 chunk** —— 首个 chunk 的 `choices[0].delta` 只带
  `vision_analysis`(不含 `content`),后续 chunk 的 `delta` 只带 `content`;
  即使上游回答为空流,vision chunk 也照常发出;末尾 `data: [DONE]` 不变。

**Anthropic**:
- 非流式:响应顶层 `vision_analysis` 字段(与 `content` 平级);
- 流式:`message_start` 事件之后、第一个 content 块之前,发一个
  `data: {"type": "vision_analysis", "vision": "<完整分析>"}` 事件,
  随后按标准 `content_block_start/delta/stop` 输出回答文本。

**Gemini**:
- 非流式:`candidates[0].content.parts` 在**首位**放置
  `{"text": "<完整分析>", "vision": true}` part,回答文本 part 跟在后面
  (用 `vision: true` 标记区分);
- 流式:**独立前置 chunk** —— 首个 chunk 的 parts 只有
  `{"text": "<完整分析>", "vision": true}`,后续 chunk 只带回答文本 part;
  即使上游回答为空流,vision chunk 也照常发出。

### 4. 错误处理与安全

- 错误映射复用现有:`ImageError` → 4xx;`ComposeError`/`VisionBackendError` → 5xx;
- 错误体按各协议形状输出:
  - OpenAI:`{"error": {"message", "type"}}`;
  - Anthropic:`{"type": "error", "error": {"type", "message"}}`;
  - Gemini:`{"error": {"code", "message"}}`;
- **畸形请求统一 400**:三个解析器对容器字段(`messages`/`contents`/`parts`)
  非数组、容器/嵌套项非对象、`text` 非字符串、`content` 字段存在但既非
  字符串也非数组(含 null/数字/对象)、`image_url`/`source`/
  `inline_data`/`file_data` 非对象等所有形状错误一律抛 `ValueError`,
  由端点映射为各协议形状的 400 —— 不得以 `TypeError`/框架级 500 泄漏;
- **形状校验先于提取,且对所有消息生效**:`content` 顶层形状校验在 `role`
  检查**之前**执行(assistant/system 消息携带非法 `content` 同样 400,不得
  静默忽略);图片叶子字段(`url`/`data`/`file_uri`)字段**存在**时先校验
  必须为字符串,再决定空字符串是否表示缺省 —— 假值(0/null/对象)不得
  绕过类型校验;提取文本/图片仍只从 `role == "user"` 的消息进行;
- **请求校验先于配置加载**:三个端点先解析并校验请求(畸形请求返回
  协议形状 400),再加载 `_current_config()`;配置缺失时畸形请求仍为 400,
  不得因 `ConfigError` 变成 500;
- 请求体 32 MiB 上限与 chunked 流式读取(`_read_body_limited`)对所有新端点生效;
- URL 图片下载统一走 SSRF 防护与字节上限(不因协议不同而绕过)。

### 5. 测试

- `tests/test_composer.py`:库层 `include_vision` 用例 —— 非流式返回
  `VisionResult`(vision 与注入 context 一致)、流式返回 `VisionResult`(vision
  完整 + chunks 可迭代)、`include_vision=False` 回归(返回类型不变);
- 新增 `tests/test_protocols/`:`parse_request` / `encode_text` / `encode_stream`
  三协议单测(图片提取:data URL、http、base64 source、inline_data、file_uri;
  拒绝 file://;字段位置断言);
- `tests/test_server/test_app.py`:三端点 × 流式/非流式 × 有图/无图集成;
  错误映射 ×3(ImageError→4xx、ComposeError→5xx 且错误体形状正确);
  安全回归(413、SSRF 拒绝、字节上限)。

### 6. 文档

- README:新增「多协议端点」小节 —— 三种请求/响应示例,视觉分析字段说明
  (未来 GUI 展开面板的内容来源)。

## 验收标准

- 全部测试通过(`pytest`);
- 三个端点均可 curl 调用:有图请求的响应(非流式)包含视觉分析字段;
  流式响应的首事件/首 chunk 携带完整视觉分析,随后是回答文本 chunk;
- `include_vision=False` 的行为与现状完全一致(回归测试);
- `/analyze` 行为不变(仍为纯视觉);
- git 工作区干净,conventional commits。
