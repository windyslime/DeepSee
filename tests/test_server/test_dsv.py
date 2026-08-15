import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from deepsee.config.loader import Config, DeepSeekConfig, VisionConfig
from deepsee.composer.vision_context import VisionContextError, VisionTransformResult
from deepsee.errors import ComposeError
from deepsee_server.app import app, configure_request_guard
from deepsee_server.auth import configure_api_key_store, disable_api_key_auth


client = TestClient(app)


@pytest.fixture(autouse=True)
def explicit_no_auth_mode():
    disable_api_key_auth()
    configure_request_guard(None)
    yield
    configure_api_key_store(None)
    configure_request_guard(None)


@pytest.fixture
def cfg():
    return Config(
        deepseek=DeepSeekConfig(api_key="deepseek-key", model="deepseek-chat"),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="vision-key",
            base_url="https://vision.example.com/v1",
            model="vision-model",
        ),
        retries=0,
    )


@pytest.fixture
def use_cfg(monkeypatch, cfg):
    monkeypatch.setattr("deepsee_server.app._current_config", lambda: cfg)
    return cfg


def _image_block() -> dict:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(1, 2, 3)).save(buf, format="PNG")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(buf.getvalue()).decode("ascii"),
        },
    }


def _request(**overrides) -> dict:
    body = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "图里有什么?"}, _image_block()]}],
        "vision": {"mode": "auto", "include_analysis": True},
    }
    body.update(overrides)
    return body


def test_dsv_non_stream_returns_independent_vision_and_answer(use_cfg, monkeypatch):
    seen = {}

    async def fake_transform(messages, **kwargs):
        seen["messages"] = messages
        seen["mode"] = kwargs["mode"]
        return VisionTransformResult(
            messages=messages,
            analyses=["图里有一只猫"],
            cache_hits=1,
        )

    async def fake_chat(messages, **kwargs):
        seen["chat_messages"] = messages
        seen["params"] = kwargs["params"]
        return {
            "id": "deepseek-response",
            "choices": [{"message": {"role": "assistant", "content": "是一只猫"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 5},
        }

    monkeypatch.setattr("deepsee_server.app.transform_messages_with_vision", fake_transform)
    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)

    response = client.post("/v1/dsv", json=_request())

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "dsv.response"
    assert body["status"] == "completed"
    assert body["vision"]["analysis"] == "图里有一只猫"
    assert body["vision"]["cache_hit"] is True
    assert body["answer"] == {"text": "是一只猫"}
    assert body["usage"] == {"total_tokens": 5}
    assert seen["mode"] == "auto"
    assert seen["chat_messages"] == seen["messages"]


def test_dsv_forwards_requested_deepseek_model(use_cfg, monkeypatch):
    seen = {}

    async def fake_transform(messages, **kwargs):
        return VisionTransformResult(messages=messages, analyses=["分析"], cache_hits=0)

    async def fake_chat(messages, **kwargs):
        seen["model"] = kwargs["model"]
        return {
            "id": "deepseek-response",
            "choices": [{"message": {"content": "回答"}, "finish_reason": "stop"}],
        }

    monkeypatch.setattr("deepsee_server.app.transform_messages_with_vision", fake_transform)
    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)

    response = client.post("/v1/dsv", json={**_request(), "model": "deepseek-reasoner"})

    assert response.status_code == 200
    assert seen["model"] == "deepseek-reasoner"


