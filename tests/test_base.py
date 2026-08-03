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
