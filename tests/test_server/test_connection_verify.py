import json
import asyncio

import httpx

from deepsee.config.loader import Config, DeepSeekConfig, VisionConfig
from deepsee.errors import ComposeError
from deepsee.pipeline.image import load_image
import deepsee_server.connection_verify as connection_verify_module
from deepsee_server.connection_verify import _TEST_PNG, verify_upstream_connections


def test_embedded_verification_image_is_decodable():
    image = load_image(_TEST_PNG)
    try:
        assert image.size == (4, 4)
    finally:
        image.close()


def test_vision_probe_limits_provider_output(monkeypatch):
    config = Config(
        deepseek=DeepSeekConfig(api_key="deepseek-secret"),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="vision-secret",
            base_url="https://vision.example/v1",
            model="vision-model",
        ),
        retries=0,
    )
    seen = {}

    async def fake_describe(image, prompt, *, config, max_tokens):
        seen.update(
            image=image,
            prompt=prompt,
            config=config,
            max_tokens=max_tokens,
        )

    monkeypatch.setattr(
        connection_verify_module,
        "describe_image_async",
        fake_describe,
    )

    asyncio.run(connection_verify_module._vision_probe(config))

    assert seen["image"] == _TEST_PNG
    assert seen["config"] is config
    assert seen["max_tokens"] == 1


def test_verification_runs_both_providers_and_sanitizes_failure_details():
    calls = []
    config = Config(
        deepseek=DeepSeekConfig(api_key="deepseek-secret"),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="vision-secret",
            base_url="https://vision.example/v1",
            model="vision-model",
        ),
        retries=0,
    )

    async def failing_deepseek(_config):
        calls.append("deepseek")
        raise ComposeError("upstream echoed deepseek-secret", status_code=401)

    async def successful_vision(_config):
        calls.append("vision")

    result = asyncio.run(
        verify_upstream_connections(
            config,
            deepseek_probe=failing_deepseek,
            vision_probe=successful_vision,
        )
    )

    assert calls == ["deepseek", "vision"]
    assert result["deepseek"]["ok"] is False
    assert result["deepseek"]["error"] == {
        "code": "AUTH",
        "message": "认证失败",
    }
    assert result["vision"]["ok"] is True
    assert "deepseek-secret" not in json.dumps(result)
    assert "vision-secret" not in json.dumps(result)


def test_verification_recognizes_transport_errors_wrapped_by_provider():
    config = Config(
        deepseek=DeepSeekConfig(api_key="deepseek-secret"),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="vision-secret",
            base_url="https://vision.example/v1",
            model="vision-model",
        ),
        retries=0,
    )

    async def wrapped_transport_error(_config):
        try:
            raise httpx.ConnectError("must not reach the response")
        except httpx.ConnectError as exc:
            raise ComposeError("provider wrapper") from exc

    async def successful_vision(_config):
        return None

    result = asyncio.run(
        verify_upstream_connections(
            config,
            deepseek_probe=wrapped_transport_error,
            vision_probe=successful_vision,
        )
    )

    assert result["deepseek"]["error"] == {
        "code": "TRANSPORT",
        "message": "网络连接失败",
    }
    assert "must not reach" not in json.dumps(result)
