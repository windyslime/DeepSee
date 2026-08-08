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


def test_encode_stream_closes_chunks_on_disconnect_during_prefix():
    """客户端在前置事件阶段断开时,上游迭代器必须被 aclose(不依赖 GC)。"""
    closed = []

    class Tracking:
        def __init__(self, inner):
            self._inner = inner

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self._inner.__anext__()

        async def aclose(self):
            closed.append(True)
            await self._inner.aclose()

    async def _run():
        ag = anthropic_protocol.encode_stream(Tracking(_chunks()), "视觉", "m")
        it = ag.__aiter__()
        await it.__anext__()  # message_start
        await it.__anext__()  # vision_analysis —— 客户端在此断开
        await ag.aclose()
        return closed

    asyncio.run(_run())
    assert closed == [True]


def test_encode_stream_no_success_tail_after_error():
    """上游错误后只发 error 事件,不再发 end_turn/message_stop 成功收尾。"""
    from deepsee.errors import ComposeError

    async def failing():
        yield "部分"
        raise ComposeError("boom", model="m")

    async def _run():
        out = []
        async for chunk in anthropic_protocol.encode_stream(failing(), None, "m"):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    lines = [ln for ln in b"".join(out).decode().splitlines() if ln.startswith("data: ")]
    events = [json.loads(ln[6:]) for ln in lines]
    types = [e["type"] for e in events]
    assert "error" in types
    assert "message_delta" not in types
    assert "message_stop" not in types
    assert "content_block_stop" not in types


def test_parse_request_rejects_malformed_content():
    with pytest.raises(ValueError, match="必须是对象"):
        anthropic_protocol.parse_request(
            {"messages": [{"role": "user", "content": [None]}]}
        )
    with pytest.raises(ValueError, match="必须是对象"):
        anthropic_protocol.parse_request(
            {"messages": [{"role": "user", "content": [{"type": "image"}]}]}
        )


def test_parse_request_picks_last_image():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "url", "url": "https://a.example/1.png"}},
                    {"type": "image", "source": {"type": "url", "url": "https://b.example/2.png"}},
                ],
            }
        ]
    }
    _, image = anthropic_protocol.parse_request(body)
    assert image == "https://b.example/2.png"


def test_parse_request_rejects_null_container():
    """容器字段为 null 时必须抛 ValueError(端点映射 400),而非 TypeError/500。"""
    with pytest.raises(ValueError, match="必须是数组"):
        anthropic_protocol.parse_request({"messages": None})
    with pytest.raises(ValueError, match="必须是数组"):
        anthropic_protocol.parse_request({"messages": "not-a-list"})


def test_parse_request_rejects_non_string_text():
    with pytest.raises(ValueError, match="text 必须是字符串"):
        anthropic_protocol.parse_request(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": 123}]}
                ]
            }
        )


def test_parse_request_rejects_invalid_content_type():
    """content 字段存在但既非字符串也非数组时必须 400,不得静默忽略。"""
    with pytest.raises(ValueError, match="content 必须是字符串或数组"):
        anthropic_protocol.parse_request(
            {"messages": [{"role": "user", "content": 123}]}
        )
    with pytest.raises(ValueError, match="content 必须是字符串或数组"):
        anthropic_protocol.parse_request(
            {"messages": [{"role": "user", "content": {"a": 1}}]}
        )


def test_parse_request_rejects_falsy_image_leaf():
    """data 字段存在但非字符串(如 0)时不得被当作"无图"忽略。"""
    with pytest.raises(ValueError, match="data 必须是字符串"):
        anthropic_protocol.parse_request(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": 0},
                            },
                        ],
                    }
                ]
            }
        )


def test_parse_request_rejects_invalid_content_on_non_user_role():
    """非 user 消息携带非法 content 同样 400(形状校验先于 role 检查)。"""
    with pytest.raises(ValueError, match="content 必须是字符串或数组"):
        anthropic_protocol.parse_request(
            {
                "messages": [
                    {"role": "assistant", "content": None},
                    {"role": "user", "content": "你好"},
                ]
            }
        )
