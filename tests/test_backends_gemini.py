import json
import httpx
import pytest
import respx

from deepsee.backends import create_backend
from deepsee.backends.gemini import GeminiBackend
from deepsee.config.loader import VisionConfig
from deepsee.errors import VisionBackendError


def make_backend(retries: int = 2) -> GeminiBackend:
    return GeminiBackend(
        api_key="gem-test",
        model="gemini-2.0-flash",
        base_url="https://generativelanguage.googleapis.com",
        retries=retries,
    )


def test_request_shape(sample_image_bytes):
    backend = make_backend()
    with respx.mock:
        route = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "这是一张截图"}]}}
                    ]
                },
            )
        )
        result = backend.describe(sample_image_bytes, "这是什么?")
    assert result == "这是一张截图"
    req = route.calls[0].request
    assert req.headers["x-goog-api-key"] == "gem-test"
    payload = json.loads(req.content)
    parts = payload["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
    assert parts[0]["inline_data"]["data"]
    assert parts[1] == {"text": "这是什么?"}


def test_error_mapping(sample_image_bytes):
    backend = make_backend(retries=0)
    with respx.mock:
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        ).mock(
            return_value=httpx.Response(
                403, json={"error": {"message": "quota exceeded"}}
            )
        )
        with pytest.raises(VisionBackendError) as exc_info:
            backend.describe(sample_image_bytes, "p")
    assert exc_info.value.status_code == 403
    assert exc_info.value.backend == "gemini"


def test_factory_selects_gemini():
    backend = create_backend(
        VisionConfig(
            backend="gemini",
            api_key="k",
            model="gemini-2.0-flash",
            base_url="https://generativelanguage.googleapis.com",
        ),
        retries=1,
    )
    assert isinstance(backend, GeminiBackend)

def test_connect_error_wrapped(sample_image_bytes):
    backend = make_backend(retries=0)
    with respx.mock:
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        ).mock(side_effect=httpx.ConnectError("connection refused"))
        with pytest.raises(VisionBackendError) as exc_info:
            backend.describe(sample_image_bytes, "p")
    assert "网络错误" in str(exc_info.value)
    assert exc_info.value.backend == "gemini"


def test_empty_candidates_wrapped(sample_image_bytes):
    backend = make_backend()
    with respx.mock:
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        ).mock(return_value=httpx.Response(200, json={"candidates": []}))
        with pytest.raises(VisionBackendError) as exc_info:
            backend.describe(sample_image_bytes, "p")
    assert "响应解析失败" in str(exc_info.value)
