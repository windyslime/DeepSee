# DeepSee 网关鉴权与滥用保护设计

日期: 2026-08-13
状态: 已批准

## 1. 目标

修复 DeepSee 本地网关没有入站鉴权、并发上限和速率限制的问题，避免服务在
loopback、局域网或公网监听时被未经授权地调用并消耗上游 API 额度。

本设计只处理安全审计问题 2。静态站点托管、请求追踪、完整消息历史、视觉模式
透传和其他审计问题不在本次范围内。

## 2. 安全默认值

- 直接导入 `deepsee_server.app:app` 但未配置鉴权时，所有受保护路径 fail closed，
  返回 503；`/health` 保持可用。
- `deepsee-server` 默认启用鉴权。首次启动时自动创建 public/admin key，明文只在
  创建时输出一次，磁盘只保存 SHA-256 摘要。
- `--no-auth` 保留，但只允许监听 `127.0.0.1`、`::1` 或 `localhost`。与其他 host
  组合时，启动前报错且不得启动 Uvicorn。
- 不新增生产依赖。

## 3. 密钥与权限

新增 `deepsee_server/auth.py`，提供以下接口:

```python
KeyScope = Literal["public", "admin"]

class ApiKeyStore:
    def __init__(self, path: str | os.PathLike) -> None: ...
    def create(self, scope: KeyScope, label: str) -> CreatedApiKey: ...
    def validate(self, token: str, scope: KeyScope) -> bool: ...
    def revoke(self, record_id: str) -> bool: ...
    def list(self) -> list[dict]: ...
    def is_empty(self) -> bool: ...

def configure_api_key_store(store: ApiKeyStore | None) -> None: ...
def disable_api_key_auth() -> None: ...
```

持久化文件使用原子替换和 `0600` 权限。比较摘要时使用
`hmac.compare_digest`。public key 通过 `Authorization: Bearer <key>` 传递；
admin key 通过 `X-DeepSee-Admin-Key` 传递。两种 scope 不可互换。

受 public key 保护的路径:

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/messages`
- `POST /v1beta/models/{model}:generateContent`
- `POST /analyze`

受 admin key 保护的路径为 `/admin/*`。本次实现密钥管理端点:

- `GET /admin/keys`
- `POST /admin/keys`
- `DELETE /admin/keys/{key_id}`

`GET /health` 不鉴权，返回 `{"status": "ok"}`。其他未受保护路径保持现状。

## 4. 速率与并发保护

新增 `deepsee_server/request_guard.py`，把限速和并发复杂度隐藏在一个小接口后:

```python
class RequestGuard:
    def __init__(
        self,
        *,
        max_concurrent: int,
        queue_timeout: float,
        rate_limit: int,
        rate_window: float,
    ) -> None: ...

    async def acquire(self, identity: str) -> GuardLease: ...

class GuardLease:
    async def __aenter__(self) -> "GuardLease": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
```

语义:

- 限速身份优先使用已验证的 public/admin key 摘要；禁用鉴权时使用客户端 IP。
- 使用固定时间窗口，每个身份默认每 60 秒最多 60 个推理请求。超限返回 429，
  带整数秒 `Retry-After`。
- 默认最多 8 个推理请求同时占用上游资源。等待并发名额最多 2 秒；超时返回 503。
- 并发租约覆盖完整响应生命周期。流式响应只有在生成器结束、异常或客户端取消后
  才释放名额。
- `/health`、密钥管理和普通 4xx 请求不占推理并发名额。鉴权在读取请求体和加载
  上游配置之前执行。
- 进程内状态不跨进程共享；多 worker 或多实例部署必须在反向代理层增加共享限速。

默认值可由环境变量覆盖:

- `DeepSee_MAX_CONCURRENT_REQUESTS`，正整数，默认 `8`
- `DeepSee_REQUEST_QUEUE_TIMEOUT`，正数秒，默认 `2`
- `DeepSee_RATE_LIMIT_REQUESTS`，正整数，默认 `60`
- `DeepSee_RATE_LIMIT_WINDOW`，正数秒，默认 `60`

非法值在启动或显式配置阶段报错，不静默回落。

## 5. 错误响应

鉴权和保护层在协议解析之前执行，统一使用 OpenAI 形状错误体:

```json
{"error":{"message":"invalid API key","type":"authentication_error"}}
```

- 鉴权未配置: 503 / `configuration_error`
- 缺失或错误 key: 401 / `authentication_error`
- 超过速率: 429 / `rate_limit_error`，带 `Retry-After`
- 并发排队超时: 503 / `overloaded_error`，带 `Retry-After: 1`

对 Anthropic/Gemini 入口也在中间件层返回同一安全错误形状，避免在安全层复制三套
协议编码器；业务端点现有的协议形状错误保持不变。

## 6. 启动接口

`deepsee-server` 新增:

- `--keys-file PATH`，默认 `~/.config/deepsee/api-keys.json`
- `--no-auth`，仅 loopback 开发模式
- `--create-recovery-keys`，新增一对 public/admin key，旧 key 保持有效

Host 校验必须发生在创建密钥和启动 Uvicorn之前。首次启动或显式恢复时输出生成的
明文 key；普通重启不重复输出。

## 7. 测试与验收

测试通过公开接口验证行为，不依赖中间件内部实现。必须覆盖:

1. 直接导入 app 且未配置鉴权时，`/health` 为 200，受保护端点为 503。
2. public/admin key 摘要持久化、scope 隔离、撤销、并发创建和 `0600` 权限。
3. 缺 key、错误 key、scope 错误均为 401；正确 key 可进入现有端点。
4. `--no-auth` 允许三种 loopback host，拒绝 `0.0.0.0`、局域网 IP 和空 host。
5. 同一身份超限返回 429 和 `Retry-After`；不同身份独立计数。
6. 并发饱和时在队列超时后返回 503；请求完成后名额可复用。
7. 流式响应消费完成、异常和取消后都释放并发名额。
8. 鉴权失败不调用配置加载、请求体解析或上游模型。
9. 原有服务端测试在测试夹具显式禁用鉴权后保持通过。

完整验证命令:

```bash
uv run pytest tests/test_server/test_auth.py -q
uv run pytest tests/test_server/test_request_guard.py -q
uv run pytest -q
uv build
git diff --check
```

## 8. 非目标与剩余风险

- 不实现分布式限速、Redis、反向代理配置或多 worker 共享状态。
- 不在本次修复 CORS、静态托管、trace、完整消息历史或 Desktop key 存储。
- public/admin key 不是上游供应商密钥；上游密钥继续只存在服务端配置中。
- 进程内保护降低误用和本地滥用风险，但公网部署仍应使用 TLS、反向代理和网络层
  访问控制。
