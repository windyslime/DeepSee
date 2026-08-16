# DSH 自动连接配置设计

## 目标

扩展 DSH 专用一键安装器，让用户可以选择是否自动配置 DeepSee 与 DSH 的连接。
自动配置时，安装器把 DSV 网关地址写入托管的 `llm-dsv` profile patch，并把 DSV
public key 安全写入 `$DSH_HOME/.credentials.yaml`；仅安装时不写入任何凭证。

## 用户选择

交互式终端默认显示一次选择：

```text
Configure DeepSee connection automatically? [Y/n/c]
Y  configure gateway URL and ask for the DSV public key when needed
n  install the DSV packages only; keep existing credentials and config
c  cancel before changing the profile
```

非交互环境必须显式传入 `--configure` 或 `--no-configure`，否则退出并说明原因。
`--configure` 使用 `DEEPSEE_DSV_API_KEY` 环境变量中的 key，或从 `/dev/tty` 隐藏读取；
`--no-configure` 保留现有 credential 文件和 patch 配置。`--verify` 只读检查，不弹出
输入框；`--uninstall` 不删除 credential，因为它可能被其他 DSH provider 使用。

## 数据与文件

- `~/.dsh/.credentials.yaml` 是 DSH 的 provider-managed 凭证文件。写入前创建安装备份，
  保持其他 key、注释和格式，写后设置权限 `0600`。凭证 helper 不打印值，也不接收
  命令行 key 参数。
- `~/.dsh/profiles/web/cordis.patch.yml` 的托管块包含 `baseURL` 和
  `apiKeyEnv: DEEPSEE_DSV_API_KEY`。URL 只允许 `http://` 或 `https://`，不会把 key
  写入 YAML。
- `$DSH_HOME/cache/deepsee-dsv/<version>` 继续保存已校验的 Release 资产和 helper。

## 原子性与回滚

凭证文件、profile package、patch 和 lockfile 一起备份。任何 checksum、profile 写入、
pnpm 安装或 Loader 验证失败都会恢复本次备份；取消选择不会创建或修改备份。凭证
helper 使用临时文件替换，避免半写文件。

## 验收

- 交互式选择自动配置会安全写入 key、配置 URL、完成健康检查；选择仅安装不会读取或
  修改 key。
- `--configure` 和 `--no-configure` 在无 TTY 环境可重复运行；缺少 key 时
  `--configure` 在 profile 修改前失败。
- 已有 key 保持不变，`--configure` 才允许轮换；`--uninstall` 保留 key。
- 测试覆盖权限 `0600`、key 不出现在 stdout/stderr、取消无写入、回滚和幂等。
