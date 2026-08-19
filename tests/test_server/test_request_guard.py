import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from deepsee_server.app import app, configure_request_guard
from deepsee_server.auth import ApiKeyStore, configure_api_key_store, disable_api_key_auth
from deepsee_server.request_guard import QueueTimeout, RateLimitExceeded, RequestGuard


@pytest.fixture(autouse=True)
def reset_gateway_security_state():
    disable_api_key_auth()
    configure_request_guard(None)
    yield
    configure_api_key_store(None)
    configure_request_guard(None)


def test_rate_limit_counts_each_identity_independently_and_reports_retry_after():
    async def scenario():
        guard = RequestGuard(
            max_concurrent=2,
            queue_timeout=0.01,
            rate_limit=2,
            rate_window=60,
        )
        first = await guard.acquire("digest-a")
        second = await guard.acquire("digest-a")
        await first.release()
        await second.release()

        with pytest.raises(RateLimitExceeded) as exceeded:
            await guard.acquire("digest-a")
        assert isinstance(exceeded.value.retry_after, int)
        assert exceeded.value.retry_after >= 1

        other_identity = await guard.acquire("digest-b")
        await other_identity.release()

    asyncio.run(scenario())


def test_concurrency_queue_timeout_and_released_lease_can_be_reused():
    async def scenario():
        guard = RequestGuard(
            max_concurrent=1,
            queue_timeout=0.01,
            rate_limit=10,
            rate_window=60,
        )
        first = await guard.acquire("client-a")
        with pytest.raises(QueueTimeout):
            await guard.acquire("client-b")

        await first.release()
        await first.release()  # release is idempotent for cancellation/error paths.
        reused = await guard.acquire("client-b")
        await reused.release()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("max_concurrent", "queue_timeout", "rate_limit", "rate_window"),
    [
        (0, 1, 1, 1),
        (1, 0, 1, 1),
        (1, 1, 0, 1),
        (1, 1, 1, 0),
        (-1, 1, 1, 1),
    ],
)
def test_invalid_guard_settings_are_rejected(
    max_concurrent, queue_timeout, rate_limit, rate_window
):
    with pytest.raises(ValueError):
        RequestGuard(
            max_concurrent=max_concurrent,
            queue_timeout=queue_timeout,
            rate_limit=rate_limit,
            rate_window=rate_window,
        )


def _asgi_scope(path: str, body: bytes) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


async def _run_asgi_request(path: str, body: bytes, sent: list[dict], first_chunk=None):
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            await asyncio.Future()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)
        if first_chunk is not None and message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    await app(_asgi_scope(path, body), receive, send)


def test_stream_completion_releases_the_http_concurrency_lease(monkeypatch):
    async def scenario():
        guard = RequestGuard(
            max_concurrent=1,
            queue_timeout=0.01,
            rate_limit=10,
            rate_window=60,
        )
        configure_request_guard(guard)
        monkeypatch.setattr(
            "deepsee_server.app._current_config",
            lambda: type(
                "Config",
                (), {"deepseek": type("DeepSeek", (), {"model": "test"})()},
            )(),
        )
        release_upstream = asyncio.Event()

        async def fake_chat(_messages, **_kwargs):
            async def chunks():
                yield {
                    "id": "test-id",
                    "choices": [{
                        "delta": {"content": "first"},
                        "finish_reason": None,
                    }],
                }
                await release_upstream.wait()
                yield {
                    "id": "test-id",
                    "choices": [{
                        "delta": {"content": "last"},
                        "finish_reason": "stop",
                    }],
                }

            return chunks()

        monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)
        first_chunk = asyncio.Event()
        first_messages: list[dict] = []
        body = json.dumps(
            {"stream": True, "messages": [{"role": "user", "content": "hold"}]}
        ).encode()
        first_request = asyncio.create_task(
            _run_asgi_request(
                "/v1/chat/completions", body, first_messages, first_chunk
            )
        )
        await asyncio.wait_for(first_chunk.wait(), timeout=1)

        second_messages: list[dict] = []
        await _run_asgi_request("/v1/chat/completions", body, second_messages)
        response_start = next(
            message for message in second_messages if message["type"] == "http.response.start"
        )
        assert response_start["status"] == 503
        assert dict(response_start["headers"])[b"retry-after"] == b"1"

        release_upstream.set()
        await asyncio.wait_for(first_request, timeout=1)

        reusable = await guard.acquire("127.0.0.1")
        await reusable.release()

    asyncio.run(scenario())


