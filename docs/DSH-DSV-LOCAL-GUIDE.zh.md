# DeepSee、DSV 网关与 DSH 本地操作指南

本文说明如何在 macOS 上运行以下三层服务，并完成图片请求的端到端验证：

| 服务 | 地址 | 作用 |
| --- | --- | --- |
| DeepSee Web | `http://127.0.0.1:5173/` | DeepSee 自身的聊天与 API 管理界面 |
| DeepSee/DSV 网关 | `http://127.0.0.1:8712/` | 提供 `/health`、`/v1/dsv` 及兼容协议端点 |
| DSH Web | `http://127.0.0.1:3081/` | 安装了 `llm-dsv` 插件的 DeepSeek Harness |

`deepsee-server` 和“DSV 网关”不是两个进程。`deepsee-server` 就是网关，
DSV 是它提供的 `POST /v1/dsv` 协议端点。

## 当前机器上的状态

本文创建时，三个地址中已有以下后台任务：

```text
com.deepsee.gateway          -> 127.0.0.1:8712
com.deepsee.frontend         -> 127.0.0.1:5173
com.deepseek.dsh-dsv-test    -> 127.0.0.1:3081
```

网关当前使用仅限本机的开发模式：

```bash
python -m deepsee_server --no-auth --host 127.0.0.1 --port 8712 \
  --allow-browser-restart
```

DSH 中已经通过凭证服务配置 `DEEPSEE_DSV_API_KEY` 的本地开发占位值。
这使 `llm-dsv` 可以发出请求；由于网关处于 `--no-auth` 模式，该占位值不会
用于网关鉴权。

当前机器尚未配置 DeepSee 所需的 DeepSeek 上游密钥和视觉模型上游密钥。
因此健康检查与 DSV 路由可用，但真实图片推理会返回
`503 configuration_error`，直到完成“配置上游模型”一节。

## 目录与运行时

```text
DeepSee 核心与网关: /Users/jerrywu/Documents/DeepSee
DeepSee Web 前端:   /Users/jerrywu/Documents/DeepSee-Desktop/frontend
DSH 命令:           /Users/jerrywu/.nvm/versions/node/v22.22.0/bin/dsh
DSH 配置目录:       /Users/jerrywu/.dsh
```

当前使用 Python 3.10 虚拟环境：

```bash
/Users/jerrywu/Documents/DeepSee/.venv/bin/python --version
```

DeepSee Web 使用 Node.js 22 和 pnpm，依赖已安装在 `frontend/node_modules`。

## 配置上游模型

DSV v1 要求视觉后端为 `openai_compatible`。DeepSee 会先调用视觉模型分析
图片，再把分析结果和消息历史交给 DeepSeek 推理模型。

### 从浏览器配置（推荐）

1. 打开 `http://127.0.0.1:5173/`，进入“API 服务”→“设置”。
2. 在 DeepSeek 区域填写 API Key、Base URL 和模型名。DeepSeek 官方接口通常使用
   `https://api.deepseek.com` 和 `deepseek-chat`。
3. 在视觉服务区域填写 OpenAI-compatible 供应商的 API Key、Base URL 和模型名。
   例如通义千问兼容接口可使用
   `https://dashscope.aliyuncs.com/compatible-mode/v1` 和 `qwen-vl-max`。
4. 确认本地服务为 `127.0.0.1:8712`。当前 `--no-auth` 模式下 Local API Key 与
   Admin API Key 都留空；启用网关鉴权时分别填写 public key 与 admin key。
5. 点击“保存、重启并验证”。页面会先写入服务端配置，再等待网关出现新的
   `instanceId`，最后分别验证 DeepSeek 和视觉服务。
6. 两项均显示“已连接”后，再到 DSH 上传图片进行端到端测试。某一项失败时，
   页面会单独显示失败供应商和稳定错误码；已保存的新配置不会自动回滚。

API Key 输入框是只写字段。保存成功后会立即清空，刷新页面只会显示“已配置”，
服务端不会把密钥返回给浏览器，也不会把上游密钥写入 `sessionStorage`。浏览器
管理的完整配置保存在：

```text
~/.config/deepsee/upstream.json
```

