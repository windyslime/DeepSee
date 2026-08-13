import asyncio
import base64
import json

import pytest

from deepsee.errors import ComposeError
from deepsee_server.protocols import openai as openai_protocol
from deepsee_server.protocols.base import (
    MAX_IMAGE_BYTES,
    decode_base64_image,
    extract_image_from_url,
)


def _data_url(b: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(b).decode()}"


def test_parse_request_text_and_data_url_image(sample_image_bytes):
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这是什么?"},
                    {"type": "image_url", "image_url": {"url": _data_url(sample_image_bytes)}},
                ],
            }
        ]
    }
    text, image = openai_protocol.parse_request(body)
    assert text == "这是什么?"
    assert image == sample_image_bytes


def test_parse_request_http_url_passthrough():
    body = {
        "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
            ]}
        ]
    }
    text, image = openai_protocol.parse_request(body)
    assert text == ""
    assert image == "https://example.com/a.png"


def test_parse_request_rejects_file_url():
    body = {
        "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}}
            ]}
        ]
    }
    with pytest.raises(ValueError, match="不支持的图片 URL"):
        openai_protocol.parse_request(body)


def test_parse_request_no_image():
    text, image = openai_protocol.parse_request(
        {"messages": [{"role": "user", "content": "你好"}]}
    )
    assert text == "你好"
    assert image is None


def test_parse_request_rejects_malformed_structure():
    with pytest.raises(ValueError, match="必须是对象"):
        openai_protocol.parse_request({"messages": [None]})
    with pytest.raises(ValueError, match="必须是对象"):
        openai_protocol.parse_request(
            {"messages": [{"role": "user", "content": [None]}]}
        )
    with pytest.raises(ValueError, match="必须是对象"):
        openai_protocol.parse_request(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": None}],
                    }
                ]
            }
        )


def test_parse_request_picks_last_image():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://a.example/1.png"}},
                    {"type": "image_url", "image_url": {"url": "https://b.example/2.png"}},
                ],
            }
        ]
    }
    _, image = openai_protocol.parse_request(body)
    assert image == "https://b.example/2.png"


def test_parse_request_rejects_null_container():
    """容器字段为 null 时必须抛 ValueError(端点映射 400),而非 TypeError/500。"""
    with pytest.raises(ValueError, match="必须是数组"):
        openai_protocol.parse_request({"messages": None})
    with pytest.raises(ValueError, match="必须是数组"):
        openai_protocol.parse_request({"messages": "not-a-list"})


def test_parse_request_rejects_non_string_text():
    with pytest.raises(ValueError, match="text 必须是字符串"):
        openai_protocol.parse_request(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": {"a": 1}}]}
                ]
            }
        )


def test_parse_request_rejects_invalid_content_type():
    """content 字段存在但既非字符串也非数组时必须 400,不得静默忽略。"""
    with pytest.raises(ValueError, match="content 必须是字符串或数组"):
        openai_protocol.parse_request(
            {"messages": [{"role": "user", "content": 123}]}
        )
    with pytest.raises(ValueError, match="content 必须是字符串或数组"):
        openai_protocol.parse_request(
            {"messages": [{"role": "user", "content": {"a": 1}}]}
        )


def test_parse_request_rejects_falsy_image_leaf():
    """url 字段存在但非字符串(如 0)时不得被当作"无图"忽略。"""
    with pytest.raises(ValueError, match="url 必须是字符串"):
        openai_protocol.parse_request(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {"type": "image_url", "image_url": {"url": 0}},
                        ],
                    }
                ]
            }
        )


def test_parse_request_rejects_invalid_content_on_non_user_role():
    """非 user 消息携带非法 content 同样 400(形状校验先于 role 检查)。"""
    with pytest.raises(ValueError, match="content 必须是字符串或数组"):
        openai_protocol.parse_request(
            {
                "messages": [
                    {"role": "assistant", "content": 123},
                    {"role": "user", "content": "你好"},
                ]
            }
        )


def test_extract_image_from_url_over_limit(sample_image_bytes, monkeypatch):
    monkeypatch.setattr("deepsee_server.protocols.base.MAX_IMAGE_BYTES", 4)
    with pytest.raises(ValueError, match="图片数据过大"):
        extract_image_from_url(_data_url(sample_image_bytes))


@pytest.mark.parametrize(
    "value",
    ["data:image/png;base64,!!!!", "data:image/png;base64,"],
)
def test_extract_image_from_url_rejects_invalid_or_empty_base64(value):
    with pytest.raises(ValueError, match="base64"):
        extract_image_from_url(value)


@pytest.mark.parametrize("value", ["!!!!", ""])
def test_decode_base64_image_rejects_invalid_or_empty_data(value):
    with pytest.raises(ValueError, match="base64"):
        decode_base64_image(value)


def test_encode_text_carries_vision():
    payload = openai_protocol.encode_text("白猫", "图片里有一只猫", "deepseek-chat")
    assert payload["choices"][0]["message"]["content"] == "白猫"
    assert payload["choices"][0]["message"]["vision_analysis"] == "图片里有一只猫"
    assert payload["model"] == "deepseek-chat"


def test_encode_text_no_vision():
    payload = openai_protocol.encode_text("你好", None, "deepseek-chat")
    assert "vision_analysis" not in payload["choices"][0]["message"]


async def _chunks():
    yield "你"
    yield "好"


