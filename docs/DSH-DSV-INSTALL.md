# DeepSee + DSH vision installation

This guide covers the DeepSeek Harness (DSH) Web profile only. DeepSee owns the vision
provider and DeepSeek upstream credentials. DSH stores only the DSV public key and never
sends provider credentials in a DSV request.

Start the gateway first:

```bash
pip install "seedeep[server]"
deepsee-server
```

Export the public key printed by the gateway in the same environment used to start DSH:

```bash
export DEEPSEE_DSV_API_KEY='<DSV public key>'
```

Install the pinned DSH adapter:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh | bash
```

The installer asks `Configure DeepSee connection automatically? [Y/n/c]`. Choose `Y` to
write the gateway URL and DSV public key, `n` to install packages while preserving the
existing credential and patch files, or `c` to cancel before any profile change. The prompt
reads from `/dev/tty`, so it also works when the script is piped from `curl`.

For non-interactive use, choose a mode explicitly:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh \
  | bash -s -- --configure
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh \
  | bash -s -- --no-configure
```

`--configure` uses `DEEPSEE_DSV_API_KEY` first and otherwise reads a hidden prompt. Explicit
configuration may rotate an existing key; an interactive `Y` keeps an existing key by
default. The key is passed to the helper over stdin, never placed in argv or output, and is
stored at `~/.dsh/.credentials.yaml` with mode `0600`.

The installer verifies the `dsh-dsv-v0.1.0` asset, backs up the Web profile, installs the
DSV packages, and adds the `llm-dsv` Loader row. Restart the existing DSH Web process after
installation. `--verify` performs a read-only profile and gateway check. Verify the profile
and gateway with:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh \
  | bash -s -- --verify
curl -fsS http://127.0.0.1:8712/health
```

Image turns show a collapsible vision row beside the normal assistant answer. Text turns and
auxiliary requests continue through the existing provider. The installer does not modify
other clients or write credentials to the repository.

To remove the managed DSV layer while retaining unrelated profile settings:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/uninstall-dsh-dsv.sh | bash
```

Uninstall removes only the managed DSV packages and patch row. It never deletes
`~/.dsh/.credentials.yaml`.

See the Chinese guide for the full restart, rollback, and troubleshooting sequence.
