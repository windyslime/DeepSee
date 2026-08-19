# Browser Upstream API Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let DeepSee Web persist complete DeepSeek and vision provider settings, restart a supervised gateway, and verify both upstream connections after restart without returning secrets to the browser.

**Architecture:** A new server-owned `UpstreamConfigStore` atomically manages a `0600` JSON document and overlays it between environment variables and the existing TOML loader. FastAPI Admin endpoints expose a redacted projection, accept explicit credential mutations, schedule an opt-in supervised restart, and independently verify both providers. The existing desktop bridge and settings sheet implement the save/restart/verify state machine while keeping upstream secrets in local password-input state only.

**Tech Stack:** Python 3.10, FastAPI/Starlette, httpx, pytest, React 19, TypeScript, Vite, Vitest, Testing Library.

## Global Constraints

- Existing user changes in both repositories must be preserved; stage only task-owned files or hunks.
- Managed config directory mode is `0700`; config and backup files are `0600`.
- API keys are write-only and may never appear in GET responses, logs, exceptions, browser storage, or test snapshots.
- Effective precedence is environment > managed JSON > TOML > defaults.
- Automatic restart requires both `--allow-browser-restart` and launchd's `XPC_SERVICE_NAME`.
- DSV v1 accepts only the `openai_compatible` vision backend.
- Verification executes both providers independently and returns sanitized stable errors.
- Existing `/health` consumers remain compatible when `instanceId` is added.

---

### Task 1: Managed Upstream Configuration Store

**Files:**
- Create: `deepsee_server/upstream_config.py`
- Create: `tests/test_server/test_upstream_config.py`

**Interfaces:**
- Produces: `UpstreamConfigStore(path)`, `ManagedUpstreamConfig`, `ManagedProviderConfig`, `load_effective_config(store, env=None)`, and `redacted_config_view(store, env=None)`.
- Consumes: existing `deepsee.config.loader.load_config` and `ConfigError`.

- [ ] **Step 1: Write failing storage and precedence tests**

Cover absent/default projection, valid round-trip, corrupt JSON, schema rejection, atomic replacement, `.bak`, modes, environment shadowing, managed-over-TOML precedence, and missing-key candidate rejection. Key assertions:

```python
store = UpstreamConfigStore(tmp_path / "upstream.json")
store.save(candidate)
assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
assert "vision-secret" not in json.dumps(store.redacted_view())

cfg = load_effective_config(
    store,
    env={"DeepSee_DEEPSEEK_API_KEY": "env-key"},
)
assert cfg.deepseek.api_key == "env-key"
assert store.redacted_view(env={"DeepSee_DEEPSEEK_API_KEY": "env-key"})[
    "deepseek"
]["keyWritable"] is False
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```bash
cd /Users/jerrywu/Documents/DeepSee
.venv/bin/pytest tests/test_server/test_upstream_config.py -q
```

Expected: collection fails because `deepsee_server.upstream_config` does not exist.

- [ ] **Step 3: Implement typed parsing and atomic storage**

Implement frozen dataclasses and a single store owner. Use `json.loads`, exact field validation, `tempfile.NamedTemporaryFile` in the destination directory, `flush`, `os.fsync`, `os.replace`, and explicit modes. `save()` writes the previous valid payload to `<path>.bak` before committing a different candidate.

Core signatures:

```python
@dataclass(frozen=True)
class ManagedProviderConfig:
    api_key: str | None
    base_url: str
    model: str

@dataclass(frozen=True)
class ManagedUpstreamConfig:
    deepseek: ManagedProviderConfig
    vision: ManagedProviderConfig
    vision_backend: str = "openai_compatible"

class UpstreamConfigStore:
    def __init__(self, path: str | os.PathLike[str]) -> None: ...
    def load(self) -> ManagedUpstreamConfig | None: ...
    def save(self, config: ManagedUpstreamConfig) -> None: ...
    def redacted_view(self, env: Mapping[str, str] | None = None) -> dict: ...