目录权限为 `0700`，配置与上一版备份 `upstream.json.bak` 均为 `0600`。文件中的
上游密钥是本机明文，因此不要提交、同步或粘贴该文件。每次连接验证会发起一次
最小 DeepSeek 请求和一次包含测试图片的视觉请求，供应商可能对此产生少量计费。
页面中的“移除”会写入显式删除标记，因此不会意外重新启用 `deepsee.toml` 中的旧
密钥；“保留”则继续使用当前受管密钥或下层 TOML 配置。

自动重启同时要求网关由 launchd 管理，并以 `--allow-browser-restart` 启动。若页面
显示“请手动重启网关”，配置已经保存；在另一个终端重启网关即可，原页面会继续
等待新实例并自动进入验证。页面等待 30 秒后会报告超时，可在重启完成后再次点击
确认。重复点击在保存、重启和验证期间会被禁用。

如果任何上游字段来自 `DEEPSEEK_...`、`VISION_...` 或对应的
`DeepSee_...` 环境变量，页面会把该字段标为只读。环境变量优先于浏览器管理配置；
要更换该字段，必须修改网关的启动环境并重启进程。

### 用配置文件或环境变量配置

也可以在 `~/.config/deepsee/deepsee.toml` 中保存非机密配置，并用环境变量提供
密钥：

```toml
[deepseek]
api_key = "${DEEPSEEK_API_KEY}"
base_url = "https://api.deepseek.com"
model = "deepseek-chat"

[vision]
backend = "openai_compatible"
api_key = "${VISION_API_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen-vl-max"

[server]
host = "127.0.0.1"
port = 8712
```

上面的视觉服务只是 OpenAI-compatible 示例。更换供应商时，必须一起更换
`api_key`、`base_url` 和 `model`，不能把一个供应商的密钥发给另一个地址。

在启动网关的同一个终端中设置密钥：

```bash
export DEEPSEEK_API_KEY='填入 DeepSeek API key'
export VISION_API_KEY='填入视觉模型 API key'
```

检查配置是否能加载，同时避免输出密钥：

```bash
cd /Users/jerrywu/Documents/DeepSee
.venv/bin/python - <<'PY'
from deepsee.config import load_config

config = load_config()
print("DeepSeek:", config.deepseek.base_url, config.deepseek.model)
print("Vision:", config.vision.backend, config.vision.base_url, config.vision.model)
PY
```

如果使用下文当前的 `launchctl submit` 后台方式，需要注意它不会继承当前 shell
中新导出的变量。填好上游密钥后，最直观的验证方式是先在前台启动网关；需要
长期自动启动时，再把环境变量接入受保护的 LaunchAgent 或系统密钥服务。

## 前台启动：最适合首次调试

前台启动能直接看到错误，是首次配置上游模型时的推荐方式。

### 1. 启动 DSV 网关

开发模式只允许监听 loopback：

```bash
cd /Users/jerrywu/Documents/DeepSee
source .venv/bin/activate
export DEEPSEEK_API_KEY='填入 DeepSeek API key'
export VISION_API_KEY='填入视觉模型 API key'
python -m deepsee_server --no-auth --host 127.0.0.1 --port 8712
```

不要把 `--no-auth` 与 `0.0.0.0` 或局域网地址一起使用。程序会拒绝这种组合。

### 2. 启动 DeepSee Web

打开第二个终端：

```bash
cd /Users/jerrywu/Documents/DeepSee-Desktop/frontend
pnpm exec vite --host 127.0.0.1 --port 5173
```

访问 `http://127.0.0.1:5173/`。开发模式下 Local API Key 和 Admin API Key
可以留空。

### 3. 启动带 DSV 插件的 DSH

打开第三个终端：

```bash
/Users/jerrywu/.nvm/versions/node/v22.22.0/bin/dsh web --port 3081
```

访问 `http://127.0.0.1:3081/`。设置页的插件列表中应看到 `llm-dsv` 已挂载且
已启用。

## 当前后台任务的管理

当前会话使用 `launchctl submit` 管理后台进程。它们在当前 macOS 登录会话内保持
运行，但不是跨重启的永久 LaunchAgent；注销或重启后需要重新提交。

查看任务：

```bash
launchctl print gui/$(id -u)/com.deepsee.gateway
launchctl print gui/$(id -u)/com.deepsee.frontend
launchctl print gui/$(id -u)/com.deepseek.dsh-dsv-test
```

查看监听端口：

