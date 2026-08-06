import asyncio
import base64
import json

import pytest

from deepsee_server.protocols import gemini as gemini_protocol


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_parse_request_inline_data_image(sample_image_bytes):
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": _b64(sample_image_bytes),
                        }
                    },
                    {"text": "这是什么?"},
                ],
            }
        ]
    }
    text, image = gemini_protocol.parse_request(body)
    assert text == "这是什么?"
    assert image == sample_image_bytes


def test_parse_request_file_data_image():
    body = {
        "contents": [
            {
                "parts": [
                    {"file_data": {"file_uri": "https://example.com/a.png"}},
                    {"text": "q"},
                ]
            }
        ]
    }
    text, image = gemini_protocol.parse_request(body)
    assert text == "q"
    assert image == "https://example.com/a.png"


def test_parse_request_rejects_file_uri():
    body = {
        "contents": [
            {
                "parts": [
                    {"file_data": {"file_uri": "file:///etc/passwd"}},
                ]
            }
        ]
    }
    with pytest.raises(ValueError, match="不支持的图片 URL"):
        gemini_protocol.parse_request(body)


def test_parse_request_no_image():
    text, image = gemini_protocol.parse_request(
        {"contents": [{"parts": [{"text": "你好"}]}]}
    )
    assert text == "你好"
    assert image is None


def test_encode_text_vision_part_first():
    payload = gemini_protocol.encode_text("白猫", "视觉分析", "gemini-2.0-flash")
    parts = payload["candidates"][0]["content"]["parts"]
    assert parts[0] == {"text": "视觉分析", "vision": True}
    assert parts[1] == {"text": "白猫"}


def test_encode_text_no_vision():
    payload = gemini_protocol.encode_text("你好", None, "m")
    assert payload["candidates"][0]["content"]["parts"] == [{"text": "你好"}]


async def _chunks():
    yield "你"
    yield "好"


def test_encode_stream_first_chunk_carries_vision():
    async def _run():
        out = []
        async for chunk in gemini_protocol.encode_stream(
            _chunks(), "视觉分析", "m"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    chunks = [json.loads(c) for c in out]
    first_parts = chunks[0]["candidates"][0]["content"]["parts"]
    assert first_parts[0] == {"text": "视觉分析", "vision": True}
    assert first_parts[1] == {"text": "你"}
    second_parts = chunks[1]["candidates"][0]["content"]["parts"]
    assert second_parts == [{"text": "好"}]
