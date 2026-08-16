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

The installer verifies the `dsh-dsv-v0.1.0` asset, backs up the Web profile, installs the
DSV packages, and adds the `llm-dsv` Loader row. Restart the existing DSH Web process after
installation. Verify the profile and gateway with:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh \
  | bash -s -- --verify
curl -fsS http://127.0.0.1:8712/health
```

Image turns show a collapsible vision row beside the normal assistant answer. Text turns and
auxiliary requests continue through the existing provider. The installer does not modify
other clients or write API keys to the repository.

To remove the managed DSV layer while retaining unrelated profile settings:

```bash
curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/uninstall-dsh-dsv.sh | bash
```

See the Chinese guide for the full restart, rollback, and troubleshooting sequence.
