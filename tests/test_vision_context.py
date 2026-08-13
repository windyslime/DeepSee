import asyncio
import copy

import pytest

from deepsee.composer.vision_context import (
    MAX_IMAGES_PER_REQUEST,
    VisionContextError,
    clear_vision_context_cache,
    transform_messages_with_vision,
)
from deepsee.config.loader import Config, DeepSeekConfig, VisionConfig


@pytest.fixture
def config():
    return Config(
        deepseek=DeepSeekConfig(api_key="ds"),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="vision",
            base_url="https://vision.example.com/v1",
            model="vision-model",
        ),
        retries=0,
    )


def test_transform_replaces_only_images_and_preserves_history(config, monkeypatch):
    messages = [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                },
            ],
        },
    ]
    original = copy.deepcopy(messages)

    async def fake_analyze(image, question, mode, cfg):
        assert image == b"image"
        assert question == "first\nsecond"
        assert mode == "ui"
        assert cfg is config
        return {"kind": "description", "text": "save button"}

    monkeypatch.setattr(
        "deepsee.composer.vision_context._analyze_image_async", fake_analyze
    )
    result = asyncio.run(
        transform_messages_with_vision(messages, config=config, mode="ui")
    )

    assert messages == original
    assert result.messages[:4] == original[:4]
    assert result.messages[-1]["content"][:2] == original[-1]["content"][:2]
    replacement = result.messages[-1]["content"][2]["text"]
    assert 'trusted="false"' in replacement
    assert "DEEPSEE_VISUAL_CONTEXT" in replacement
    assert "save button" in replacement
    assert result.analyses == ["save button"]


def test_transform_rejects_oversized_single_context(config, monkeypatch):
    async def fake_analyze(*args, **kwargs):
        return {"kind": "description", "text": "123456"}

    monkeypatch.setattr(
        "deepsee.composer.vision_context._analyze_image_async", fake_analyze
    )
    monkeypatch.setattr(
        "deepsee.composer.vision_context.MAX_CONTEXT_CHARS_PER_IMAGE", 5
    )
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                }
            ],
        }
    ]

    with pytest.raises(VisionContextError, match="第 1 张图片"):
        asyncio.run(transform_messages_with_vision(messages, config=config))


def test_transform_reuses_analysis_cache(config, monkeypatch):
    clear_vision_context_cache()
    calls = []
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is visible?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                },
            ],
        }
    ]

    async def fake_analyze(image, question, mode, cfg):
        calls.append((image, question, mode, cfg.vision.model))
        return {"kind": "description", "text": "cached analysis"}

    monkeypatch.setattr(
        "deepsee.composer.vision_context._analyze_image_async", fake_analyze
    )
    first = asyncio.run(transform_messages_with_vision(messages, config=config))
    second = asyncio.run(transform_messages_with_vision(messages, config=config))
    clear_vision_context_cache()
    third = asyncio.run(transform_messages_with_vision(messages, config=config))

    assert len(calls) == 2
    assert first.cache_hits == 0
    assert second.cache_hits == 1
    assert third.cache_hits == 0
    assert second.analyses == ["cached analysis"]


def test_rejected_oversized_analysis_is_not_cached(config, monkeypatch):
    clear_vision_context_cache()
    calls = []
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                }
            ],
        }
    ]

    async def fake_analyze(*args, **kwargs):
        calls.append(True)
        return {"kind": "description", "text": "123456"}

    monkeypatch.setattr(
        "deepsee.composer.vision_context._analyze_image_async", fake_analyze
    )
    monkeypatch.setattr(
        "deepsee.composer.vision_context.MAX_CONTEXT_CHARS_PER_IMAGE", 5
    )
    for _ in range(2):
        with pytest.raises(VisionContextError, match="第 1 张图片"):
            asyncio.run(transform_messages_with_vision(messages, config=config))

    assert len(calls) == 2


def test_transform_rejects_too_many_images_before_analysis(config, monkeypatch):
    called = False

    async def fake_analyze(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "deepsee.composer.vision_context._analyze_image_async", fake_analyze
    )
    blocks = [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
        }
        for _ in range(MAX_IMAGES_PER_REQUEST + 1)
    ]

    with pytest.raises(VisionContextError, match="最多"):
        asyncio.run(
            transform_messages_with_vision(
                [{"role": "user", "content": blocks}], config=config
            )
        )

    assert called is False


def test_transform_rejects_total_context_limit(config, monkeypatch):
    clear_vision_context_cache()
    analyses = iter(["1234", "5678"])

    async def fake_analyze(*args, **kwargs):
        return {"kind": "description", "text": next(analyses)}

    monkeypatch.setattr(
        "deepsee.composer.vision_context._analyze_image_async", fake_analyze
    )
    monkeypatch.setattr(
        "deepsee.composer.vision_context.MAX_CONTEXT_CHARS_TOTAL", 7
    )
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aW1hZ2Ux"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aW1hZ2Uy"},
                },
            ],
        }
    ]

    with pytest.raises(VisionContextError, match="总长度"):
        asyncio.run(transform_messages_with_vision(messages, config=config))