```bash
lsof -nP -iTCP:8712 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
lsof -nP -iTCP:3081 -sTCP:LISTEN
```

查看日志：

```bash
tail -f /Users/jerrywu/.config/deepsee/gateway.log
tail -f /Users/jerrywu/.config/deepsee/frontend.log
tail -f /Users/jerrywu/.dsh/dsv-test.log
```

停止当前任务：

```bash
launchctl remove com.deepsee.frontend
launchctl remove com.deepsee.gateway
launchctl remove com.deepseek.dsh-dsv-test
```

只停止需要重启的任务即可，不必同时停止三项。

重新提交本机开发模式网关：

```bash
launchctl submit \
  -l com.deepsee.gateway \
  -o /Users/jerrywu/.config/deepsee/gateway.log \
  -e /Users/jerrywu/.config/deepsee/gateway.log \
  -- /Users/jerrywu/Documents/DeepSee/.venv/bin/python \
  -m deepsee_server --no-auth --host 127.0.0.1 --port 8712 \
  --allow-browser-restart
```

重新提交 DeepSee Web：

```bash
launchctl submit \
  -l com.deepsee.frontend \
  -o /Users/jerrywu/.config/deepsee/frontend.log \
  -e /Users/jerrywu/.config/deepsee/frontend.log \
  -- /Users/jerrywu/.nvm/versions/node/v22.22.0/bin/node \
  /Users/jerrywu/Documents/DeepSee-Desktop/frontend/node_modules/vite/bin/vite.js \
  /Users/jerrywu/Documents/DeepSee-Desktop/frontend \
  --host 127.0.0.1 --port 5173
```

绝对 Node 路径是必要的，因为 launchd 的默认 `PATH` 不包含 NVM。

## DSH 的 DSV 凭证

`llm-dsv` 默认解析名为 `DEEPSEE_DSV_API_KEY` 的凭证。不要把视觉供应商的
`VISION_API_KEY` 放进 DSH；DSH 只应持有 DSV 网关的 public key。

在本机 `--no-auth` 开发模式中，可以通过 DSH 自己的凭证 API 写入非机密占位值：

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  --data '{
    "type":"client-request",
    "rpcId":"set-dsv-local",
    "method":"credentials.set",
    "payload":{
      "ref":"DEEPSEE_DSV_API_KEY",
      "value":"local-development-only"
    }
  }' \
  http://127.0.0.1:3081/api/credentials.set
```

确认凭证存在但不读取其值：

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  --data '{
    "type":"client-request",
    "rpcId":"describe-dsv-local",
    "method":"credentials.describe",
    "payload":{"refs":["DEEPSEE_DSV_API_KEY"]}
  }' \
  http://127.0.0.1:3081/api/credentials.describe
```

成功响应中应出现 `configured: true`。凭证文件位于
`~/.dsh/.credentials.yaml`，权限应为 `0600`；不要用覆盖写入的方式修改它，
否则可能删除已有的 `DEEPSEEK_API_KEY`。

## 启用网关鉴权

日常仅本机测试可以使用 `--no-auth`。需要让其他本机客户端共享、绑定非 loopback
地址或做更接近生产的验证时，应启用默认鉴权。

首次启动空的 key store 时，网关会生成 public/admin key。当前机器已经存在
`~/.config/deepsee/api-keys.json`，其中只保存摘要，无法从摘要恢复旧明文。
需要新的明文 key 时运行：

```bash
cd /Users/jerrywu/Documents/DeepSee
source .venv/bin/activate
export DEEPSEEK_API_KEY='填入 DeepSeek API key'
export VISION_API_KEY='填入视觉模型 API key'
python -m deepsee_server \
  --host 127.0.0.1 \
  --port 8712 \
  --create-recovery-keys
```

该命令会输出一组新的 Public API key 和 Admin API key，并启动服务。明文只显示
一次。Public key 配给 DSH 与 DeepSee Web；Admin key 只在 DeepSee Web 的请求
日志和密钥管理功能需要时填写。

在 DSH 中把 `DEEPSEE_DSV_API_KEY` 的占位值替换成新的 Public key。为避免密钥
进入 shell 历史，优先在 DSH 的凭证设置界面中输入；不要把它写进
`cordis.patch.yml`。DeepSee Web 的设置仅保存在当前浏览器会话，关闭标签页后会
清除。

