import copy
import json

import pytest
from fastapi.testclient import TestClient

from deepsee.composer.vision_context import VisionTransformResult
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


def test_chat_preserves_multi_turn_tools_params_and_upstream_response(monkeypatch):
    messages = [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "read"},
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
    ]
    upstream = {
        "id": "upstream-id",
        "object": "chat.completion",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "need another read",
                    "tool_calls": [],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"total_tokens": 9},
    }
    seen = {}

    async def fake_chat(received, **kwargs):
        seen["messages"] = received
        seen.update(kwargs)
        return upstream

    monkeypatch.setattr("deepsee_server.app._current_config", lambda: _config())
    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": messages,
            "tools": [],
            "tool_choice": "auto",
            "max_completion_tokens": 123,
            "store": False,
        },
    )

    assert response.status_code == 200
    assert response.json() == upstream
    assert seen["messages"] == messages
    assert seen["params"]["max_completion_tokens"] == 123
    assert "store" not in seen["params"]


def test_chat_rejects_unknown_parameter_before_config_or_upstream(monkeypatch):
    def config_must_not_load():
        raise AssertionError("configuration should not be loaded")

    async def chat_must_not_run(*args, **kwargs):
        raise AssertionError("upstream should not be called")

    monkeypatch.setattr("deepsee_server.app._current_config", config_must_not_load)
    monkeypatch.setattr("deepsee_server.app.chat_async", chat_must_not_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "unexpected": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "unexpected" in response.json()["error"]["message"]


def test_chat_image_preserves_history_and_gates_vision_extension(monkeypatch):
    messages = [
        {"role": "system", "content": "Use tools."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "type": "function"}],
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
    transformed = copy.deepcopy(messages)
    transformed[-1]["content"][2] = {
        "type": "text",
        "text": "<DEEPSEE_VISUAL_CONTEXT trusted=\"false\">button",
    }
    seen = {}

    async def fake_transform(received, **kwargs):
        seen["transform_messages"] = received
        seen["vision_mode"] = kwargs["mode"]
        return VisionTransformResult(
            messages=transformed,
            analyses=["button"],
            cache_hits=1,
        )

    async def fake_chat(received, **kwargs):
        seen["messages"] = received
        seen["params"] = kwargs["params"]
        return {
            "id": "upstream-id",
            "object": "chat.completion",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
        }

    monkeypatch.setattr("deepsee_server.app._current_config", lambda: _config())
    monkeypatch.setattr(
        "deepsee_server.app.transform_messages_with_vision", fake_transform
    )
    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)

    body = {"messages": messages, "tools": []}
    standard = client.post("/v1/chat/completions", json=body)
    extended = client.post(
        "/v1/chat/completions",
        json=body,
        headers={
            "X-DeepSee-Include-Vision": "1",
            "X-DeepSee-Vision-Mode": "ui",
        },
    )

    assert standard.status_code == 200
    assert "vision_analysis" not in standard.json()["choices"][0]["message"]
    assert extended.status_code == 200
    assert (
        extended.json()["choices"][0]["message"]["vision_analysis"] == "button"
    )
    assert seen["transform_messages"] == messages
    assert seen["messages"] == transformed
    assert seen["params"]["tools"] == []
    assert seen["vision_mode"] == "ui"
    assert extended.headers["X-DeepSee-Vision-Cache-Hits"] == "1"


def test_chat_stream_preserves_upstream_chunks_and_stable_id(monkeypatch):
    chunks = [
        {
            "id": "stable-id",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-chat",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "read", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "stable-id",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-chat",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
    ]

    async def fake_chat(received, **kwargs):
        async def source():
            for chunk in chunks:
                yield chunk

        return source()

    monkeypatch.setattr("deepsee_server.app._current_config", lambda: _config())
    monkeypatch.setattr("deepsee_server.app.chat_async", fake_chat)
    response = client.post(
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "go"}]},
    )

    lines = [line for line in response.text.splitlines() if line.startswith("data: ")]
    payloads = [json.loads(line[6:]) for line in lines[:-1]]
    assert [payload["id"] for payload in payloads] == ["stable-id", "stable-id"]
    assert payloads[0]["choices"][0]["delta"]["tool_calls"][0]["id"] == "call-1"
    assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert lines[-1] == "data: [DONE]"


def _config():
    from deepsee.config.loader import Config, DeepSeekConfig, VisionConfig

    return Config(
        deepseek=DeepSeekConfig(api_key="ds", model="deepseek-chat"),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="vision",
            base_url="https://vision.example.com/v1",
            model="vision-model",
        ),
        retries=0,
    )
