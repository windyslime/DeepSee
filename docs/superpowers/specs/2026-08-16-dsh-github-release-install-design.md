# DSH 专用 GitHub 发布与一键安装设计

## 目标

将 DeepSee 网关项目发布到 `github.com/windyslime/DeepSee`，并提供一个只面向
DeepSeek Harness（DSH）Web profile 的可复现安装入口。安装后，DSH 可以把含图请求
路由到 DeepSee DSV 网关，同时保留 DSV 的独立视觉分析行和追问工具。

本设计不扩展 OpenAI、Anthropic、Gemini 或其他客户端的安装集成；DeepSee 现有的
多协议服务仍可作为网关能力保留，但一键安装器只修改 DSH。

## 发布边界

DeepSee 是唯一需要推送的仓库，目标 remote 为 `git@github.com:windyslime/DeepSee.git`。
DSH harness 的实现继续由其自身仓库和固定提交提供，DeepSee 仓库不复制 DSH 源码，
也不依赖开发机的 `.dsh/plugins/dsv-test` 临时目录。

发布由两层组成：

1. Git 仓库保存网关源码、安装器、版本清单、DSH profile 模板和中文文档。
2. GitHub Release 保存经过验证的 DSV 适配资产包。资产包包含 DSV LLM 插件、统一
   session/runtime/UI 适配包、匹配的 Web frontend，以及安装所需的 manifest 和
   SHA-256 校验值。

Release 版本使用独立标签，例如 `dsh-dsv-v0.1.0`，安装器默认固定到该版本；升级
必须显式传入版本或更新安装器的默认版本，避免上游资产漂移。

## 一键安装入口

仓库提供 `scripts/install-dsh-dsv.sh`，文档中的主命令为：

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh | bash
```

安装器的职责是：

- 检查 macOS/Linux、`curl`、Python 3、Node.js/pnpm 和现有 DSH home；不满足时给出
  可执行的修复提示并退出。
- 从固定 Release 下载单一资产包到 `$DSH_HOME/cache/deepsee-dsv/<version>`，先用
  manifest 校验每个文件的 SHA-256，再解包；任何校验失败都不修改 profile。
- 为 `$DSH_HOME/profiles/web/package.json` 和同目录的 `cordis.patch.yml` 创建带
  时间戳的备份，重复执行时保留最近一次可恢复备份。
- 以 JSON/YAML 结构化方式合并 DSV 相关本地 tarball 依赖和 `llm-dsv` 插入 patch，
  不覆盖已有的用户依赖或 patch 行。
- 在 profile 目录运行 `pnpm install --lockfile-only=false`，然后调用 DSH 的配置转储
  或启动检查，确认 `llm-dsv` 被 Loader 识别。
- 调用 `http://127.0.0.1:8712/health`；网关未运行时只报告“已安装、待启动”，不把
  连接失败伪装成安装成功。若检测到网关正在运行，再执行不含密钥的健康探测。

安装器支持环境变量 `DSH_HOME`、`DEEPSEE_DSV_VERSION`、`DEEPSEE_GATEWAY_URL`，
并提供 `--dry-run`、`--uninstall` 和 `--verify`。默认只操作 web profile，禁止把 API
key 写入文件或命令输出。

## 资产构建与兼容性

资产包由固定的 DSH harness 提交构建，并由发布前检查脚本生成。构建过程会把 Web app
包中的开发机绝对 `file:` 依赖改写为资产包内可解析的相对/固定版本依赖，保证安装后
不引用 `/Users/...` 路径。manifest 记录：DSH harness commit、Node/pnpm 约束、每个
tarball 的包名/版本/文件名/SHA-256，以及需要写入 profile 的依赖映射。

安装器只接受 manifest 中列出的包名，拒绝未知包和路径穿越；所有文件先写入版本化
缓存目录，profile 更新在校验和解包完成后一次性进行。目标 DSH 版本不满足 rc.5 兼容
范围时，安装器在修改前停止并报告版本冲突。

## 网关连接配置

DeepSee 网关继续通过现有 `seedeep[server]` 包安装和 `deepsee-server` 启动。DSH
安装器不接管 DeepSee API key，也不将上游视觉/DeepSeek 凭证写进 profile。文档提供
环境变量或 `deepsee.toml` 的配置示例，并明确使用 DSH 的 provider/model 设置将请求
送到 `DEEPSEE_GATEWAY_URL/v1/dsv`。

## 回滚与错误处理

所有会改变 profile 的步骤都在备份成功后执行。依赖安装、Loader 校验或连接验证失败
时，安装器恢复本次备份并保留下载缓存供诊断；网关本身的运行状态不会被安装器强制
终止。`--uninstall` 删除 DSV 插件依赖和插入 patch，但保留用户手工修改，并在无法
安全判断时拒绝自动删除而要求使用备份恢复。

错误输出包含下一步命令、profile 路径和 release 版本，不包含任何 credential、完整
请求体或图片内容。

## 文档与验收

README 增加“DSH 专用安装”章节，并链接到完整中文操作文档。验收至少包括：

- 在干净的临时 DSH profile 上执行安装器，下载资产、校验、安装和 Loader 组合成功。
- 重复执行安装器保持幂等，不重复插入 patch，不损坏用户已有行。
- `--dry-run` 不写文件，错误 checksum 不修改 profile，`--uninstall` 可恢复原状态。
- 网关运行时执行一次无图请求和一次含图请求，DSH UI 显示普通回答及独立视觉分析行。
- ShellCheck（若可用）、Python 测试、DSH 相关安装器测试和文档命令示例通过。

非目标是自动创建 GitHub 仓库、自动申请或轮换任何 API key、修改 DSH harness 上游
仓库，以及为非 DSH 客户端增加安装脚本。
