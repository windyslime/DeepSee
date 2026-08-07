import asyncio
import base64
import json

import pytest

from deepsee_server.protocols import openai as openai_protocol
from deepsee_server.protocols.base import MAX_IMAGE_BYTES, extract_image_from_url


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


def test_extract_image_from_url_over_limit(sample_image_bytes, monkeypatch):
    monkeypatch.setattr("deepsee_server.protocols.base.MAX_IMAGE_BYTES", 4)
    with pytest.raises(ValueError, match="图片数据过大"):
        extract_image_from_url(_data_url(sample_image_bytes))


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
