# Gateway Authentication And Abuse Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add fail-closed inbound API-key authentication plus process-local rate and concurrency protection to the DeepSee gateway.

**Architecture:** `deepsee_server.auth` owns hashed key persistence and request authentication. `deepsee_server.request_guard` owns fixed-window rate accounting and concurrency leases; application middleware composes both before endpoint parsing and keeps streaming leases until the response iterator closes. The CLI validates no-auth hosts before side effects, configures auth/guard state, and then starts Uvicorn.

**Tech Stack:** Python 3.10+, FastAPI/Starlette ASGI middleware, asyncio, standard-library hashing/file APIs, pytest, FastAPI TestClient/httpx.

## Global Constraints

- Direct `deepsee_server.app:app` imports fail closed on protected paths until authentication is configured.
- `GET /health` stays public.
- `--no-auth` is accepted only for `127.0.0.1`, `::1`, and `localhost`.
- Persist only SHA-256 key digests using atomic replacement and file mode `0600`.
- Public and admin scopes are not interchangeable.
- Defaults are 60 requests per 60 seconds, 8 concurrent inference requests, and a 2-second queue timeout.
- Streaming responses retain their concurrency lease through completion, failure, or cancellation.
- Do not add a production dependency.
- Do not modify CORS, static hosting, traces, complete message history, vision-mode forwarding, or Desktop key storage.

---

### Task 1: API Key Store And Fail-Closed Authentication

**Files:**
- Create: `deepsee_server/auth.py`
- Modify: `deepsee_server/app.py`
- Create: `tests/test_server/test_auth.py`
- Modify: `tests/test_server/test_app.py`

**Interfaces:**
- Produces: `ApiKeyStore(path)`, `CreatedApiKey`, `configure_api_key_store(store)`, `disable_api_key_auth()`, `api_key_auth_mode()`, `configured_api_key_store()`, `public_token(request)`, `admin_token(request)`, and `token_digest(token)`.
- Produces: request state field `deepsee_identity`, containing the validated token digest or client IP in explicit no-auth mode.

- [ ] **Step 1: Write failing public-interface tests**

Test `/health`, fail-closed protected routes, missing/invalid/scope-mismatched keys, successful public/admin access, key hashing, revocation, concurrent creation, plaintext exclusion, and `0600` permissions. Add an autouse fixture in existing endpoint tests that calls `disable_api_key_auth()` and resets with `configure_api_key_store(None)` so legacy behavior is explicit.

```python
def test_unconfigured_auth_fails_closed_for_direct_import():
    configure_api_key_store(None)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    response = client.get("/v1/models")
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "configuration_error"
```

- [ ] **Step 2: Run tests and confirm the missing module/middleware failures**

Run: `uv run pytest tests/test_server/test_auth.py tests/test_server/test_app.py -q`

Expected: new authentication tests fail before endpoint parsing; existing app tests remain runnable after their explicit no-auth fixture is added.

- [ ] **Step 3: Implement the key store and authentication middleware**

Use `threading.RLock`, `NamedTemporaryFile`, `os.replace`, `os.fsync`, `secrets.token_urlsafe`, `hashlib.sha256`, and `hmac.compare_digest`. Match paths exactly:

```python
PUBLIC_PATHS = {
    "/v1/models",
    "/v1/chat/completions",
    "/v1/messages",
    "/analyze",
}

def required_scope(path: str) -> KeyScope | None:
    if path in PUBLIC_PATHS or (
        path.startswith("/v1beta/models/") and path.endswith(":generateContent")
    ):
        return "public"
    if path.startswith("/admin/"):
        return "admin"
    return None
```

Authentication errors use `{"error":{"message":...,"type":...}}`. Do not call `request.body()`, `_current_config()`, or an endpoint on rejected requests.

- [ ] **Step 4: Add health and admin key-management endpoints**

Implement `GET /health`, `GET /admin/keys`, `POST /admin/keys`, and `DELETE /admin/keys/{key_id}`. Validate create payload scope as `public` or `admin` and label as a non-empty string; return a newly created plaintext key only from the create response.

- [ ] **Step 5: Run authentication and legacy endpoint tests**

Run: `uv run pytest tests/test_server/test_auth.py tests/test_server/test_app.py -q`

Expected: PASS.

### Task 2: Fixed-Window Rate Limiting And Concurrency Leases

**Files:**
- Create: `deepsee_server/request_guard.py`
- Create: `tests/test_server/test_request_guard.py`

**Interfaces:**
- Produces: `RequestGuard(max_concurrent: int, queue_timeout: float, rate_limit: int, rate_window: float)`.
- Produces: `async RequestGuard.acquire(identity: str) -> GuardLease`.
- Produces: `RateLimitExceeded(retry_after: int)` and `QueueTimeout()` exceptions.
- Produces: idempotent `async GuardLease.release() -> None` plus async context-manager methods.

- [ ] **Step 1: Write failing behavioral tests**

Use an injected monotonic clock only if needed for deterministic fixed-window tests. Verify same-identity exhaustion, identity isolation, integer `Retry-After`, queue timeout, lease reuse, and idempotent release.

```python
guard = RequestGuard(max_concurrent=1, queue_timeout=0.01, rate_limit=2, rate_window=60)
first = await guard.acquire("client-a")
with pytest.raises(QueueTimeout):
    await guard.acquire("client-b")
await first.release()
second = await guard.acquire("client-b")
await second.release()
```

- [ ] **Step 2: Run guard tests and confirm failure**

Run: `uv run pytest tests/test_server/test_request_guard.py -q`

