# DeepSee 异步 API 设计

日期: 2026-08-04
状态: 已批准(用户确认设计,含两个隐含决策点)

## 背景

README「已知限制与后续工作」列出的第一项:库仅提供同步接口。`deepsee_server`
的端点虽是 `async def`,内部仍同步调用 `ask`/`ask_with_image`(流式用同步
生成器 `def gen()`),请求实际阻塞事件循环。

## 范围

- 新增三个公开 async 函数:`ask_async` / `ask_with_image_async` / `describe_image_async`。
- 三个视觉后端支持 `describe_async`。
- server 端点改为真正异步(`await` + 异步生成器),消除事件循环阻塞。
- README 更新:新增异步用法示例,从已知限制移除异步 API 项。
- 不做:连接池复用优化(保持每次调用创建/关闭,镜像同步模式)、异步 SSRF
  传输层重写(安全关键代码不二次实现)。

## 设计

### 1. 公共 API(`deepsee/composer/deepseek.py` + `deepsee/__init__.py`)

```python
async def describe_image_async(image, prompt, *, config=None) -> str
async def ask_async(question, *, stream=False, config=None) -> str | AsyncIterator[str]
async def ask_with_image_async(image, question, *, stream=False, config=None, mode="auto") -> str | AsyncIterator[str]
```

- 签名与同步版镜像;`stream=True` 返回 `AsyncIterator[str]`,`async for` 消费;
- 错误语义与同步版一致(`ComposeError`/`VisionBackendError`/`ConfigError`/
  `ImageError`;非法 mode 抛 `ValueError`);
- 每次调用创建并关闭自己的 `httpx.AsyncClient`,无共享状态。

### 2. 后端异步化(`deepsee/backends/base.py` + 3 个 backend)

- `VisionBackend.__init__` 持有同步 `self._client`;异步 client 由 `async_client`
  属性**懒创建**(同步路径不分配)。`close()` 只关闭同步 client,`aclose()` 关闭
  两者——async 路径必须用 `aclose()`(需在对应事件循环中运行);
- 三个后端各实现 `async def describe_async(self, image, prompt, **opts) -> str`,
  与 `describe` 共享 payload/headers 构造(提取为私有方法);
  请求用 `await client.send(...)`,异常包装三段分支与同步版相同
  (`HTTPStatusError` → 请求失败;`HTTPError` → 网络错误;解析错误含 `IndexError`);
- `base.py` 新增 `retry_request_async` / `stream_request_async`:
  与同步版同构,`asyncio.sleep` 代替 `time.sleep`,`resp.aiter_lines()` 异步流式迭代;
- 复用 `create_backend`(基类已含 async client),不新增工厂。

### 3. 图片处理:复用同步管线 + `asyncio.to_thread`

- `await asyncio.to_thread(prepare_image, image)`;SSRF 防护、字节/像素上限、
  EXIF/透明处理零改动;
- 不重写异步 SSRF 传输层(URL 下载仍在 to_thread 中走同步路径)。

### 4. composer 异步化(`deepsee/composer/deepseek.py`)

- 新增 `_request_deepseek_async`(AsyncClient + `retry_request_async`,异常包装
  与同步一致)、`_stream_answers_async`(`stream_request_async` + `aiter_lines`
  异步 SSE 解析:`data:` 前缀过滤、`[DONE]` 终止)、`_analyze_image_async`
  (`await backend.describe_async`,含 mode 校验、`normalize_ui_map` 接入);
- `_compose_messages`/`_format_context`/`_format_ui_map`/`normalize_ui_map`
  与同步版共享。

### 5. server 集成(`deepsee_server/app.py`)

- `chat_completions`: `await ask_with_image_async(...)` / `await ask_async(...)`;
- 流式改为异步生成器 `async def gen(): async for chunk in answer: ...`
  (`StreamingResponse` 原生支持);
- `analyze`: `await describe_image_async(...)`。

### 6. 测试

- `tests/test_composer.py` 追加 async 用例(respx async mock):
  三个函数非流式/流式、错误包装、mode 校验、system 静态断言;
- 三个 backend 测试文件追加 `describe_async` 请求形状 + 网络错误包装用例;
- `tests/test_server/test_app.py` 现有用例回归(async 化后行为不变)。

### 7. 文档

- README 新增「异步 API」用法示例;从「已知限制与后续工作」移除异步 API 项,
  保留 CI、分支保护。

## 验收标准

- 全部测试通过(`pytest`)。
- `ask_with_image_async(stream=True)` 返回可 `async for` 的迭代器,SSE 分块逐块到达。
- server 流式端点使用异步生成器,不再在事件循环中同步阻塞。
- `deepsee/__init__.py` 导出三个 async 函数。
- git 工作区干净,conventional commits。
