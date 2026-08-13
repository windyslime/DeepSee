import asyncio
import copy
import json

import httpx
import pytest
import respx

from deepsee.composer.chat import chat_async
from deepsee.config.loader import Config, DeepSeekConfig, VisionConfig
from deepsee.errors import ComposeError


@pytest.fixture
def config():
    return Config(
        deepseek=DeepSeekConfig(
            api_key="sk-deepseek-test",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        ),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="sk-vision-test",
            base_url="https://vision.example.com/v1",
            model="vision-test",
        ),
        retries=0,
    )


def test_chat_async_preserves_messages_params_inputs_and_response(config):
    messages = [
        {"role": "system", "content": "Use tools."},
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
    params = {"tools": [], "tool_choice": "auto", "temperature": 0.2}
    original_messages = copy.deepcopy(messages)
    original_params = copy.deepcopy(params)
    upstream = {
        "id": "upstream-id",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"total_tokens": 7},
    }

    async def run():
        async with respx.mock:
            route = respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(200, json=upstream)
            )
            result = await chat_async(messages, config=config, params=params)
            return result, json.loads(route.calls.last.request.content)

    result, sent = asyncio.run(run())
    assert result == upstream
    assert sent["messages"] == messages
    assert sent["tools"] == []
    assert sent["tool_choice"] == "auto"
    assert sent["temperature"] == 0.2
    assert messages == original_messages
    assert params == original_params


def test_chat_async_stream_preserves_complete_chunks(config):
    body = (
        'data: {"id":"stable","choices":[{"delta":{"tool_calls":'
        '[{"index":0,"id":"call-1","type":"function","function":'
        '{"name":"read","arguments":"{}"}}]},"finish_reason":null}]}\n\n'
        'data: {"id":"stable","choices":[{"delta":{},'
        '"finish_reason":"tool_calls"}],"usage":{"total_tokens":5}}\n\n'
        "data: [DONE]\n\n"
    )

    async def run():
        async with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(200, content=body.encode())
            )
            chunks = await chat_async(
                [{"role": "user", "content": "go"}],
                stream=True,
                config=config,
                params={"tools": []},
            )
            return [chunk async for chunk in chunks]

    chunks = asyncio.run(run())
    assert [chunk["id"] for chunk in chunks] == ["stable", "stable"]
    assert chunks[0]["choices"][0]["delta"]["tool_calls"][0]["id"] == "call-1"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"]["total_tokens"] == 5


def test_chat_async_rejects_non_object_response(config):
    async def run():
        async with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(200, json=[])
            )
            await chat_async([{"role": "user", "content": "go"}], config=config)

    with pytest.raises(ComposeError, match="响应解析失败"):
        asyncio.run(run())


def test_chat_async_stream_wraps_invalid_json(config):
    async def run():
        async with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(200, content=b"data: {bad json}\n\n")
            )
            chunks = await chat_async(
                [{"role": "user", "content": "go"}],
                stream=True,
                config=config,
            )
            return [chunk async for chunk in chunks]

    with pytest.raises(ComposeError, match="流式响应解析失败"):
        asyncio.run(run())