Expected: FAIL because `deepsee_server.request_guard` does not exist.

- [ ] **Step 3: Implement the minimal guard**

Protect fixed-window dictionaries with `asyncio.Lock`; use `asyncio.Semaphore` and `asyncio.wait_for` for the concurrency queue. Validate all constructor values as positive. Count an accepted request before waiting for concurrency so bursts cannot bypass the rate limit by filling the queue.

- [ ] **Step 4: Run guard tests**

Run: `uv run pytest tests/test_server/test_request_guard.py -q`

Expected: PASS.

### Task 3: Application Guard Integration And Streaming Lifecycle

**Files:**
- Modify: `deepsee_server/app.py`
- Modify: `tests/test_server/test_auth.py`
- Modify: `tests/test_server/test_request_guard.py`

**Interfaces:**
- Consumes: `request.state.deepsee_identity` from authentication.
- Consumes: `RequestGuard.acquire(identity)` and `GuardLease.release()`.
- Produces: `configure_request_guard(guard: RequestGuard | None) -> None` for CLI/tests; `None` disables guard accounting but does not alter auth mode.

- [ ] **Step 1: Write failing HTTP lifecycle tests**

Verify only inference paths are guarded; rate exhaustion returns 429 with `Retry-After`; queue timeout returns 503 with `Retry-After: 1`; malformed authenticated requests do not retain a lease; and auth failures never reach guard/config/body parsing. Use an async client/ASGI transport for overlapping requests.

- [ ] **Step 2: Write failing streaming release tests**

Cover normal stream completion, generator exception, and iterator cancellation/close. The second request must time out while the first stream is open and must acquire after the stream closes.

- [ ] **Step 3: Integrate request protection**

Guard exactly the five inference paths. Convert exceptions to:

```python
JSONResponse(
    {"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}},
    status_code=429,
    headers={"Retry-After": str(exc.retry_after)},
)
```

For non-streaming responses, release after `call_next` completes. For `StreamingResponse`, replace `response.body_iterator` with an async generator whose `finally` block calls `lease.release()`. Make release idempotent to cover response errors and cancellation without double-incrementing the semaphore.

- [ ] **Step 4: Run focused server tests**

Run: `uv run pytest tests/test_server/test_auth.py tests/test_server/test_request_guard.py tests/test_server/test_app.py -q`

Expected: PASS.

### Task 4: CLI Configuration, Host Validation, And Recovery Keys

**Files:**
- Modify: `deepsee_server/config.py`
- Modify: `deepsee_server/__main__.py`
- Modify: `tests/test_server/test_server_config.py`

**Interfaces:**
- Produces: `GuardSettings(max_concurrent, queue_timeout, rate_limit, rate_window)` and `request_guard_settings()`.
- Produces: `validate_no_auth_host(host: str) -> None` accepting exactly `127.0.0.1`, `::1`, and `localhost`.
- CLI flags: `--keys-file PATH`, `--no-auth`, and `--create-recovery-keys`.

- [ ] **Step 1: Write failing config and CLI tests**

Parametrize allowed and denied hosts, including empty host. Test default and environment-overridden guard values plus rejection of zero, negative, NaN, infinity, and nonnumeric values. Mock key creation and `uvicorn.run` to prove invalid no-auth hosts cause no side effects.

- [ ] **Step 2: Run config tests and confirm failure**

Run: `uv run pytest tests/test_server/test_server_config.py -q`

Expected: FAIL for missing settings, flags, and validation helper.

- [ ] **Step 3: Implement configuration parsing and CLI startup order**

Startup order must be: parse arguments, validate no-auth host, load/validate guard settings, configure auth and create first/recovery keys if required, configure request guard, then call `uvicorn.run`. The default key path is `Path.home() / ".config" / "deepsee" / "api-keys.json"`.

First authenticated startup creates one public key labeled `default-public` and one admin key labeled `default-admin` only when the store is empty. `--create-recovery-keys` always adds a new pair without revoking old records. Print plaintext only for keys created in that invocation.

- [ ] **Step 4: Run config and authentication tests**

Run: `uv run pytest tests/test_server/test_server_config.py tests/test_server/test_auth.py -q`

Expected: PASS.

### Task 5: Documentation And Full Verification

**Files:**
- Modify: `README.md`
- Create or modify: `.ohmycodex/plans/implementation.md`

**Interfaces:**
- Documents: default authenticated startup, one-time key output, public/admin headers, loopback-only no-auth mode, recovery keys, environment overrides, and process-local multi-worker limitation.

- [ ] **Step 1: Update operator documentation**

Add runnable examples using placeholders rather than real keys:

```bash
deepsee-server
curl -H 'Authorization: Bearer <public-key>' http://127.0.0.1:8712/v1/models
deepsee-server --no-auth --host 127.0.0.1
deepsee-server --create-recovery-keys
```

- [ ] **Step 2: Run focused and full verification**

Run:

```bash
uv run pytest tests/test_server/test_auth.py -q
uv run pytest tests/test_server/test_request_guard.py -q
uv run pytest tests/test_server/test_server_config.py -q
uv run pytest tests/test_server/test_app.py -q
uv run pytest -q
uv build
git diff --check
```

Expected: all tests pass, build succeeds, and diff check emits no errors.

- [ ] **Step 3: Record evidence and residual risks**

Write exact command results to `.ohmycodex/plans/implementation.md`. Record that rate/concurrency state is process-local and that public exposure still requires TLS, reverse-proxy controls, and shared limiting for multiple workers or instances.
