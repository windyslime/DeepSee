import httpx
import pytest
import respx

from deepsee.backends.base import stream_request


class _BodyStream(httpx.SyncByteStream):
    """可记录读取时机的流式正文:每次迭代记录事件再 yield 分块。"""

    def __init__(self, events: list[str], chunks: list[bytes]):
        self._events = events
        self._chunks = chunks

    def __iter__(self):
        for i, chunk in enumerate(self._chunks):
            self._events.append(f"chunk-{i}")
            yield chunk

    def close(self) -> None:
        pass


def test_stream_request_returns_before_body_consumed():
    """响应头到达即返回,正文按迭代惰性读取(真流式语义)。"""
    events: list[str] = []

    def handler(request):
        events.append("headers")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=_BodyStream(
                events,
                [
                    b'data: {"choices": [{"delta": {"content": "a"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ],
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resp = stream_request(client, "POST", "https://example.com/api", retries=0)
    assert events == ["headers"]  # 正文尚未被读取

    it = resp.iter_lines()
    first = next(it)
    assert first == 'data: {"choices": [{"delta": {"content": "a"}}]}'
    assert events == ["headers", "chunk-0"]  # 首行到达,第二个分块未读
    # iter_lines 按 \n 切分,\n\n 会产出空行;过滤后只剩 SSE 数据行
    assert [line for line in it if line] == ["data: [DONE]"]
    assert events == ["headers", "chunk-0", "chunk-1"]
    client.close()


def test_stream_request_retries_5xx_before_body():
    with respx.mock:
        route = respx.post("https://example.com/api").mock(
            side_effect=[
                httpx.Response(500, content=b"boom"),
                httpx.Response(200, content=b"data: [DONE]\n\n"),
            ]
        )
        with httpx.Client() as client:
            resp = stream_request(client, "POST", "https://example.com/api", retries=2)
            lines = [line for line in resp.iter_lines() if line]
            assert lines == ["data: [DONE]"]
    assert len(route.calls) == 2


def test_stream_request_429_retries_then_succeeds():
    with respx.mock:
        route = respx.post("https://example.com/api").mock(
            side_effect=[
                httpx.Response(429, content=b"slow down"),
                httpx.Response(200, content=b"data: [DONE]\n\n"),
            ]
        )
        with httpx.Client() as client:
            resp = stream_request(client, "POST", "https://example.com/api", retries=2)
            list(resp.iter_lines())
    assert len(route.calls) == 2


def test_stream_request_5xx_exhausted_raises_http_status_error():
    with respx.mock:
        respx.post("https://example.com/api").mock(
            return_value=httpx.Response(500, content=b"boom")
        )
        with httpx.Client() as client:
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                stream_request(client, "POST", "https://example.com/api", retries=0)
    assert exc_info.value.response.status_code == 500


import asyncio

from deepsee.backends.base import retry_request_async, stream_request_async


class _AsyncBodyStream(httpx.AsyncByteStream):
    """记录读取时机的异步流式正文。"""

    def __init__(self, events: list[str], chunks: list[bytes]):
        self._events = events
        self._chunks = chunks

    async def __aiter__(self):
        for i, chunk in enumerate(self._chunks):
            self._events.append(f"chunk-{i}")
            yield chunk

    async def aclose(self) -> None:
        pass


def test_async_stream_request_lazy_body():
    events: list[str] = []

    async def handler(request):
        events.append("headers")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=_AsyncBodyStream(
                events,
                [
                    b'data: {"choices": [{"delta": {"content": "a"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ],
            ),
        )

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resp = await stream_request_async(
            client, "POST", "https://example.com/api", retries=0
        )
        assert events == ["headers"]  # 正文尚未被读取
        lines = []
        async for line in resp.aiter_lines():
            lines.append(line)
        await client.aclose()
        return lines

    lines = asyncio.run(run())
    assert lines[0] == 'data: {"choices": [{"delta": {"content": "a"}}]}'
    assert events == ["headers", "chunk-0", "chunk-1"]  # 逐块读取


def test_async_stream_request_retries_5xx_before_body():
    async def run():
        async with respx.mock:
            route = respx.post("https://example.com/api").mock(
                side_effect=[
                    httpx.Response(500, content=b"boom"),
                    httpx.Response(200, content=b"data: [DONE]\n\n"),
                ]
            )
            async with httpx.AsyncClient() as client:
                resp = await stream_request_async(
                    client, "POST", "https://example.com/api", retries=2
                )
                lines = [line async for line in resp.aiter_lines()]
            return route, lines

    route, lines = asyncio.run(run())
    assert [l for l in lines if l] == ["data: [DONE]"]
    assert len(route.calls) == 2


def test_async_retry_request_429_then_succeeds():
    async def run():
        async with respx.mock:
            route = respx.post("https://example.com/api").mock(
                side_effect=[
                    httpx.Response(429, content=b"slow down"),
                    httpx.Response(200, json={"ok": True}),
                ]
            )
            async with httpx.AsyncClient() as client:
                resp = await retry_request_async(
                    client, "POST", "https://example.com/api", retries=2
                )
            return route, resp.json()

    route, data = asyncio.run(run())
    assert data == {"ok": True}
    assert len(route.calls) == 2


def test_async_retry_request_5xx_exhausted_raises():
    async def run():
        async with respx.mock:
            respx.post("https://example.com/api").mock(
                return_value=httpx.Response(500, content=b"boom")
            )
            async with httpx.AsyncClient() as client:
                try:
                    await retry_request_async(
                        client, "POST", "https://example.com/api", retries=0
                    )
                except httpx.HTTPStatusError as exc:
                    return exc.response.status_code

    assert asyncio.run(run()) == 500


def test_sync_client_ignores_socks_proxy_env(monkeypatch, sample_image_bytes):
    """无 socksio 时,ALL_PROXY=socks5 环境不得让库崩溃(trust_env=False)。"""
    import importlib.util

    if importlib.util.find_spec("socksio") is not None:
        pytest.skip("socksio installed; ImportError cannot be reproduced")

    from deepsee.backends.openai_compat import OpenAICompatibleBackend

    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("HTTP_PROXY", "socks5://127.0.0.1:1080")
    backend = OpenAICompatibleBackend(
        api_key="k", model="m", base_url="https://vision.example.com/v1", retries=0
    )
    with respx.mock:
        respx.post("https://vision.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        )
        assert backend.describe(sample_image_bytes, "p") == "ok"
    backend.close()


def test_async_client_ignores_socks_proxy_env(monkeypatch, sample_image_bytes):
    """异步路径同样不受 socks 代理环境变量影响。"""
    import importlib.util

    if importlib.util.find_spec("socksio") is not None:
        pytest.skip("socksio installed; ImportError cannot be reproduced")

    from deepsee.backends.openai_compat import OpenAICompatibleBackend

    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("HTTP_PROXY", "socks5://127.0.0.1:1080")
    backend = OpenAICompatibleBackend(
        api_key="k", model="m", base_url="https://vision.example.com/v1", retries=0
    )

    async def run():
        async with respx.mock:
            respx.post("https://vision.example.com/v1/chat/completions").mock(
                return_value=httpx.Response(
                    200, json={"choices": [{"message": {"content": "ok"}}]}
                )
            )
            return await backend.describe_async(sample_image_bytes, "p")

    assert asyncio.run(run()) == "ok"
    backend.close()


def test_sync_path_never_allocates_async_client(sample_image_bytes):
    """同步 describe 不应创建 AsyncClient(懒创建,避免 sync 路径残留 async 资源)。"""
    from deepsee.backends.openai_compat import OpenAICompatibleBackend

    backend = OpenAICompatibleBackend(
        api_key="k", model="m", base_url="https://vision.example.com/v1", retries=0
    )
    with respx.mock:
        respx.post("https://vision.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        )
        assert backend.describe(sample_image_bytes, "p") == "ok"
    assert backend._async_client is None  # 懒创建:同步路径不分配
    backend.close()
