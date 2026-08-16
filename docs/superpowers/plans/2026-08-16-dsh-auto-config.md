# Optional DSH Connection Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the DSH-only DSV installer so users can choose automatic DeepSee connection configuration, install without touching credentials, or cancel safely.

**Architecture:** Keep shell orchestration in `install-dsh-dsv.sh`, isolate credential-file mutation in `dsh-credentials.py`, and keep profile patch generation in `dsh-profile.py`. The installer reads choices from `/dev/tty`, passes keys only through stdin, snapshots all mutable profile files plus credentials, and restores the snapshot on any post-mutation failure.

**Tech Stack:** Bash, Python 3 standard library, JSON/YAML-compatible text templates, pytest, existing DSH profile and Release asset tooling.

## Global Constraints

- DSH Web is the only supported target; do not modify other clients or DSH core packages.
- Never accept or print an API key in command-line arguments, logs, JSON output, or release assets.
- Store only the DSV public key reference `DEEPSEE_DSV_API_KEY` in the managed profile patch.
- Store credentials in `$DSH_HOME/.credentials.yaml` with file mode `0600` and preserve unrelated entries.
- `--no-configure` must not read, create, or modify the credential file.
- `--uninstall` removes only the managed DSV layer and never deletes credentials.
- Preserve unrelated user edits and never reset or checkout existing worktree changes.

---

### Task 1: Credential file helper

**Files:**
- Create: `scripts/dsh-credentials.py`
- Test: `tests/test_dsh_credentials.py`

**Interfaces:**
- `python3 scripts/dsh-credentials.py has --file PATH --name DEEPSEE_DSV_API_KEY` exits 0 when the YAML mapping contains a non-empty scalar and exits 1 otherwise without printing the value.
- `python3 scripts/dsh-credentials.py set --file PATH --name DEEPSEE_DSV_API_KEY --stdin` reads exactly one key from stdin, atomically replaces the file, preserves unrelated YAML lines, and applies mode `0600`.

- [ ] **Step 1: Write tests for missing, existing, and replacement credentials.** Assert `has` output contains only status text, `set` accepts a key only via stdin, unrelated lines survive, replacement is possible, and stdout/stderr never contains the secret.
- [ ] **Step 2: Run `pytest -q tests/test_dsh_credentials.py` and verify the new tests fail because the helper is absent.**
- [ ] **Step 3: Implement a conservative line-oriented YAML mapping editor using only the standard library.** Reject non-scalar or malformed target entries, write to a sibling temporary file, `chmod(0600)`, then `replace()` the destination; never include the value in diagnostics.
- [ ] **Step 4: Run the focused test file and verify all credential cases pass.**
- [ ] **Step 5: Commit `feat: add safe DSH credential helper`.**

### Task 2: Dynamic profile patch configuration

**Files:**
- Modify: `scripts/dsh-profile.py`
- Test: `tests/test_dsh_installer.py`

**Interfaces:**
- `install(..., gateway_url: str, api_key_ref: str)` renders the managed block with `baseURL` and `apiKeyEnv`.
- CLI accepts `--gateway-url URL` and `--api-key-ref NAME`; URL must be `http://` or `https://` and the reference must match `[A-Z][A-Z0-9_]*`.

- [ ] **Step 1: Add tests for the default managed block, custom gateway URL, update of an existing managed block, and preservation of a hand-written `llm-dsv` row.**
- [ ] **Step 2: Run the focused profile tests and confirm the new assertions fail.**
- [ ] **Step 3: Generate the block from validated arguments, replacing only the existing managed block; leave hand-written rows untouched.**
- [ ] **Step 4: Run `pytest -q tests/test_dsh_installer.py` and verify idempotency and uninstall behavior remain green.**
- [ ] **Step 5: Commit `feat: configure DSH DSV profile endpoint`.**

### Task 3: Installer choice flow, backups, and rollback

**Files:**
- Modify: `scripts/install-dsh-dsv.sh`
- Test: `tests/test_dsh_installer.py`

