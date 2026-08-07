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


def test_parse_request_rejects_malformed_structure():
    with pytest.raises(ValueError, match="必须是对象"):
        gemini_protocol.parse_request({"contents": [None]})
    with pytest.raises(ValueError, match="必须是对象"):
        gemini_protocol.parse_request({"contents": [{"parts": [None]}]})
    with pytest.raises(ValueError, match="必须是对象"):
        gemini_protocol.parse_request(
            {"contents": [{"parts": [{"inline_data": None}]}]}
        )


def test_parse_request_picks_last_image():
    body = {
        "contents": [
            {
                "parts": [
                    {"file_data": {"file_uri": "https://a.example/1.png"}},
                    {"file_data": {"file_uri": "https://b.example/2.png"}},
                ]
            }
        ]
    }
    _, image = gemini_protocol.parse_request(body)
    assert image == "https://b.example/2.png"


def test_parse_request_rejects_null_container():
    """容器字段为 null 时必须抛 ValueError(端点映射 400),而非 TypeError/500。"""
    with pytest.raises(ValueError, match="必须是数组"):
        gemini_protocol.parse_request({"contents": None})
    with pytest.raises(ValueError, match="必须是数组"):
        gemini_protocol.parse_request({"contents": [{"parts": None}]})
    with pytest.raises(ValueError, match="必须是数组"):
        gemini_protocol.parse_request({"contents": [{"parts": "not-a-list"}]})


def test_parse_request_rejects_non_string_text():
    with pytest.raises(ValueError, match="text 必须是字符串"):
        gemini_protocol.parse_request(
            {"contents": [{"parts": [{"text": {"a": 1}}]}]}
        )


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


def test_encode_stream_vision_is_leading_chunk():
    async def _run():
        out = []
        async for chunk in gemini_protocol.encode_stream(
            _chunks(), "视觉分析", "m"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    chunks = [json.loads(c) for c in out]
    # vision 是独立前置 chunk:只带 vision part,不带回答文本
    first_parts = chunks[0]["candidates"][0]["content"]["parts"]
    assert first_parts == [{"text": "视觉分析", "vision": True}]
    second_parts = chunks[1]["candidates"][0]["content"]["parts"]
    assert second_parts == [{"text": "你"}]
    third_parts = chunks[2]["candidates"][0]["content"]["parts"]
    assert third_parts == [{"text": "好"}]


def test_encode_stream_empty_chunks_keeps_vision():
    """上游零文本时,vision 仍作为首个 chunk 发出(不丢失)。"""

    async def empty():
        return
        yield  # pragma: no cover

    async def _run():
        out = []
        async for chunk in gemini_protocol.encode_stream(
            empty(), "视觉分析", "m"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    chunks = [json.loads(c) for c in out]
    assert len(chunks) == 1
    assert chunks[0]["candidates"][0]["content"]["parts"] == [
        {"text": "视觉分析", "vision": True}
    ]
