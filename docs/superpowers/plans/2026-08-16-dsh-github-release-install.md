# DSH GitHub Release Installer Implementation Plan

> **For agentic workers:** Execute this plan task-by-task with a verification checkpoint after each task.

**Goal:** Publish DeepSee with a reproducible, DSH-only installer that installs a pinned DSV asset bundle, safely patches the DSH Web profile, and verifies the gateway connection.

**Architecture:** Keep DeepSee as the only source repository. Build a versioned DSV asset archive from the pinned `deepseek-harness` commit, publish that archive as a GitHub Release asset, and keep the installer/manifest/templates in the DeepSee repository. The installer validates tools and checksums before backing up and structurally merging the DSH profile, then verifies Loader composition and the local gateway without handling credentials.

**Tech Stack:** POSIX shell, Python 3 standard library for JSON/YAML-safe helper operations, GitHub CLI, DeepSee Python package, DSH pnpm profile.

## Global Constraints

- Only `github.com/windyslime/DeepSee` is pushed by this work.
- The installer modifies only `$DSH_HOME/profiles/web` and its versioned cache.
- The default Release is pinned; no floating `latest` download is allowed.
- API keys, image bytes, request bodies, and `/Users/...` development paths must never enter Git or installer output.
- Existing user dependencies and patch rows must be preserved; repeated installation must be idempotent.
- Profile changes require a timestamped backup and must be reversible.
- DSH compatibility is limited to the validated rc.5 asset contract.

---

### Task 1: Add release metadata and asset builder

**Files:**
- Create: `scripts/dsh-dsv-release.json`
- Create: `scripts/build-dsh-dsv-assets.sh`
- Create: `scripts/verify-dsh-dsv-assets.py`
- Test: `tests/test_release_assets.py`

**Interfaces:**
- `build-dsh-dsv-assets.sh <harness-repo> <output-dir> <commit>` creates an archive and manifest without absolute local paths.
- `verify-dsh-dsv-assets.py <archive> <manifest>` exits zero only when package names, hashes, and paths match the manifest.

- [ ] Write tests for rejecting absolute paths, unknown package names, checksum mismatches, and accepting the local tarball fixture.
- [ ] Run `python3 -m pytest tests/test_release_assets.py -q` and confirm the new tests fail before helpers exist.
- [ ] Build the script around a temporary staging directory, copy only the validated DSV package tarballs, rewrite Web app `file:` dependencies to archive-relative package versions, and emit SHA-256 entries.
- [ ] Make the verifier reject `..`, absolute paths, undeclared files, and package names outside the allowlist.
- [ ] Run the focused tests and verify the archive manifest contains the harness commit and rc.5 compatibility.
- [ ] Commit only the metadata, builder, verifier, and tests with `build: package pinned DSH DSV assets`.

### Task 2: Implement the DSH installer and rollback helper

**Files:**
- Create: `scripts/install-dsh-dsv.sh`
- Create: `scripts/uninstall-dsh-dsv.sh`
- Create: `scripts/dsh-profile.py`
- Test: `tests/test_dsh_installer.py`

**Interfaces:**
- `install-dsh-dsv.sh [--dry-run|--verify|--uninstall]` accepts `DSH_HOME`, `DEEPSEE_DSV_VERSION`, and `DEEPSEE_GATEWAY_URL`.
- `dsh-profile.py install|uninstall|verify --profile <path> --asset-root <path>` performs structured package/patch edits and returns JSON status on stdout.

- [ ] Write tests covering a missing profile, dry-run no-write behavior, idempotent insertion, preservation of an unrelated patch, and rollback after a simulated pnpm failure.
- [ ] Run the focused tests and confirm they fail before the helper and shell entrypoint exist.
- [ ] Implement JSON edits with Python's `json` module and YAML patch edits as a restricted list-of-maps transform; do not use text concatenation for package JSON or patch identity checks.
- [ ] Implement backup creation before mutation, atomic temporary-file replacement, and restoration on any post-mutation failure.
- [ ] Implement the shell preflight for macOS/Linux, `curl`, Python 3, Node, pnpm, DSH home, and the pinned release URL. Download into `$DSH_HOME/cache/deepsee-dsv/<version>` and verify before extraction.
- [ ] Run `pnpm install` only after profile edits are complete, then invoke the DSH config dump/loader check when available.
- [ ] Run the focused installer tests and a `--dry-run` against a temporary profile.
- [ ] Commit the installer, helper, tests, and uninstall script with `feat: add DSH-only DSV installer`.

### Task 3: Add gateway setup and DSH operation documentation

**Files:**
- Modify: `README.md`
- Create: `docs/DSH-DSV-INSTALL.zh.md`
- Create: `docs/DSH-DSV-INSTALL.md`
- Modify: `scripts/install-dsh-dsv.sh`
- Test: `tests/test_docs_install_command.py`

**Interfaces:**
- Documentation exposes one canonical command using the pinned `main` installer URL.
- Documentation describes `pip install "seedeep[server]"`, `deepsee-server`, gateway health verification, and DSH restart/verification without embedding credentials.

- [ ] Add a test that extracts the documented command and checks that it points to the repository installer and does not contain a key or local path.
- [ ] Add the DSH-only README section and bilingual operation guide with install, configure, restart, verify, uninstall, and troubleshooting steps.
- [ ] Add installer output that clearly distinguishes “installed” from “gateway reachable” and prints the exact next command for a stopped gateway.
- [ ] Run documentation tests and `python3 -m pytest tests/test_docs_install_command.py -q`.
- [ ] Commit docs and output wording with `docs: document DSH DSV installation`.

### Task 4: Build and publish the Release asset

**Files:**
- Modify: `scripts/dsh-dsv-release.json`
- Create: `dist/` release archive locally only (ignored from Git)

**Interfaces:**
- GitHub Release tag: `dsh-dsv-v0.1.0`.
- Asset: `deepsee-dsh-dsv-v0.1.0.tar.gz` plus its manifest/checksum sidecar.

- [ ] Build from harness commit `58fded97234e744c02fd165cfed9632cf6ccc61e`.
- [ ] Run the verifier against the generated archive and inspect that no `/Users/` or API key appears in it.
- [ ] Run `gh release create dsh-dsv-v0.1.0 ...` only after the archive passes verification and the DeepSee commit is ready.
- [ ] Query `gh release view dsh-dsv-v0.1.0` and compare asset names and checksums with the local manifest.

### Task 5: End-to-end validation and GitHub push

**Files:**
- No new source files; validate the files from Tasks 1-4.

- [ ] Run DeepSee focused tests for server, DSV, configuration, and installer helpers.
- [ ] Run installer dry-run, checksum failure, install, repeat install, verify, and uninstall against a temporary DSH profile.
- [ ] Run the existing DSH DSV tests from `packages/llm/llm-dsv`, UI tests, and Loader composition tests on the pinned harness commit.
- [ ] Confirm the running local endpoints respond: `http://127.0.0.1:8712/health` and `http://127.0.0.1:3081/`.
- [ ] Review staged diff for secrets, temp files, generated frames, and absolute paths.
- [ ] Commit the remaining release metadata if needed, push `main` to `origin`, and verify the public raw installer URL returns HTTP 200.

## Self-review checklist

- Every design section maps to at least one task: repository boundary (Tasks 3-5), fixed Release assets (Tasks 1 and 4), installer safety (Task 2), gateway verification (Tasks 2-3), rollback (Task 2), and DSH-only scope (all tasks).
- No task requires an unspecified API or a package outside the named DeepSee/DSH repositories.
- All tests have concrete paths and commands; no unresolved placeholder implementation step remains.