def test_dsv_stream_emits_vision_before_answer(use_cfg, monkeypatch):
    async def fake_transform(messages, **kwargs):
        return VisionTransformResult(messages=messages, analyses=["视觉分析"], cache_hits=0)

    async def fake_chat(messages, **kwargs):
        async def source():
            yield {"id": "stream-id", "choices": [{"delta": {"content": "回答"}, "finish_reason": None}]}
            yield {"id": "stream-id", "choices": [{"delta": {}, "finish_reason": "stop"}]}

        return source()

    monkeypatch.setattr("deepsee_server.app.transform_messages_with_vision", fake_transform)
    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)

    response = client.post("/v1/dsv", json={**_request(), "stream": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    types = [event["type"] for event in events]
    assert types[:3] == ["response.created", "vision.started", "vision.completed"]
    assert "answer.delta" in types
    assert types[-2:] == ["answer.completed", "response.completed"]
    assert events[2]["vision"]["analysis"] == "视觉分析"


def test_dsv_stream_visual_failure_is_an_sse_error(use_cfg, monkeypatch):
    async def failing_transform(messages, **kwargs):
        raise VisionContextError("视觉服务失败")

    monkeypatch.setattr("deepsee_server.app.transform_messages_with_vision", failing_transform)

    response = client.post("/v1/dsv", json={**_request(), "stream": True})
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    assert [event["type"] for event in events] == [
        "response.created",
        "vision.started",
        "error",
        "response.completed",
    ]
    assert events[2]["stage"] == "vision"
    assert events[-1]["status"] == "failed"


def test_dsv_non_stream_reasoning_failure_preserves_vision(use_cfg, monkeypatch):
    async def fake_transform(messages, **kwargs):
        return VisionTransformResult(messages=messages, analyses=["分析"], cache_hits=1)

    async def failing_chat(messages, **kwargs):
        raise ComposeError("DeepSeek 推理失败")

    monkeypatch.setattr("deepsee_server.app.transform_messages_with_vision", fake_transform)
    monkeypatch.setattr("deepsee_server.app.chat_async", failing_chat)

    response = client.post("/v1/dsv", json=_request())

    assert response.status_code == 502
    body = response.json()
    assert body["object"] == "dsv.response"
    assert body["status"] == "failed"
    assert body["vision"]["analysis"] == "分析"
    assert body["error"]["stage"] == "reasoning"


def test_dsv_non_stream_malformed_reasoning_response_preserves_vision(use_cfg, monkeypatch):
    async def fake_transform(messages, **kwargs):
        return VisionTransformResult(messages=messages, analyses=["分析"], cache_hits=0)

    async def malformed_chat(messages, **kwargs):
        return {"choices": []}

    monkeypatch.setattr("deepsee_server.app.transform_messages_with_vision", fake_transform)
    monkeypatch.setattr("deepsee_server.app.chat_async", malformed_chat)

    response = client.post("/v1/dsv", json=_request())

    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "failed"
    assert body["vision"]["analysis"] == "分析"
    assert body["error"]["stage"] == "reasoning"


def test_dsv_stream_reasoning_failure_preserves_vision(use_cfg, monkeypatch):
    async def fake_transform(messages, **kwargs):
        return VisionTransformResult(messages=messages, analyses=["分析"], cache_hits=0)

    async def failing_chat(messages, **kwargs):
        raise ComposeError("DeepSeek 推理失败")

    monkeypatch.setattr("deepsee_server.app.transform_messages_with_vision", fake_transform)
    monkeypatch.setattr("deepsee_server.app.chat_async", failing_chat)

    response = client.post("/v1/dsv", json={**_request(), "stream": True})

    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    assert [event["type"] for event in events] == [
        "response.created",
        "vision.started",
        "vision.completed",
        "error",
        "response.completed",
    ]
    assert events[2]["vision"]["analysis"] == "分析"
    assert events[3]["stage"] == "reasoning"


def test_dsv_returns_tool_calls_without_executing_them(use_cfg, monkeypatch):
    calls = []

    async def fake_transform(messages, **kwargs):
        return VisionTransformResult(messages=messages, analyses=["分析"], cache_hits=0)

    async def fake_chat(messages, **kwargs):
        calls.append(messages)
        return {
            "id": "tool-response",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
                },
                "finish_reason": "tool_calls",
            }],
        }

    monkeypatch.setattr("deepsee_server.app.transform_messages_with_vision", fake_transform)
    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)

    response = client.post(
        "/v1/dsv",
        json={**_request(), "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "requires_action"
    assert body["tool_calls"][0]["function"]["name"] == "lookup"
    assert calls


def test_dsv_preserves_tool_result_history(use_cfg, monkeypatch):
    seen = {}

    async def fake_transform(messages, **kwargs):
        seen["messages"] = messages
        return VisionTransformResult(messages=messages, analyses=["分析"], cache_hits=1)

    async def fake_chat(messages, **kwargs):
        return {"id": "answer", "choices": [{"message": {"content": "继续回答"}, "finish_reason": "stop"}]}

    monkeypatch.setattr("deepsee_server.app.transform_messages_with_vision", fake_transform)
    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)

    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "工具结果"},
        {"role": "user", "content": [_image_block()]},
    ]
    response = client.post("/v1/dsv", json={**_request(), "messages": messages})

    assert response.status_code == 200
    assert seen["messages"][:2] == messages[:2]
    assert seen["messages"][2]["content"][0]["type"] == "image_url"


def test_dsv_rejects_invalid_request_before_loading_config(monkeypatch):
    def config_must_not_load():
        raise AssertionError("configuration should not load")

    monkeypatch.setattr("deepsee_server.app._current_config", config_must_not_load)
    response = client.post(
        "/v1/dsv",
        json={"messages": [{"role": "user", "content": "no image"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_dsv_requires_openai_compatible_vision_backend(use_cfg, monkeypatch):
    use_cfg.vision.backend = "anthropic"
    response = client.post("/v1/dsv", json=_request())
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "configuration_error"


def test_dsv_body_limit(use_cfg, monkeypatch):
    monkeypatch.setattr("deepsee_server.app._MAX_REQUEST_BODY", 64)
    response = client.post("/v1/dsv", json=_request())
    assert response.status_code == 413
