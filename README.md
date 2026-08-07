# DeepSee

为 DeepSeek 官方 API 提供可插拔的视觉处理层,让 DeepSeek 获得多模态能力:
一次 `ask_with_image()` 调用,完成"视觉模型看图 → DeepSeek 推理回答"。

## 安装

```bash
pip install seedeep
```

> PyPI 上的 `deepsee` 已被 2014 年的无关项目占用,本包发布名为 `seedeep`;
> import 包名仍是 `deepsee`。启动本地服务时用 `pip install "seedeep[server]"`。

## 快速开始

```python
from deepsee import ask_with_image

answer = ask_with_image("photo.jpg", "这张图里有什么?")
print(answer)
```

## 配置

配置文件 `deepsee.toml`(放在当前目录或 `~/.config/deepsee/`),也可以只用环境变量
(`DeepSee_DEEPSEEK_API_KEY` 等)。`${ENV}` 可引用环境变量:

```toml
[deepseek]
api_key = "${DEEPSEEK_API_KEY}"

[vision]
backend = "openai_compatible"   # openai_compatible | anthropic | gemini
api_key = "${VISION_API_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen-vl-max"
```

切换视觉后端只需修改 `backend` / `api_key` / `base_url` / `model` 四个字段。

用环境变量覆盖 `VISION_BACKEND` 切换后端时,TOML 中的 `base_url` / `api_key`
/ `model` 不会沿用(它们属于旧后端:base_url 指向旧主机,key 属于旧供应商,
会被发给错误的主机/供应商)。`base_url` 回落到新后端的官方默认主机 ——
Anthropic 和 Gemini 有默认主机;OpenAI-compatible 没有默认值,必须显式设置
`VISION_BASE_URL`。`api_key` 与 `model` 必须由环境变量显式提供,否则报错。
环境变量与 TOML 中**字面量** `backend` 相同的 `VISION_BACKEND` 不算切换,
TOML 配置原样保留(自定义代理 / 审计 / 数据驻留场景)。但 TOML `backend`
若写成 `${ENV}` 插值(如 `backend = "${VISION_BACKEND}"`),一律视为切换:
`base_url` 回落默认,且 `api_key` / `model` 必须使用标准环境变量
`VISION_API_KEY` / `VISION_MODEL` 显式提供 —— TOML 中的自定义 `${ENV}`
占位符不会生效,旧变量可安全删除。

## 支持的后端

- **openai_compatible**: Qwen-VL、GPT-4o、GLM-4V、Moonshot 等任意 OpenAI 兼容服务
- **anthropic**: Claude 系列(原生 API)
- **gemini**: Google Gemini(原生 API)

## 流式输出

```python
for chunk in ask_with_image("photo.jpg", "讲个故事", stream=True):
    print(chunk, end="", flush=True)
```

## 异步 API

所有同步接口都有对应的 `async` 版本,签名一致:

```python
import asyncio
from deepsee import ask_with_image_async

async def main():
    # 非流式
    answer = await ask_with_image_async("photo.jpg", "这张图里有什么?")
    print(answer)

    # 流式(async 迭代器)
    async for chunk in ask_with_image_async("photo.jpg", "讲个故事", stream=True):
        print(chunk, end="", flush=True)

asyncio.run(main())
```

另有 `ask_async`(纯文本)与 `describe_image_async`(仅视觉分析)。
错误语义与同步接口一致;图片处理(含 SSRF 防护)复用同一套同步管线。

## 多协议端点

服务同时暴露三种协议形状的聊天端点,视觉分析结果作为响应元数据返回,
供 GUI 像展开思考过程一样点击查看(字段语义 = "模型看到了什么"):

- `POST /v1/chat/completions` — OpenAI 兼容;有图时非流式响应
  `choices[0].message.vision_analysis`;流式响应以独立前置 chunk 发出
  `choices[0].delta.vision_analysis`(不含 `content`),随后是回答文本 chunk;
- `POST /v1/messages` — Anthropic messages 形状;非流式响应顶层
  `vision_analysis`;流式响应在 `message_start` 后发
  `{"type": "vision_analysis", "vision": ...}` 事件;
- `POST /v1beta/models/{model}:generateContent` — Gemini 形状;非流式
  响应 `parts` 首位是 `{"text": ..., "vision": true}`;流式响应以独立前置
  chunk 发出该 part。

三种端点都支持 `stream` 参数(流式/非流式),图片输入按各自协议形状
(data URL / base64 source / inline_data / http URL),统一受 SSRF 防护与
字节上限约束;`file://` 与本地路径一律拒绝。