def load_effective_config(
    store: UpstreamConfigStore,
    env: Mapping[str, str] | None = None,
) -> Config: ...
```

Managed values are injected into a copy of the environment only where neither the prefixed nor bare environment key exists, then passed to `load_config(env=...)`. This preserves the existing loader as the only semantic validator.

- [ ] **Step 4: Run focused tests**

Run the same pytest command. Expected: all tests pass.

---

### Task 2: Restart Controller and Connection Verifier

**Files:**
- Create: `deepsee_server/runtime_control.py`
- Create: `deepsee_server/connection_verify.py`
- Create: `tests/test_server/test_runtime_control.py`
- Create: `tests/test_server/test_connection_verify.py`

**Interfaces:**
- Produces: `RestartController`, `configure_restart_controller`, `configured_restart_controller`, `verify_upstream_connections`.
- Consumes: effective `Config`, `ask_async`, `describe_image_async`, and Starlette background tasks.

- [ ] **Step 1: Write failing restart-controller tests**

Prove restart is unsupported without explicit enablement or without `XPC_SERVICE_NAME`; prove `request_restart()` schedules exactly one delayed SIGTERM and never logs environment values.

```python
controller = RestartController(
    enabled=True,
    environment={"XPC_SERVICE_NAME": "com.deepsee.gateway"},
    terminate=lambda: calls.append("terminate"),
    schedule=lambda delay, callback: scheduled.append((delay, callback)),
)
assert controller.supported is True
controller.request_restart()
scheduled[0][1]()
assert calls == ["terminate"]
```

- [ ] **Step 2: Write failing verifier tests**

Stub both provider calls. Assert both execute under partial failure and responses contain only stable codes/messages and non-negative latency.

```python
result = await verify_upstream_connections(config, deepseek_probe=bad, vision_probe=good)
assert result["deepseek"] == {
    "ok": False,
    "latencyMs": 0,
    "error": {"code": "AUTH", "message": "认证失败"},
}
assert result["vision"]["ok"] is True
assert "secret" not in json.dumps(result)
```

- [ ] **Step 3: Run tests and confirm failure**

```bash
.venv/bin/pytest \
  tests/test_server/test_runtime_control.py \
  tests/test_server/test_connection_verify.py -q
```

Expected: module import failures.

- [ ] **Step 4: Implement restart control and verification**

`RestartController` owns capability detection and idempotent scheduling. The production terminator sends SIGTERM to `os.getpid()` from a short `threading.Timer` scheduled as a response background action.

The verifier calls:

```python
await ask_async("Reply OK", config=config, max_tokens=1)
await describe_image_async(TEST_PNG_BYTES, "Reply OK", config=config)
```

Map HTTP 401/403 to `AUTH`, 429 to `RATE_LIMIT`, remaining provider failures to `UPSTREAM`, timeouts/network failures to `TRANSPORT`, and unexpected failures to `INTERNAL`. Never return exception text.

- [ ] **Step 5: Run focused tests**

Expected: both test files pass.

---

### Task 3: FastAPI Admin Configuration API and CLI Wiring

**Files:**
- Modify: `deepsee_server/app.py`
- Modify: `deepsee_server/__main__.py`
- Create: `tests/test_server/test_admin_config.py`
- Modify: `tests/test_server/test_main.py`

**Interfaces:**
- Produces: `GET /admin/config`, `POST /admin/config`, `POST /admin/config/verify`, `/health.instanceId`, `--allow-browser-restart`.
- Consumes: Task 1 store/projection/effective loader and Task 2 controller/verifier.

- [ ] **Step 1: Write failing API tests**

Add isolated TestClient coverage for:

```python
response = client.get("/admin/config")
assert response.status_code == 200
assert "apiKey" not in json.dumps(response.json())