def test_encode_stream_vision_is_leading_chunk():
    async def _run():
        out = []
        async for chunk in openai_protocol.encode_stream(
            _chunks(), "视觉分析内容", "deepseek-chat"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    lines = [ln for ln in b"".join(out).decode().splitlines() if ln.startswith("data: ")]
    first = json.loads(lines[0][6:])
    # vision 是独立前置 chunk:只带 vision_analysis,不带 content
    assert first["choices"][0]["delta"]["vision_analysis"] == "视觉分析内容"
    assert "content" not in first["choices"][0]["delta"]
    second = json.loads(lines[1][6:])
    assert second["choices"][0]["delta"]["content"] == "你"
    assert "vision_analysis" not in second["choices"][0]["delta"]
    assert lines[-1] == "data: [DONE]"


def test_encode_stream_empty_chunks_keeps_vision():
    """上游零文本时,vision 仍作为首个 chunk 发出(不丢失)。"""

    async def empty():
        return
        yield  # pragma: no cover

    async def _run():
        out = []
        async for chunk in openai_protocol.encode_stream(
            empty(), "视觉分析内容", "deepseek-chat"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    lines = [ln for ln in b"".join(out).decode().splitlines() if ln.startswith("data: ")]
    assert len(lines) == 2  # vision chunk + [DONE]
    first = json.loads(lines[0][6:])
    assert first["choices"][0]["delta"]["vision_analysis"] == "视觉分析内容"
    assert lines[-1] == "data: [DONE]"


def test_parse_chat_request_preserves_full_history_and_params():
    messages = [
        {"role": "system", "content": "Use tools."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
        },
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
    ]
    body = {
        "model": "client-model",
        "messages": messages,
        "stream": False,
        "tools": [],
        "tool_choice": "auto",
        "max_completion_tokens": 100,
    }

    parsed = openai_protocol.parse_chat_request(body)

    assert parsed.messages == messages
    assert parsed.stream is False
    assert parsed.image_count == 0
    assert parsed.params == {
        "tools": [],
        "tool_choice": "auto",
        "max_completion_tokens": 100,
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"messages": []}, "不能为空"),
        ({"messages": [{"role": "developer", "content": "x"}]}, "role"),
        ({"messages": [{"role": "assistant", "content": None}]}, "content"),
        ({"messages": [{"role": "user", "content": "x"}], "stream": "false"}, "stream"),
        ({"messages": [{"role": "user", "content": "x"}], "unexpected": True}, "不支持"),
    ],
)
def test_parse_chat_request_rejects_invalid_shape(body, message):
    with pytest.raises(ValueError, match=message):
        openai_protocol.parse_chat_request(body)


def test_parse_chat_request_counts_images_and_preserves_unknown_content_blocks():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "input_audio", "audio": {"data": "opaque"}},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/a.png"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/b.png"},
                },
            ],
        }
    ]

    parsed = openai_protocol.parse_chat_request({"messages": messages})

    assert parsed.image_count == 2
    assert parsed.messages == messages


def test_encode_upstream_response_preserves_all_fields_and_gates_vision():
    upstream = {
        "id": "upstream-id",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "think",
                    "tool_calls": [{"id": "call-2"}],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }

    standard = openai_protocol.encode_upstream_response(upstream)
    extended = openai_protocol.encode_upstream_response(
        upstream, vision="button visible", include_vision=True
    )

    assert standard == upstream
    assert "vision_analysis" not in standard["choices"][0]["message"]
    assert extended["id"] == "upstream-id"
    assert extended["choices"][0]["message"]["reasoning_content"] == "think"
    assert extended["choices"][0]["message"]["tool_calls"] == [{"id": "call-2"}]
    assert extended["choices"][0]["message"]["vision_analysis"] == "button visible"
    assert "vision_analysis" not in upstream["choices"][0]["message"]


def test_encode_upstream_stream_preserves_tool_delta_and_finish_reason():
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

    async def run():
        async def source():
            for chunk in chunks:
                yield chunk

        return [
            line async for line in openai_protocol.encode_upstream_stream(
                source(), vision="button visible", include_vision=True
            )
        ]

    lines = asyncio.run(run())
    payloads = [json.loads(line[6:]) for line in lines if line.startswith(b"data: ") and line != b"data: [DONE]\n\n"]
    assert [payload["id"] for payload in payloads] == ["stable-id"] * 3
    assert payloads[0]["choices"][0]["delta"]["vision_analysis"] == "button visible"
    assert payloads[1]["choices"][0]["delta"]["tool_calls"][0]["id"] == "call-1"
    assert payloads[2]["choices"][0]["finish_reason"] == "tool_calls"
    assert lines[-1] == b"data: [DONE]\n\n"


def test_encode_upstream_stream_emits_error_without_success_tail():
    async def source():
        yield {
            "id": "stable-id",
            "choices": [{"delta": {"content": "partial"}, "finish_reason": None}],
        }
        raise ComposeError("upstream failed")

    async def run():
        return [
            line
            async for line in openai_protocol.encode_upstream_stream(source())
        ]

    lines = asyncio.run(run())
    assert b'"content": "partial"' in lines[0]
    assert b'"type": "upstream_error"' in lines[1]
    assert b'"finish_reason": "stop"' not in b"".join(lines)
    assert lines[-1] == b"data: [DONE]\n\n"


def test_encode_upstream_stream_closes_cleanly_on_client_disconnect():
    closed = False

    async def source():
        nonlocal closed
        try:
            yield {
                "id": "stable-id",
                "choices": [
                    {"delta": {"content": "partial"}, "finish_reason": None}
                ],
            }
            await asyncio.sleep(3600)
        finally:
            closed = True

    async def run():
        stream = openai_protocol.encode_upstream_stream(source())
        first = await stream.__anext__()
        await stream.aclose()
        return first

    first = asyncio.run(run())
    assert b'"content": "partial"' in first
    assert closed is True