**Interfaces:**
- Options: `--configure`, `--no-configure`, `--verify`, `--uninstall`, and `--dry-run`.
- With no explicit choice on a TTY, prompt `Configure DeepSee connection automatically? [Y/n/c]`; `Y` configures, `n` installs only, and `c` exits before creating a backup.
- In non-interactive mode, no explicit choice exits with status 2 and explains that `--configure` or `--no-configure` is required.

- [ ] **Step 1: Add shell-level tests using temporary `DSH_HOME`, fake `curl`/`pnpm`/`dsh`, and `/dev/tty` input.** Cover cancel without writes, `--no-configure` not invoking the credential helper, `--configure` writing a supplied environment key, hidden TTY input, existing-key preservation, forced rotation, and rollback after a simulated `pnpm` or Loader failure.
- [ ] **Step 2: Run the new installer tests and confirm they fail against the current script.**
- [ ] **Step 3: Parse mutually exclusive mode flags and obtain interactive input from `/dev/tty`; refuse ambiguous non-TTY invocation.**
- [ ] **Step 4: In configure mode, prefer `DEEPSEE_DSV_API_KEY`, otherwise use `read -rs` from `/dev/tty`; validate non-empty input before any profile mutation and pipe it to `dsh-credentials.py set --stdin`.**
- [ ] **Step 5: Snapshot package, patch, lockfile, and credentials before mutation; restore every present/missing file on failures, including credential mode and content.**
- [ ] **Step 6: Pass `--gateway-url` and `--api-key-ref DEEPSEE_DSV_API_KEY` to the profile helper; keep no-configure mode on the existing profile values and avoid reading the key.**
- [ ] **Step 7: Run the focused installer tests, then exercise `--help`, `--dry-run`, `--configure`, `--no-configure`, `--verify`, and `--uninstall` in a temporary profile.**
- [ ] **Step 8: Commit `feat: make DSH connection configuration optional`.**

### Task 4: Documentation and command contract tests

**Files:**
- Modify: `README.md`
- Modify: `docs/DSH-DSV-INSTALL.zh.md`
- Modify: `docs/DSH-DSV-INSTALL.md`
- Modify: `tests/test_docs_install_command.py`

**Interfaces:**
- Documentation shows the interactive choices and exact non-interactive commands:
  `bash -s -- --configure`, `bash -s -- --no-configure`, `bash -s -- --verify`, and uninstall.

- [ ] **Step 1: Add documentation assertions for both configure modes, credential path, no-key logging guarantee, and DSH-only scope.**
- [ ] **Step 2: Run the docs tests and verify the new assertions fail.**
- [ ] **Step 3: Update the Chinese and English guides and README with the choice prompt, environment-key shortcut, restart/verify sequence, and rollback semantics.**
- [ ] **Step 4: Run `pytest -q tests/test_docs_install_command.py` and verify all documentation tests pass.**
- [ ] **Step 5: Commit `docs: document optional DSH auto configuration`.**

### Task 5: Full verification and public installer smoke test

**Files:**
- Modify only files produced by Tasks 1-4 if fixes are required.

- [ ] **Step 1: Run `pytest -q tests/test_dsh_credentials.py tests/test_dsh_installer.py tests/test_release_assets.py tests/test_docs_install_command.py`.**
- [ ] **Step 2: Run the existing DeepSee and DSH DSV test suites without changing unrelated user files.**
- [ ] **Step 3: Build a temporary DSH profile, invoke the published raw installer with `--no-configure` and `--configure`, and verify package checksums, dynamic patch URL, credential mode `0600`, health check output, and rollback behavior.**
- [ ] **Step 4: Confirm no secret value appears in captured stdout/stderr or generated release assets.**
- [ ] **Step 5: Push `main` and re-run the public raw command against a fresh temporary profile.**

## Self-review checklist

- Every design requirement maps to Tasks 1-5: choice handling, credential safety, dynamic URL, rollback, docs, and real smoke test.
- No task relies on an undefined helper or option; all interfaces are named above.
- No placeholders or open-ended “handle errors” steps remain; each failure mode has a concrete test or restore action.
