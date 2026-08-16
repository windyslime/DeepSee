# DeepSee + DSH 视觉安装指南

这份指南只覆盖 DeepSeek Harness (DSH) Web profile。DeepSee 网关负责视觉模型和
DeepSeek 上游凭证；DSH 只保存网关 public key,不会把视觉提供方密钥发送到浏览器或
DSV 请求体。

## 1. 启动网关

在 DeepSee 仓库或已安装 Python 环境中执行:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "seedeep[server]"
deepsee-server
```

首次启动时终端会显示 public key 和 admin key。只把 public key 提供给 DSH。视觉
提供方的 `DEEPSEEK_API_KEY`、`VISION_API_KEY`、`VISION_BASE_URL` 和
`VISION_MODEL` 留在 DeepSee 的 `deepsee.toml` 或网关环境中。

## 2. 安装 DSH 适配层

确认 DSH 已安装并存在 `~/.dsh/profiles/web` 后，在另一个终端执行:

```bash
export DEEPSEE_DSV_API_KEY='<DSV public key>'
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh | bash
```

安装器固定使用 `dsh-dsv-v0.1.0` Release。它会下载资产包并校验 SHA-256,将文件
缓存到 `~/.dsh/cache/deepsee-dsv/0.1.0`,然后备份并更新 Web profile 的
`package.json`、`cordis.patch.yml` 和 lockfile。重复执行是幂等的。

如果 DSH 不在默认目录:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh \
  | DSH_HOME="$HOME/.dsh" bash
```

`DSH_HOME` 应指向包含 `profiles/web` 的目录。不要把 API key 写入仓库文件。

## 3. 重启并检查

安装后重启当前 DSH Web 进程。若使用命令行启动 profile,重新执行原来的 Web 启动
命令即可；安装器不会强制终止正在运行的进程。

验证 profile 和网关:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh \
  | bash -s -- --verify
curl -fsS http://127.0.0.1:8712/health
```

`--verify` 会检查 DSV 包、`llm-dsv` Loader 行和网关健康端点。若网关未启动,它会
报告“已安装、待启动”,并提示执行 `deepsee-server`。

在 DSH 中发送一条含图片的消息。正常结果包含助手正文和一个可折叠的“识图”行；
识图行展示后端、模型、模式、耗时、缓存和追踪元数据。无图消息和标题/压缩等辅助
请求继续走原有 DSH provider。

## 4. 回滚或卸载

安装器会在 `~/.dsh/profiles/web/.deepsee-dsv-backups/` 保存时间戳备份。卸载只移除
它自己写入的依赖和 patch 行,保留其他用户配置:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/uninstall-dsh-dsv.sh | bash
```

如果依赖安装失败,安装器会自动恢复本次备份。也可以停止 DSH 后手工复制最近的备份
文件恢复 profile,再运行 `pnpm install`。

## 5. 常见问题

`No API key for provider: deepsee` 表示 DSH 进程没有读取到
`DEEPSEE_DSV_API_KEY`。在启动 DSH 的同一个终端导出 public key,再重启 DSH；这不是
视觉模型的 API key。

`/health` 不通表示网关没有运行、端口不同或被本机防火墙拦截。设置
`DEEPSEE_GATEWAY_URL` 可验证其他本地地址:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh \
  | DEEPSEE_GATEWAY_URL=http://127.0.0.1:8712 bash -s -- --verify
```

如果提示 DSH 版本不兼容,请使用 rc.5 Web profile 与固定 Release,不要混用其他提交
构建的前端包。安装器不会替换 DSH 本体。
