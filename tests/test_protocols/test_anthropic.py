import asyncio
import base64
import json

import pytest

from deepsee_server.protocols import anthropic as anthropic_protocol


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_parse_request_base64_image_and_text(sample_image_bytes):
    body = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": _b64(sample_image_bytes),
                        },
                    },
                    {"type": "text", "text": "这是什么?"},
                ],
            }
        ],
    }
    text, image = anthropic_protocol.parse_request(body)
    assert text == "这是什么?"
    assert image == sample_image_bytes


def test_parse_request_url_image(sample_image_bytes):
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": "https://example.com/a.png",
                        },
                    },
                    {"type": "text", "text": "q"},
                ],
            }
        ]
    }
    text, image = anthropic_protocol.parse_request(body)
    assert text == "q"
    assert image == "https://example.com/a.png"


def test_parse_request_rejects_file_url():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "file:///etc/passwd"},
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="不支持的图片 URL"):
        anthropic_protocol.parse_request(body)


def test_parse_request_no_image():
    text, image = anthropic_protocol.parse_request(
        {"messages": [{"role": "user", "content": "你好"}]}
    )
    assert text == "你好"
    assert image is None


def test_encode_text_carries_vision():
    payload = anthropic_protocol.encode_text("白猫", "视觉分析", "claude-3-5-sonnet")
    assert payload["content"] == [{"type": "text", "text": "白猫"}]
    assert payload["vision_analysis"] == "视觉分析"
    assert payload["model"] == "claude-3-5-sonnet"


def test_encode_text_no_vision():
    payload = anthropic_protocol.encode_text("你好", None, "m")
    assert "vision_analysis" not in payload


async def _chunks():
    yield "你"
    yield "好"


def test_encode_stream_emits_vision_event_before_content():
    async def _run():
        out = []
        async for chunk in anthropic_protocol.encode_stream(
            _chunks(), "视觉分析", "m"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    lines = [ln for ln in b"".join(out).decode().splitlines() if ln.startswith("data: ")]
    events = [json.loads(ln[6:]) for ln in lines]
    assert events[0]["type"] == "message_start"
    assert events[1]["type"] == "vision_analysis"
    assert events[1]["vision"] == "视觉分析"
    assert events[2]["type"] == "content_block_start"
    # 回答文本以 text_delta 逐块到达
    deltas = [e for e in events if e["type"] == "content_block_delta"]
    assert [d["delta"]["text"] for d in deltas] == ["你", "好"]
    assert events[-1]["type"] == "message_stop"