## 分层验证

### 1. 网关健康检查

```bash
curl -sS http://127.0.0.1:8712/health
```

期望：

```json
{"status":"ok","instanceId":"每次网关启动生成的新实例标识"}
```

### 2. DeepSee Web 页面

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5173/
```

期望：`200`。

### 3. DSH 页面

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3081/
```

期望：`200`。

### 4. DSV 路由存在性

```bash
curl -sS -o /tmp/dsv-probe.json -w '%{http_code}\n' \
  -H 'Content-Type: application/json' \
  --data '{}' \
  http://127.0.0.1:8712/v1/dsv
cat /tmp/dsv-probe.json
```

期望：`400`，并指出 `messages` 必须是数组。这证明路由已启动；它不是服务故障。

### 5. 真实图片测试

1. 确认 DeepSeek 与视觉模型上游配置可加载。
2. 打开 `http://127.0.0.1:3081/`。
3. 新建对话并选择 DeepSeek 模型。
4. 上传或粘贴一张图片，然后提问。
5. 含图请求应由 `llm-dsv` 短路到 `/v1/dsv`；无图请求仍走原 DSH provider。
6. 回复中应出现可折叠的“识图分析”行，随后显示推理或最终回答。
7. 如需补充识图细节，可触发 `deepsee_vision_detail`；每次回答最多追问两轮。

同时观察日志：

```bash
tail -f /Users/jerrywu/.config/deepsee/gateway.log
tail -f /Users/jerrywu/.dsh/dsv-test.log
```

## 常见错误

| 表现 | 含义 | 处理 |
| --- | --- | --- |
| 连接 `8712` 失败 | 网关未运行 | 检查 `launchctl`、`lsof` 和 `gateway.log` |
| `503 configuration_error` | 网关已运行，但上游配置缺失或非法 | 配置并验证 DeepSeek/视觉模型三元组 |
| `401` 或 DSH `AUTH` | DSV public key 不匹配 | 更新 `DEEPSEE_DSV_API_KEY`，不要使用视觉供应商 key |
| DSH `MISSING_CREDENTIAL` | DSH 没有解析到 `DEEPSEE_DSV_API_KEY` | 通过凭证 API或设置界面写入 |
| `502` 且 vision 阶段失败 | 视觉供应商调用失败 | 检查 `VISION_API_KEY`、URL、模型名和网络 |
| `502` 且 reasoning 阶段失败 | DeepSeek 调用失败 | 检查 `DEEPSEEK_API_KEY`、URL和模型名 |
| 页面显示 `AUTH` | 对应上游拒绝密钥 | 核对该供应商的 API Key，不要混用两家供应商的密钥 |
| 页面显示 `RATE_LIMIT` | 对应上游触发限流 | 等待供应商限流窗口恢复后再次验证 |
| 页面显示 `TRANSPORT` | 网关无法连接上游 | 检查网络、代理、DNS 与 Base URL |
| 页面等待重启超时 | 网关没有出现新的 `instanceId` | 确认 launchd 启动命令带 `--allow-browser-restart`，或手动重启网关 |
| `5173` 页面打不开 | Vite 未运行 | 检查 `frontend.log`；后台启动必须使用绝对 Node 路径 |
| `address already in use` | 端口已有进程 | 用 `lsof` 确认进程，不要重复启动 |
| DeepSee 显示服务运行但聊天失败 | `/health` 正常不代表上游密钥有效 | 继续检查 `/v1/dsv` 响应和网关日志 |

## 安全边界

- `--no-auth` 只能用于 `127.0.0.1`、`::1` 或 `localhost`。
- DSH 只保存 DSV public key，不保存 `VISION_API_KEY`。
- DeepSee 上游密钥应来自环境变量或权限为 `0600` 的受管配置，不要提交到 Git。
- 浏览器只会提交新的上游密钥，不会读取已有密钥；`upstream.json` 只应由当前用户读取。
- `~/.dsh/.credentials.yaml` 和 `~/.config/deepsee/api-keys.json` 应保持 `0600`。
- `api-keys.json` 只保存 SHA-256 摘要；忘记明文时创建 recovery key，不要尝试
  从摘要恢复。
- 对外部署时还需要 TLS、反向代理共享限速以及正式的进程管理；当前
  `launchctl submit` 配置只用于本机开发。
