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


def test_encode_stream_first_chunk_carries_vision():
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
    assert first["choices"][0]["delta"]["vision_analysis"] == "视觉分析内容"
    assert first["choices"][0]["delta"]["content"] == "你"
    second = json.loads(lines[1][6:])
    assert "vision_analysis" not in second["choices"][0]["delta"]
    assert lines[-1] == "data: [DONE]"