def test_stream_failure_and_cancellation_release_the_http_concurrency_lease(monkeypatch):
    async def scenario():
        guard = RequestGuard(
            max_concurrent=1,
            queue_timeout=0.01,
            rate_limit=20,
            rate_window=60,
        )
        configure_request_guard(guard)
        monkeypatch.setattr(
            "deepsee_server.app._current_config",
            lambda: type(
                "Config",
                (), {"deepseek": type("DeepSeek", (), {"model": "test"})()},
            )(),
        )
        cancel_gate = asyncio.Event()
        calls = 0

        async def fake_chat(_messages, **_kwargs):
            nonlocal calls
            calls += 1

            async def chunks():
                yield {
                    "id": "test-id",
                    "choices": [{
                        "delta": {"content": "first"},
                        "finish_reason": None,
                    }],
                }
                if calls == 1:
                    raise RuntimeError("upstream stream exploded")
                await cancel_gate.wait()
                yield {
                    "id": "test-id",
                    "choices": [{
                        "delta": {"content": "never"},
                        "finish_reason": "stop",
                    }],
                }

            return chunks()

        monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)
        body = json.dumps(
            {"stream": True, "messages": [{"role": "user", "content": "hold"}]}
        ).encode()

        # A stream generator exception must not retain the sole lease.
        with pytest.raises(Exception):
            await _run_asgi_request("/v1/chat/completions", body, [])
        after_error = await guard.acquire("127.0.0.1")
        await after_error.release()

        # Client cancellation after a first chunk must also release it.
        first_chunk = asyncio.Event()
        pending = asyncio.create_task(
            _run_asgi_request("/v1/chat/completions", body, [], first_chunk)
        )
        await asyncio.wait_for(first_chunk.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        after_cancel = await guard.acquire("127.0.0.1")
        await after_cancel.release()

    asyncio.run(scenario())


def test_rate_limit_http_error_uses_openai_error_shape(monkeypatch):
    guard = RequestGuard(
        max_concurrent=2,
        queue_timeout=0.01,
        rate_limit=1,
        rate_window=60,
    )
    configure_request_guard(guard)
    monkeypatch.setattr(
        "deepsee_server.app._current_config",
        lambda: type(
            "Config", (), {"deepseek": type("DeepSeek", (), {"model": "test"})()}
        )(),
    )

    async def fake_chat(_messages, **_kwargs):
        return {
            "id": "test-id",
            "choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
        }

    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)
    client = TestClient(app)

    first = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    second = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "second"}]},
    )

    assert first.status_code != 429
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limit_error"
    assert int(second.headers["retry-after"]) >= 1


def test_authenticated_protocol_error_releases_the_http_concurrency_lease(
    monkeypatch, tmp_path
):
    store = ApiKeyStore(tmp_path / "keys.json")
    public = store.create("public", "client")
    configure_api_key_store(store)
    guard = RequestGuard(
        max_concurrent=1,
        queue_timeout=0.01,
        rate_limit=10,
        rate_window=60,
    )
    configure_request_guard(guard)
    client = TestClient(app)

    malformed = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {public.key}"},
        content=b"not json",
    )

    assert malformed.status_code == 400

    async def verify_reusable():
        lease = await guard.acquire("still-available")
        await lease.release()

    asyncio.run(verify_reusable())


def test_rejected_key_does_not_consume_valid_identity_rate_budget(monkeypatch, tmp_path):
    store = ApiKeyStore(tmp_path / "keys.json")
    public = store.create("public", "client")
    configure_api_key_store(store)
    guard = RequestGuard(
        max_concurrent=2,
        queue_timeout=0.01,
        rate_limit=1,
        rate_window=60,
    )
    configure_request_guard(guard)
    monkeypatch.setattr(
        "deepsee_server.app._current_config",
        lambda: type(
            "Config", (), {"deepseek": type("DeepSeek", (), {"model": "test"})()}
        )(),
    )

    async def fake_chat(_messages, **_kwargs):
        return {
            "id": "test-id",
            "choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
        }

    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)
    client = TestClient(app)

    rejected = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer invalid"},
        content=b"not json",
    )
    accepted = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {public.key}"},
        json={"messages": [{"role": "user", "content": "allowed"}]},
    )

    assert rejected.status_code == 401
    assert accepted.status_code != 429


def test_malformed_json_does_not_consume_rate_budget(monkeypatch):
    """非法 JSON 是普通 4xx:即使 rate_limit=1,后续合法请求也不得 429。"""
    guard = RequestGuard(
        max_concurrent=2,
        queue_timeout=0.01,
        rate_limit=1,
        rate_window=60,
    )
    configure_request_guard(guard)
    monkeypatch.setattr(
        "deepsee_server.app._current_config",
        lambda: type(
            "Config", (), {"deepseek": type("DeepSeek", (), {"model": "test"})()}
        )(),
    )

    async def fake_chat(_messages, **_kwargs):
        return {
            "id": "test-id",
            "choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
        }

    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)
    client = TestClient(app)

    malformed = client.post(
        "/v1/chat/completions", content=b"{not json"
    )
    accepted = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "still allowed"}]},
    )

    assert malformed.status_code == 400
    assert accepted.status_code == 200


def test_protocol_validation_error_does_not_consume_rate_budget(monkeypatch):
    """协议校验失败(如未知参数)同样不得消耗速率预算。"""
    guard = RequestGuard(
        max_concurrent=2,
        queue_timeout=0.01,
        rate_limit=1,
        rate_window=60,
    )
    configure_request_guard(guard)
    monkeypatch.setattr(
        "deepsee_server.app._current_config",
        lambda: type(
            "Config", (), {"deepseek": type("DeepSeek", (), {"model": "test"})()}
        )(),
    )

    async def fake_chat(_messages, **_kwargs):
        return {
            "id": "test-id",
            "choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
        }

    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)
    client = TestClient(app)

    rejected = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "unknown_param": True,
        },
    )
    accepted = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "still allowed"}]},
    )

    assert rejected.status_code == 400
    assert accepted.status_code == 200


def test_malformed_json_returns_400_while_concurrency_is_exhausted(monkeypatch):
    """并发槽满时,非法 JSON 返回自身的 400,而不是被误映射为 503。"""
    guard = RequestGuard(
        max_concurrent=1,
        queue_timeout=0.01,
        rate_limit=10,
        rate_window=60,
    )
    configure_request_guard(guard)
    client = TestClient(app)

    held = asyncio.run(guard.acquire("holder"))
    try:
        malformed = client.post(
            "/v1/chat/completions", content=b"{not json"
        )
        assert malformed.status_code == 400
    finally:
        asyncio.run(held.release())