saved = client.post("/admin/config", json={
    "deepseek": {
        "baseUrl": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key": {"action": "replace", "value": "deepseek-secret"},
    },
    "vision": {
        "backend": "openai_compatible",
        "baseUrl": "https://vision.example/v1",
        "model": "vision-model",
        "key": {"action": "replace", "value": "vision-secret"},
    },
})
assert saved.json() == {
    "saved": True,
    "restartRequired": True,
    "restartSupported": True,
}
```

Also test keep/remove, environment conflicts, malformed bodies, failed candidate validation without write, auth, post-response restart scheduling, independent verify results, and stable `instanceId` per process.

- [ ] **Step 2: Run API tests and confirm failure**

```bash
.venv/bin/pytest tests/test_server/test_admin_config.py tests/test_server/test_main.py -q
```

Expected: 404s and missing CLI option.

- [ ] **Step 3: Wire store/controller configuration seams**

Add module-level defaults and test setters in `app.py`:

```python
_INSTANCE_ID = uuid.uuid4().hex
_upstream_store = UpstreamConfigStore(default_upstream_config_path())

def configure_upstream_store(store: UpstreamConfigStore) -> None: ...
```

Change `_current_config()` to `load_effective_config(_upstream_store)`. Extend health to:

```python
return {"status": "ok", "instanceId": _INSTANCE_ID}
```

- [ ] **Step 4: Implement strict request parsing and Admin routes**

Use helper functions for exact object/string/key-action validation. Build the complete candidate in memory, validate with `load_effective_config`, save, then return `JSONResponse` with a `BackgroundTask` only when restart is supported. Verification calls Task 2 and always returns both provider results.

- [ ] **Step 5: Add CLI option and production controller setup**

In `__main__.py`, add `--allow-browser-restart`, configure the controller before `uvicorn.run`, and reset it in tests. Preserve all existing auth, request-guard, recovery-key, and web-dist behavior.

- [ ] **Step 6: Run focused and server suites**

```bash
.venv/bin/pytest \
  tests/test_server/test_upstream_config.py \
  tests/test_server/test_runtime_control.py \
  tests/test_server/test_connection_verify.py \
  tests/test_server/test_admin_config.py \
  tests/test_server/test_main.py \
  tests/test_server/test_app.py -q
```

Expected: all pass.

---

### Task 4: Desktop Bridge Contracts and Restart/Verify State

**Files:**
- Modify: `/Users/jerrywu/Documents/DeepSee-Desktop/frontend/src/types.ts`
- Modify: `/Users/jerrywu/Documents/DeepSee-Desktop/frontend/src/bridge/DesktopBridge.ts`
- Modify: `/Users/jerrywu/Documents/DeepSee-Desktop/frontend/src/bridge/HttpDesktopBridge.ts`
- Modify: `/Users/jerrywu/Documents/DeepSee-Desktop/frontend/src/bridge/MockDesktopBridge.ts`
- Modify: `/Users/jerrywu/Documents/DeepSee-Desktop/frontend/src/state/AppStateContext.tsx`
- Create: `/Users/jerrywu/Documents/DeepSee-Desktop/frontend/src/__tests__/UpstreamConfigurationBridge.test.ts`

**Interfaces:**
- Produces: `UpstreamConfigView`, `UpstreamConfigDraft`, `UpstreamVerification`, bridge methods `loadUpstreamConfig`, `saveUpstreamConfig`, `verifyUpstreamConfig`, and `waitForGatewayRestart`.
- Consumes: Task 3 Admin API.

- [ ] **Step 1: Add failing HTTP bridge tests**

Assert Admin key headers, exact request shapes, write-only secret behavior, redacted response parsing, instance-id restart polling, timeout, and verify results. The test must inspect `sessionStorage` and prove neither submitted key appears.

- [ ] **Step 2: Run focused test and confirm type/test failure**

```bash
cd /Users/jerrywu/Documents/DeepSee-Desktop/frontend
pnpm test -- UpstreamConfigurationBridge.test.ts
```

- [ ] **Step 3: Add types and bridge methods**

Use these public shapes:

```ts
export type CredentialMutation =
  | { action: "keep" }
  | { action: "remove" }
  | { action: "replace"; value: string };

export interface UpstreamConfigDraft {
  deepseek: { baseUrl: string; model: string; key: CredentialMutation };
  vision: {
    backend: "openai_compatible";
    baseUrl: string;
    model: string;
    key: CredentialMutation;
  };
}

