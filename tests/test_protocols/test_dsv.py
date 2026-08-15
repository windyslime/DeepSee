import asyncio
import base64
import json

import pytest

from deepsee.errors import ComposeError
from deepsee_server.protocols import dsv


def _data_url(data: bytes = b"image", media_type: str = "image/png") -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _vision() -> dict:
    return {
        "analysis": "图里有一只猫",
        "mode": "auto",
        "backend": "openai_compatible",
        "model": "vision-model",
        "latency_ms": 12,
        "cache_hit": False,
        "trace_id": "trace-1",
    }


def test_parse_dsv_normalizes_base64_image_and_vision_options():
    request = dsv.parse_request(
        {
            "model": "deepseek-chat",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "图里有什么?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(b"image").decode("ascii"),
                            },
                        },
                    ],
                }
            ],
            "vision": {"mode": "ui", "include_analysis": False},
            "stream": True,
            "tools": [],
        }
    )

    assert request.stream is True
    assert request.model == "deepseek-chat"
    assert request.vision_mode == "ui"
    assert request.include_analysis is False
    assert request.image_count == 1
    image = request.messages[0]["content"][1]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"] == _data_url()


def test_parse_dsv_accepts_openai_image_url_and_tool_result():
    request = dsv.parse_request(
        {
            "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": _data_url()}}]},
                {"role": "tool", "tool_call_id": "call-1", "content": "done"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
        }
    )

    assert request.image_count == 1
    assert request.params["tools"][0]["function"]["name"] == "lookup"
    assert request.messages[-1]["role"] == "tool"


@pytest.mark.parametrize(
    "body, message",
    [
        ({"messages": [{"role": "user", "content": "no image"}]}, "至少包含一张图片"),
        (
            {
                "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "url", "url": "file:///etc/passwd"}}]}]
            },
            "不支持的图片 URL",
        ),
        (
            {
                "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "bad"}}]}]
            },
            "图片 base64 数据非法",
        ),
    ],
)
def test_parse_dsv_rejects_invalid_images(body, message):
    with pytest.raises(ValueError, match=message):
        dsv.parse_request(body)


def test_parse_dsv_rejects_invalid_vision_options():
    with pytest.raises(ValueError, match="vision.mode"):
        dsv.parse_request(
            {
                "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": _data_url()}}]}],
                "vision": {"mode": "bad"},
            }
        )


def test_encode_dsv_response_separates_vision_answer_and_tool_calls():
    upstream = {
        "id": "upstream-id",
        "model": "deepseek-chat",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "需要查一下",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"total_tokens": 9},
    }

    payload = dsv.encode_response(upstream, vision=_vision())

    assert payload["object"] == "dsv.response"
    assert payload["status"] == "requires_action"
    assert payload["vision"] == _vision()
    assert payload["answer"] == {"text": ""}
    assert payload["reasoning"] == {"text": "需要查一下"}
    assert payload["tool_calls"][0]["id"] == "call-1"
    assert payload["usage"] == {"total_tokens": 9}


def test_encode_dsv_stream_orders_vision_answer_and_requires_action():
    async def source():
        yield {
            "id": "stream-id",
            "model": "deepseek-chat",
            "choices": [{"delta": {"reasoning_content": "先想"}, "finish_reason": None}],
        }
        yield {
            "id": "stream-id",
            "choices": [{"delta": {"content": "结论"}, "finish_reason": None}],
        }
        yield {
            "id": "stream-id",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }]
                },
                "finish_reason": "tool_calls",
            }],
        }

    async def collect():
        frames = [frame async for frame in dsv.encode_stream(source(), vision=_vision())]
        return [json.loads(frame.decode()[6:]) for frame in frames if frame != b"data: [DONE]\n\n"]

    events = asyncio.run(collect())
    types = [event["type"] for event in events]
    assert len({event["id"] for event in events}) == 1
    assert types[:4] == ["response.created", "vision.started", "vision.completed", "reasoning.started"]
    assert "reasoning.delta" in types
    assert "answer.delta" in types
    assert "tool_call.completed" in types
    assert types[-2:] == ["response.requires_action", "response.completed"]
    assert events[2]["vision"] == _vision()
    assert events[-2]["tool_calls"][0]["id"] == "call-1"


def test_encode_dsv_stream_emits_error_without_success_completion():
    async def source():
        yield {"id": "stream-id", "choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}
        raise ComposeError("deepseek failed")

    async def collect():
        frames = [frame async for frame in dsv.encode_stream(source(), vision=_vision())]
        return [json.loads(frame.decode()[6:]) for frame in frames if frame != b"data: [DONE]\n\n"]

    events = asyncio.run(collect())
    assert any(event["type"] == "error" for event in events)
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["status"] == "failed"
    assert not any(event["type"] == "answer.completed" for event in events)


def test_encode_dsv_stream_closes_upstream_on_disconnect():
    closed = False

    async def source():
        nonlocal closed
        try:
            yield {
                "id": "stream-id",
                "choices": [{"delta": {"content": "partial"}, "finish_reason": None}],
            }
            await asyncio.sleep(60)
        finally:
            closed = True

    async def close_after_first_upstream_chunk():
        stream = dsv.encode_stream(source(), vision=_vision())
        for _ in range(4):
            await stream.__anext__()
        await stream.aclose()

    asyncio.run(close_after_first_upstream_chunk())
    assert closed is True


def test_encode_dsv_error_stream_marks_stage_and_failure():
    async def collect():
        frames = [
            frame
            async for frame in dsv.encode_error_stream(
                stage="vision", message="视觉服务失败"
            )
        ]
        return [json.loads(frame.decode()[6:]) for frame in frames if frame != b"data: [DONE]\n\n"]

    events = asyncio.run(collect())
    assert [event["type"] for event in events] == [
        "response.created",
        "vision.started",
        "error",
        "response.completed",
    ]
    assert events[2]["stage"] == "vision"
    assert events[-1]["status"] == "failed"


def test_encode_dsv_error_response_preserves_vision_metadata():
    payload = dsv.encode_error_response(
        stage="reasoning",
        message="DeepSeek failed",
        vision=_vision(),
        include_analysis=False,
    )

    assert payload["status"] == "failed"
    assert "analysis" not in payload["vision"]
    assert payload["vision"]["model"] == "vision-model"
    assert payload["error"]["stage"] == "reasoning"
