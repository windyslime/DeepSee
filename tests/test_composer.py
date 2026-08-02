import json
import httpx
import pytest
import respx

from deepsee.backends.base import VisionBackend
from deepsee.composer.deepseek import ask_with_image, describe_image
from deepsee.config.loader import Config, DeepSeekConfig, VisionConfig
from deepsee.errors import ComposeError, VisionBackendError

FAKE_DESCRIPTION = "图片里有一只白色的猫在窗台上。"


class FakeBackend(VisionBackend):
    """Records calls; returns a fixed description."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def describe(self, image, prompt, **opts):
        self.calls.append((image, prompt))
        return FAKE_DESCRIPTION


@pytest.fixture
def config():
    return Config(
        deepseek=DeepSeekConfig(
            api_key="sk-ds",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        ),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="sk-v",
            model="qwen-vl-max",
            base_url="https://vision.example.com/v1",
        ),
        retries=0,
    )


def test_describe_image_uses_backend(config, sample_image_bytes, monkeypatch):
    fake = FakeBackend("k", "m", "https://x")
    monkeypatch.setattr("deepsee.composer.deepseek.create_backend", lambda cfg, retries: fake)
    result = describe_image(sample_image_bytes, "有什么?", config=config)
    assert result == FAKE_DESCRIPTION
    assert fake.calls[0][1] == "有什么?"


def test_ask_with_image_composes(config, sample_image_bytes, monkeypatch):
    fake = FakeBackend("k", "m", "https://x")
    monkeypatch.setattr("deepsee.composer.deepseek.create_backend", lambda cfg, retries: fake)
    with respx.mock:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "是一只白猫。"}}]},
            )
        )
        answer = ask_with_image(sample_image_bytes, "图里是什么动物?", config=config)
    assert answer == "是一只白猫。"
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer sk-ds"
    payload = json.loads(req.content)
    assert payload["model"] == "deepseek-chat"
    assert payload["stream"] is False
    messages = payload["messages"]
    assert messages[0]["role"] == "system"
    assert FAKE_DESCRIPTION in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "图里是什么动物?"}
    # The vision prompt passed to the backend includes the user question
    assert "图里是什么动物?" in fake.calls[0][1]


def test_ask_with_image_streams(config, sample_image_bytes, monkeypatch):
    fake = FakeBackend("k", "m", "https://x")
    monkeypatch.setattr("deepsee.composer.deepseek.create_backend", lambda cfg, retries: fake)
    sse_body = (
        'data: {"choices": [{"delta": {"content": "是"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "白猫"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, content=sse_body.encode())
        )
        chunks = list(ask_with_image(sample_image_bytes, "是什么?", config=config, stream=True))
    assert chunks == ["是", "白猫"]


def test_deepseek_500_maps_to_compose_error(config, sample_image_bytes, monkeypatch):
    fake = FakeBackend("k", "m", "https://x")
    monkeypatch.setattr("deepsee.composer.deepseek.create_backend", lambda cfg, retries: fake)
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        with pytest.raises(ComposeError) as exc_info:
            ask_with_image(sample_image_bytes, "q", config=config)
    assert exc_info.value.status_code == 500
    assert exc_info.value.model == "deepseek-chat"


def test_vision_failure_propagates(config, sample_image_bytes, monkeypatch):
    def failing_backend(cfg, retries):
        class Boom(FakeBackend):
            def describe(self, image, prompt, **opts):
                raise VisionBackendError("vision down", backend="openai_compatible")

        return Boom("k", "m", "https://x")

    monkeypatch.setattr("deepsee.composer.deepseek.create_backend", failing_backend)
    with pytest.raises(VisionBackendError):
        ask_with_image(sample_image_bytes, "q", config=config)

def test_public_api_exports():
    import deepsee

    assert callable(deepsee.ask_with_image)
    assert callable(deepsee.describe_image)
    assert callable(deepsee.create_backend)
    assert callable(deepsee.load_config)
    assert deepsee.__version__ == "0.1.0"