**示例**(以 base64 图片 + 流式为例):

```bash
# OpenAI 兼容
curl -N http://127.0.0.1:8712/v1/chat/completions -H "Content-Type: application/json" -d '{
  "stream": true,
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "这张图里有什么?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,<BASE64>"}}
  ]}]
}'
# 响应:首个 chunk 为 {"choices":[{"delta":{"vision_analysis":"..."}}]},
#       之后 chunk 为 {"choices":[{"delta":{"content":"..."}}]},最后 data: [DONE]

# Anthropic messages
curl -N http://127.0.0.1:8712/v1/messages -H "Content-Type: application/json" -d '{
  "model": "claude-3-5-sonnet",
  "max_tokens": 1024,
  "stream": true,
  "messages": [{"role": "user", "content": [
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "<BASE64>"}},
    {"type": "text", "text": "这张图里有什么?"}
  ]}]
}'
# 响应:message_start → {"type":"vision_analysis","vision":"..."} → content_block_delta(text) → message_stop

# Gemini generateContent
curl -N http://127.0.0.1:8712/v1beta/models/gemini-2.0-flash:generateContent -H "Content-Type: application/json" -d '{
  "stream": true,
  "contents": [{"parts": [
    {"inline_data": {"mime_type": "image/png", "data": "<BASE64>"}},
    {"text": "这张图里有什么?"}
  ]}]
}'
# 响应:首个 chunk 的 parts 为 [{"text":"...","vision":true}],后续 chunk 只带回答文本 part
```

## 安全限制

图片加载对服务化入口(`/v1/chat/completions` 的 `image_url`、`/analyze`)统一生效:

- **SSRF 防护**:http(s) URL 的主机(含每一跳重定向目标)解析到私网、loopback、
  link-local、保留或特殊用途地址(如 `127.0.0.1`、`169.254.169.254`)时拒绝下载。
  校验通过后,TCP 连接固定到已校验的 IP(域名只解析一次),消除 DNS rebinding
  TOCTOU;TLS 仍按原始域名校验证书。下载不读环境代理(`trust_env=False`),
  防止代理绕过本地校验。RFC 6052 NAT64 前缀(`64:ff9b::/96`、`64:ff9b:1::/48`)
  显式拒绝;部署网络若使用其他自定义 NAT64 前缀,需自行扩展
  `deepsee/pipeline/image.py` 的 `_NAT64_NETWORKS`;
- **本地路径**:服务端只接受 `data:` 与 http(s) URL,`file://` 与本地路径一律拒绝
  (CLI 本地调用不受影响);
- **资源上限**:原始图片字节上限 20 MiB、解码像素上限约 1670 万(4096x4096),
  超限在下载/解码前拒绝;下载请求 `Accept-Encoding: identity` 并拒绝压缩响应,
  字节上限按原始字节流式累计(扩容前检查),防止大响应与解压炸弹耗尽内存;
- **请求体上限**:服务端请求体超过 32 MiB 返回 413,请求体流式读取,
  无 `Content-Length` 的 chunked 请求同样受限;
- **流式超时**:DeepSeek 流式响应的 HTTP 帧间超时 120 秒(完全静默的上游
  120 秒后报错),另有总时长上限 300 秒(`deepsee/composer/deepseek.py`
  的 `_STREAM_TOTAL_TIMEOUT`)—— 持续发送 SSE keepalive 却永不 `[DONE]`
  的上游会触发总时长上限,超时抛 `ComposeError`(服务端以 error chunk 通知);
- **流式资源释放**:库的流式接口(`stream=True`)返回的迭代器需完整消费或
  调用 `close()` / `aclose()`(建议 `contextlib.closing` / `aclosing`)以释放
  底层连接;服务端流式端点已用 `aclosing` 保证取消/断开时释放;
- **环境代理**:库发起的上游请求不读环境代理(`trust_env=False`)。SOCKS 代理
  (如 `ALL_PROXY=socks5://`)在未安装 `socksio` 时会直接 ImportError,且代理
  会把含 API key 的请求转发到第三方。依赖代理访问公网 API 的环境需直连或
  自行配置传输层。

## 已知限制与后续工作

以下问题已确认但不在当前版本修复,列为后续工作:

- **CI**: 仓库尚无 CI(GitHub Actions)。建议配置 pytest 在 Python 3.10-3.12
  矩阵上运行,并开启依赖安全扫描;
- **分支保护**: 主分支保护属 GitHub 仓库设置,需人工开启(建议要求 PR 评审
  与 CI 通过后才能合并)。

## 许可证

MIT