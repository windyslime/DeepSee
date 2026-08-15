# DSV Public Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement DeepSee's public `POST /v1/dsv` protocol so DSH can receive independent vision, answer, reasoning, and tool-call SSE events while DeepSee retains all vision and DeepSeek orchestration.

**Architecture:** Add a dedicated DSV protocol adapter that normalizes DSV image blocks to the existing OpenAI message shape, reuses the existing vision-context transformer, and converts DeepSeek responses into a stable DSV envelope. Add one FastAPI route with public authentication, request limits, trace metadata, and lease lifecycle identical to the existing inference routes. Existing compatibility endpoints remain unchanged.

**Tech Stack:** Python >= 3.10, FastAPI, httpx, pytest.

## Global Constraints

- `POST /v1/dsv` is the public DeepSee orchestration/output endpoint.
- DeepSee calls the configured vision provider internally; DSH never receives or sends provider API keys.
- DSV v1 requires the configured vision backend to use the OpenAI-compatible provider adapter.
- DSV returns tool calls but never executes DSH tools; DSH sends tool results in a later DSV request.
- Vision analysis is emitted once as a complete event before reasoning/answer events.
- Existing compatibility endpoints and their response shapes must not change.
- Preserve the current 32 MiB body limit, image validation, SSRF protection, API-key auth, request guard, tracing, and cancellation behavior.

---

### Task 1: Define DSV request and response adapter

**Files:**
- Create: `deepsee_server/protocols/dsv.py`
- Modify: `deepsee_server/protocols/__init__.py`
- Test: `tests/test_protocols/test_dsv.py`

**Interfaces:**
- Produces `ParsedDsvRequest(messages, stream, params, vision_mode, include_analysis, image_count)`.
- Produces `parse_request(body)` that accepts DSV `vision` options and both DSV base64 image blocks and OpenAI `image_url` blocks.
- Produces `encode_response(...)` for non-streaming DSV envelopes.
- Produces `encode_stream(chunks, ...)` for DSV SSE events, including `vision.completed`, reasoning/answer deltas, tool calls, `response.requires_action`, `error`, and terminal `response.completed`.

- [x] **Step 1: Write parser and encoder tests** covering valid base64 images, OpenAI image blocks, tool schemas/results, invalid `vision` fields, non-image requests, completed answers, and tool-call streams.
- [x] **Step 2: Run `uv run pytest tests/test_protocols/test_dsv.py -q` and verify the new tests fail because the adapter does not exist.**
- [x] **Step 3: Implement DSV normalization and strict validation.** Normalize `{type: "image", source: {type: "base64", media_type, data}}` into an OpenAI `image_url` data URL, reuse `openai.parse_chat_request` for message/tool validation, require at least one image, and parse `vision.mode` (`auto`, `ui`, `general`) plus boolean `include_analysis`.
- [x] **Step 4: Implement non-stream response encoding.** Return `object: "dsv.response"`, `status: "completed"` or `"requires_action"`, independent `vision`, `answer`, `reasoning`, `tool_calls`, and `usage` fields without exposing provider credentials.
- [x] **Step 5: Implement SSE encoding.** Emit `response.created`, `vision.started`, `vision.completed`, reasoning deltas, answer deltas, tool-call deltas/completion, `response.requires_action`, and `response.completed`; catch upstream exceptions as a DSV `error` event without emitting a false successful answer.
- [x] **Step 6: Run the focused protocol tests and make them pass.**

### Task 2: Add the `/v1/dsv` FastAPI route

**Files:**
- Modify: `deepsee_server/app.py`
- Test: `tests/test_server/test_dsv.py`

**Interfaces:**
- `POST /v1/dsv` consumes the parsed DSV request and returns JSON or `text/event-stream`.
- The route calls `transform_messages_with_vision`, then `chat_async`, and never calls a DSH tool.

- [x] **Step 1: Add failing route tests** for non-stream vision+answer, SSE event ordering, tool-call `requires_action`, role-tool follow-up preservation, invalid requests before configuration loading, body limits, configured public auth, and OpenAI-compatible backend enforcement.
- [x] **Step 2: Run `uv run pytest tests/test_server/test_dsv.py -q` and verify failure.**
- [x] **Step 3: Register `/v1/dsv` in `_required_scope` and implement body parsing with `_body_too_large`, `_read_body_limited`, and protocol-shaped 400 errors.**
- [x] **Step 4:** Load config after validation, require `cfg.vision.backend == "openai_compatible"`, set trace route/image/model fields, acquire the existing inference lease, and run the existing vision transformer plus `chat_async`.
- [x] **Step 5: Build vision metadata from config, elapsed time, cache hits, and request trace id; return the DSV JSON envelope or stream encoder and attach the lease to response lifetime.**
- [x] **Step 6: Run focused route tests and then the full server test suite.**

### Task 3: Document and validate the public contract

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-15-dsh-vision-integration-design.md`
- Test: `tests/test_server/test_dsv.py`

- [x] **Step 1: Add a README DSV example** showing base64 image input, SSE event names, and DSH tool-result continuation without including any provider secret.
- [x] **Step 2: Update the design status to implementation-ready and record the implemented server-side boundary; keep DSH plugin changes scoped to the external DSH repository.**
- [x] **Step 3: Add an end-to-end contract assertion that `vision.completed` is independent from answer content and that DSV does not execute a returned tool call.**
- [x] **Step 4: Run `uv run pytest -q` and `git diff --check`.**

### Task 4: Review and commit the vertical slice

- [x] **Step 1: Inspect the diff for compatibility regressions, secret leakage, false success events, and unrelated changes.**
- [x] **Step 2: Run `uv run pytest -q` one final time.**
- [x] **Step 3: Commit only the DSV implementation, tests, README, and plan files with `git add <explicit files> && git commit -m "feat(server): add DSV vision orchestration protocol"`.**
