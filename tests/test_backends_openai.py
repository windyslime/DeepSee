import json
import httpx
import pytest
import respx

from deepsee.backends import create_backend
from deepsee.backends.openai_compat import OpenAICompatibleBackend
from deepsee.config.loader import VisionConfig
from deepsee.errors import ConfigError, VisionBackendError


def make_backend(retries: int = 2) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(
        api_key="sk-test",
        model="qwen-vl-max",
        base_url="https://vision.example.com/v1",
        retries=retries,
    )


def test_request_shape(sample_image_bytes):
    backend = make_backend()
    with respx.mock:
        route = respx.post("https://vision.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "一只红色的猫"}}]},
            )
        )
        result = backend.describe(sample_image_bytes, "图里有什么?")
    assert result == "一只红色的猫"
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer sk-test"
    payload = json.loads(req.content)
    assert payload["model"] == "qwen-vl-max"
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[1] == {"type": "text", "text": "图里有什么?"}


def test_backend_accepts_pil_image(sample_image_bytes):
    import io
    from PIL import Image

    backend = make_backend()
    pil_img = Image.open(io.BytesIO(sample_image_bytes))
    with respx.mock:
        respx.post("https://vision.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        )
        assert backend.describe(pil_img, "p") == "ok"


def test_429_retries_then_succeeds(sample_image_bytes):
    backend = make_backend(retries=2)
    with respx.mock:
        route = respx.post("https://vision.example.com/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(429, json={"error": "rate limited"}),
                httpx.Response(
                    200, json={"choices": [{"message": {"content": "ok"}}]}
                ),
            ]
        )
        assert backend.describe(sample_image_bytes, "p") == "ok"
    assert len(route.calls) == 2


def test_5xx_raises_vision_error(sample_image_bytes):
    backend = make_backend(retries=0)
    with respx.mock:
        respx.post("https://vision.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        with pytest.raises(VisionBackendError) as exc_info:
            backend.describe(sample_image_bytes, "p")
    assert exc_info.value.status_code == 500
    assert exc_info.value.model == "qwen-vl-max"
    assert exc_info.value.backend == "openai_compatible"


def test_4xx_does_not_retry(sample_image_bytes):
    backend = make_backend(retries=2)
    with respx.mock:
        route = respx.post("https://vision.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(400, json={"error": "bad request"})
        )
        with pytest.raises(VisionBackendError):
            backend.describe(sample_image_bytes, "p")
    assert len(route.calls) == 1


def test_missing_content_field_raises(sample_image_bytes):
    backend = make_backend()
    with respx.mock:
        respx.post("https://vision.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {}}]})
        )
        with pytest.raises(VisionBackendError):
            backend.describe(sample_image_bytes, "p")


def test_factory_selects_backend():
    backend = create_backend(
        VisionConfig(
            backend="openai_compatible",
            api_key="k",
            model="qwen-vl-max",
            base_url="https://x.example.com/v1",
        ),
        retries=1,
    )
    assert isinstance(backend, OpenAICompatibleBackend)


def test_factory_unknown_backend_raises():
    with pytest.raises(ConfigError):
        create_backend(
            VisionConfig(backend="nope", api_key="k", model="m"), retries=1
        )