import json
import httpx
import pytest
import respx

from deepsee.backends import create_backend
from deepsee.backends.anthropic import AnthropicBackend
from deepsee.config.loader import VisionConfig
from deepsee.errors import VisionBackendError


def make_backend(retries: int = 2) -> AnthropicBackend:
    return AnthropicBackend(
        api_key="sk-ant-test",
        model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com",
        retries=retries,
    )


def test_request_shape(sample_image_bytes):
    backend = make_backend()
    with respx.mock:
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "画面是蓝天白云"}]},
            )
        )
        result = backend.describe(sample_image_bytes, "图里有什么?")
    assert result == "画面是蓝天白云"
    req = route.calls[0].request
    assert req.headers["x-api-key"] == "sk-ant-test"
    assert req.headers["anthropic-version"] == "2023-06-01"
    payload = json.loads(req.content)
    assert payload["model"] == "claude-sonnet-4-5"
    assert payload["max_tokens"] == 1024
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[1] == {"type": "text", "text": "图里有什么?"}


def test_error_mapping(sample_image_bytes):
    backend = make_backend(retries=0)
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
        )
        with pytest.raises(VisionBackendError) as exc_info:
            backend.describe(sample_image_bytes, "p")
    assert exc_info.value.status_code == 401
    assert exc_info.value.backend == "anthropic"


def test_factory_selects_anthropic():
    backend = create_backend(
        VisionConfig(
            backend="anthropic",
            api_key="k",
            model="claude-sonnet-4-5",
            base_url="https://api.anthropic.com",
        ),
        retries=1,
    )
    assert isinstance(backend, AnthropicBackend)

def test_connect_error_wrapped(sample_image_bytes):
    backend = make_backend(retries=0)
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(VisionBackendError) as exc_info:
            backend.describe(sample_image_bytes, "p")
    assert "网络错误" in str(exc_info.value)
    assert exc_info.value.backend == "anthropic"


def test_empty_content_wrapped(sample_image_bytes):
    backend = make_backend()
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json={"content": []})
        )
        with pytest.raises(VisionBackendError) as exc_info:
            backend.describe(sample_image_bytes, "p")
    assert "响应解析失败" in str(exc_info.value)


import asyncio


def test_describe_async_request_shape(sample_image_bytes):
    backend = make_backend()

    async def _run():
        async with respx.mock:
            route = respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(
                    200, json={"content": [{"type": "text", "text": "画面是蓝天白云"}]}
                )
            )
            result = await backend.describe_async(sample_image_bytes, "图里有什么?")
            return route, result

    route, result = asyncio.run(_run())
    assert result == "画面是蓝天白云"
    payload = json.loads(route.calls[0].request.content)
    assert payload["messages"][0]["content"][0]["type"] == "image"
    backend.close()


def test_describe_async_connect_error_wrapped(sample_image_bytes):
    backend = make_backend(retries=0)

    async def _run():
        async with respx.mock:
            respx.post("https://api.anthropic.com/v1/messages").mock(
                side_effect=httpx.ConnectError("connection refused")
            )
            await backend.describe_async(sample_image_bytes, "p")

    with pytest.raises(VisionBackendError) as exc_info:
        asyncio.run(_run())
    assert "网络错误" in str(exc_info.value)
    backend.close()