export interface UpstreamVerification {
  deepseek: ProviderVerification;
  vision: ProviderVerification;
}
```

Admin calls use `adminHeaders()`. `getGatewayHealth()` parses both status and `instanceId`; `waitForGatewayRestart(previousId, 30_000)` accepts an injected polling clock in tests or a private bounded loop in production.

- [ ] **Step 4: Integrate AppState actions without storing secrets**

Expose load/save/verify actions that return values to `SettingsSheet`; do not add draft key values to global state. Keep only redacted configured state and nonsecret fields in `DeepSeeSettings`.

- [ ] **Step 5: Run bridge and state tests**

```bash
pnpm test -- UpstreamConfigurationBridge.test.ts AppStateContext.test.tsx HttpDesktopBridge.test.ts
```

Expected: all pass.

---

### Task 5: Settings UI, End-to-End Component Flow, and Operations

**Files:**
- Modify: `/Users/jerrywu/Documents/DeepSee-Desktop/frontend/src/features/api/SettingsSheet.tsx`
- Modify: `/Users/jerrywu/Documents/DeepSee-Desktop/frontend/src/styles.css`
- Create: `/Users/jerrywu/Documents/DeepSee-Desktop/frontend/src/__tests__/UpstreamConfigurationFlow.test.tsx`
- Modify: `docs/DSH-DSV-LOCAL-GUIDE.zh.md`

**Interfaces:**
- Consumes: Task 4 bridge/actions.
- Produces: accessible complete settings workflow and updated operating instructions.

- [ ] **Step 1: Write failing component tests**

Cover initial redacted load, fields and labels, keep/replace/remove actions, validation, disabled submit, automatic restart polling, manual restart wait, partial verification failure, retry, and password clearing.

```tsx
await user.type(screen.getByLabelText("DeepSeek API Key"), "new-secret");
await user.click(screen.getByRole("button", { name: "保存、重启并验证" }));
expect(await screen.findByText("DeepSeek 已连接")).toBeVisible();
expect(screen.getByText("视觉服务已连接")).toBeVisible();
expect(screen.getByLabelText("DeepSeek API Key")).toHaveValue("");
```

- [ ] **Step 2: Run component test and confirm failure**

```bash
pnpm test -- UpstreamConfigurationFlow.test.tsx
```

- [ ] **Step 3: Implement the settings sections and state machine**

Use compact full-width configuration bands rather than nested cards. Use password inputs for keys, a select for Backend, icon buttons with tooltips for delete/retry, and stable status rows for `saving`, `restarting`, `verifying`, `success`, and `error`. Preserve the existing portal/focus trap and local/admin key controls.

- [ ] **Step 4: Add responsive and accessibility styles**

Ensure labels and errors fit mobile width, status rows do not shift layout, focus remains visible, buttons have stable dimensions, and reduced-motion behavior avoids spinner animation.

- [ ] **Step 5: Update operations guide**

Document browser-managed storage path, Admin key requirement, `--allow-browser-restart`, connection-validation token cost, automatic/manual restart states, and how to clear or replace credentials.

- [ ] **Step 6: Run full verification**

Backend:

```bash
cd /Users/jerrywu/Documents/DeepSee
.venv/bin/pytest -q
```

Frontend:

```bash
cd /Users/jerrywu/Documents/DeepSee-Desktop/frontend
pnpm test
pnpm lint
pnpm build
```

Expected: all commands pass.

- [ ] **Step 7: Restart current services and perform browser QA**

Re-submit `com.deepsee.gateway` with `--allow-browser-restart`, retain `127.0.0.1` binding, and keep `com.deepsee.frontend` running. Verify `/health`, the settings workflow at `http://127.0.0.1:5173/`, desktop/mobile layout, browser console, and that no secret appears in DOM snapshots or browser storage.

- [ ] **Step 8: Review changes without staging unrelated work**

Run `git diff --check` in both repositories. Stage only newly created files and owned hunks if commits are requested; do not stage pre-existing user modifications.
