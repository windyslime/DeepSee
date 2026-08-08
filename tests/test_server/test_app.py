import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import deepsee.pipeline.image as image_module
from deepsee.config.loader import Config, DeepSeekConfig, VisionConfig

from deepsee_server.app import app

client = TestClient(app)


@pytest.fixture
def cfg():
    return Config(
        deepseek=DeepSeekConfig(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        ),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="v-key",
            model="qwen-vl-max",
            base_url="https://vision.example.com/v1",
        ),
        retries=0,
    )


@pytest.fixture
def use_cfg(monkeypatch, cfg):
    monkeypatch.setattr("deepsee_server.app._current_config", lambda: cfg)


def _png_data_url() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


def test_models_endpoint(use_cfg):
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data[0]["id"] == "deepseek-chat"  # 从配置动态读取,不写死
    assert data[0]["owned_by"] == "deepsee"


def test_chat_text(use_cfg, monkeypatch):
    async def fake_ask(question, **kw):
        return "你好!"

    monkeypatch.setattr("deepsee_server.app.ask_async", fake_ask)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "anything", "messages": [{"role": "user", "content": "你好"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "你好!"
    assert body["model"] == "deepseek-chat"


def test_chat_with_image(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    seen = {}

    async def fake_ask_with_image(image, question, **kw):
        seen["image"] = image
        seen["question"] = question
        return VisionResult(vision="图里有一只猫", text="图里是一只猫")

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "图里有什么?"},
                        {"type": "image_url", "image_url": {"url": _png_data_url()}},
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()["choices"][0]["message"]
    assert body["content"] == "图里是一只猫"
    assert body["vision_analysis"] == "图里有一只猫"
    assert isinstance(seen["image"], bytes)
    assert seen["question"] == "图里有什么?"


def test_chat_stream(use_cfg, monkeypatch):
    async def fake_ask(question, **kw):
        async def gen():
            yield "你"
            yield "好"

        return gen()

    monkeypatch.setattr("deepsee_server.app.ask_async", fake_ask)
    resp = client.post(
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    first = json.loads(lines[0][6:])
    assert first["choices"][0]["delta"]["content"] == "你"
    second = json.loads(lines[1][6:])
    assert second["choices"][0]["delta"]["content"] == "好"


def test_chat_empty_messages_400(use_cfg):
    resp = client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status_code == 400


def test_chat_bad_image_400(use_cfg):
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "not-a-url"}}
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 400


def test_chat_rejects_file_url_400(use_cfg):
    # 服务端入口禁止本地路径/file://,防止任意文件读取
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}}
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 400


def test_chat_data_url_over_limit_400(use_cfg, monkeypatch):
    monkeypatch.setattr("deepsee_server.protocols.base.MAX_IMAGE_BYTES", 64)
    big_b64 = base64.b64encode(b"x" * 512).decode("ascii")
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{big_b64}"},
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 400


def test_chat_body_too_large_413(use_cfg, monkeypatch):
    monkeypatch.setattr("deepsee_server.app._MAX_REQUEST_BODY", 64)
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x" * 4096}]},
    )
    assert resp.status_code == 413


def test_analyze_body_too_large_413(use_cfg, monkeypatch):
    monkeypatch.setattr("deepsee_server.app._MAX_REQUEST_BODY", 64)
    resp = client.post("/analyze", json={"image": "x" * 4096})
    assert resp.status_code == 413


def test_analyze_endpoint(use_cfg, monkeypatch):
    """analyze 是纯视觉分析(设计 §5: describe_image_async),不调 DeepSeek。"""
    seen = {}

    async def fake_describe(image, prompt, **kw):
        seen["image"] = image
        seen["prompt"] = prompt
        return "分析结果"

    monkeypatch.setattr("deepsee_server.app.describe_image_async", fake_describe)
    resp = client.post(
        "/analyze", json={"image": _png_data_url(), "question": "这是什么?"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"kind": "description", "text": "分析结果"}
    assert isinstance(seen["image"], bytes)  # data URL 已解码为 bytes
    assert seen["prompt"] == "这是什么?"


def test_chat_chunked_body_too_large_413(use_cfg, monkeypatch):
    """无 Content-Length 的 chunked 请求必须在读取阶段被拦截。"""
    monkeypatch.setattr("deepsee_server.app._MAX_REQUEST_BODY", 64)
    payload = json.dumps(
        {"messages": [{"role": "user", "content": "x" * 4096}]}
    ).encode()
    resp = client.post("/v1/chat/completions", content=(c for c in [payload]))
    assert resp.status_code == 413


def test_analyze_chunked_body_too_large_413(use_cfg, monkeypatch):
    monkeypatch.setattr("deepsee_server.app._MAX_REQUEST_BODY", 64)
    resp = client.post("/analyze", content=(c for c in [b"x" * 4096]))
    assert resp.status_code == 413


def test_chat_numeric_image_url_400(use_cfg):
    # url 字段是数字等非字符串时不得 500
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": 123}}
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 400


def test_analyze_numeric_image_400(use_cfg):
    resp = client.post("/analyze", json={"image": 123, "question": "q"})
    assert resp.status_code == 400


def test_chat_image_error_maps_to_400(use_cfg, monkeypatch):
    from deepsee.errors import ImageError

    async def boom(*args, **kwargs):
        raise ImageError("图片下载失败: 目标被拒绝")

    monkeypatch.setattr("deepsee_server.app.ask_with_image_async", boom)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _png_data_url()}}
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 400
    assert "拒绝" in resp.json()["error"]["message"]


def test_analyze_image_error_maps_to_400(use_cfg, monkeypatch):
    from deepsee.errors import ImageError

    async def boom(*args, **kwargs):
        raise ImageError("图片解码失败")

    monkeypatch.setattr("deepsee_server.app.describe_image_async", boom)
    resp = client.post("/analyze", json={"image": _png_data_url()})
    assert resp.status_code == 400


def test_chat_invalid_json_400(use_cfg):
    resp = client.post("/v1/chat/completions", content=b"{not json")
    assert resp.status_code == 400


def test_analyze_invalid_json_400(use_cfg):
    resp = client.post("/analyze", content=b"not json")
    assert resp.status_code == 400


def test_chat_json_root_not_object_400(use_cfg):
    # 合法 JSON 但根节点不是对象([] / "hello" / null),不得 500
    for doc in (b"[]", b'"hello"', b"null", b"123"):
        resp = client.post("/v1/chat/completions", content=doc)
        assert resp.status_code == 400, f"{doc!r} -> {resp.status_code}"


def test_analyze_json_root_not_object_400(use_cfg):
    resp = client.post("/analyze", content=b"[]")
    assert resp.status_code == 400


def test_chat_upstream_error_maps_to_502(use_cfg, monkeypatch):
    from deepsee.errors import ComposeError

    async def boom(question, **kw):
        raise ComposeError(
            "DeepSeek API 请求失败: HTTP 502", model="deepseek-chat", status_code=502
        )

    monkeypatch.setattr("deepsee_server.app.ask_async", boom)
    resp = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["type"] == "upstream_error"
    assert "DeepSeek" in body["error"]["message"]


def test_chat_stream_upstream_error_emits_error_chunk(use_cfg, monkeypatch):
    from deepsee.errors import ComposeError

    async def boom(question, **kw):
        async def gen():
            yield "部分内容"
            raise ComposeError(
                "DeepSeek API 请求失败: HTTP 502",
                model="deepseek-chat",
                status_code=502,
            )

        return gen()

    monkeypatch.setattr("deepsee_server.app.ask_async", boom)
    resp = client.post(
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    # 正常 chunk 仍先到达
    assert json.loads(lines[0][6:])["choices"][0]["delta"]["content"] == "部分内容"
    # 上游错误以 error chunk 形式发出,而非截断流
    error_line = json.loads(lines[-2][6:])
    assert error_line["error"]["type"] == "upstream_error"


def test_analyze_upstream_error_maps_to_502(use_cfg, monkeypatch):
    from deepsee.errors import ComposeError

    async def boom(image, question, **kw):
        raise ComposeError(
            "DeepSeek API 请求失败: HTTP 502", model="deepseek-chat", status_code=502
        )

    monkeypatch.setattr("deepsee_server.app.describe_image_async", boom)
    resp = client.post("/analyze", json={"image": _png_data_url()})
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"


def test_chat_stream_acloses_iterator(use_cfg, monkeypatch):
    """流式回答消费完毕后,server 用 aclosing 保证底层 iterator 被 aclose,
    不依赖 GC(生成器自然耗尽不等于调用方调用了 aclose)。"""

    closed = []

    async def fake_ask(question, **kw):
        async def inner():
            yield "你"
            yield "好"

        class Tracked:
            def __init__(self):
                self._inner = inner()

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self._inner.__anext__()

            async def aclose(self):
                closed.append(True)
                await self._inner.aclose()

        return Tracked()

    monkeypatch.setattr("deepsee_server.app.ask_async", fake_ask)
    resp = client.post(
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    assert closed == [True]


def test_chat_stream_with_vision_first_chunk(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    async def fake_ask_with_image(image, question, **kw):
        async def gen():
            yield "你"
            yield "好"

        return VisionResult(vision="视觉分析内容", text=gen())

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1/chat/completions",
        json={
            "stream": True,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _png_data_url()}},
                        {"type": "text", "text": "hi"},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    first = json.loads(lines[0][6:])
    # vision 是独立前置 chunk:只带 vision_analysis
    assert first["choices"][0]["delta"]["vision_analysis"] == "视觉分析内容"
    assert "content" not in first["choices"][0]["delta"]
    second = json.loads(lines[1][6:])
    assert second["choices"][0]["delta"]["content"] == "你"
    assert lines[-1] == "data: [DONE]"


def test_messages_endpoint_anthropic(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    async def fake_ask_with_image(image, question, **kw):
        return VisionResult(vision="视觉分析", text="白猫")

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1/messages",
        json={
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
                                "media_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            },
                        },
                        {"type": "text", "text": "这是什么?"},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "白猫"}]
    assert body["vision_analysis"] == "视觉分析"


def test_messages_endpoint_no_image_plain_text(use_cfg, monkeypatch):
    async def fake_ask(question, **kw):
        return "你好!"

    monkeypatch.setattr("deepsee_server.app.ask_async", fake_ask)
    resp = client.post(
        "/v1/messages",
        json={"model": "m", "messages": [{"role": "user", "content": "你好"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == [{"type": "text", "text": "你好!"}]
    assert "vision_analysis" not in body


def test_gemini_endpoint(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    async def fake_ask_with_image(image, question, **kw):
        return VisionResult(vision="视觉分析", text="白猫")

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            }
                        },
                        {"text": "这是什么?"},
                    ]
                }
            ]
        },
    )
    assert resp.status_code == 200
    parts = resp.json()["candidates"][0]["content"]["parts"]
    assert parts[0] == {"text": "视觉分析", "vision": True}
    assert parts[1] == {"text": "白猫"}


def test_messages_endpoint_upstream_error_502(use_cfg, monkeypatch):
    from deepsee.errors import ComposeError

    async def boom(image, question, **kw):
        raise ComposeError("DeepSeek API 请求失败: HTTP 502", model="m", status_code=502)

    monkeypatch.setattr("deepsee_server.app.ask_with_image_async", boom)
    resp = client.post(
        "/v1/messages",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            },
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 502
    assert resp.json()["type"] == "error"
    assert resp.json()["error"]["type"] == "upstream_error"


def test_messages_endpoint_stream_anthropic(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    async def fake_ask_with_image(image, question, **kw):
        async def gen():
            yield "你"
            yield "好"

        return VisionResult(vision="视觉分析", text=gen())

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1/messages",
        json={
            "stream": True,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            },
                        }
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    events = [json.loads(ln[6:]) for ln in lines]
    assert events[0]["type"] == "message_start"
    assert events[1]["type"] == "vision_analysis"
    assert events[1]["vision"] == "视觉分析"
    deltas = [e for e in events if e["type"] == "content_block_delta"]
    assert [d["delta"]["text"] for d in deltas] == ["你", "好"]
    assert events[-1]["type"] == "message_stop"


def test_gemini_endpoint_stream(use_cfg, monkeypatch):
    from deepsee.composer.deepseek import VisionResult

    async def fake_ask_with_image(image, question, **kw):
        async def gen():
            yield "你"
            yield "好"

        return VisionResult(vision="视觉分析", text=gen())

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/v1beta/models/m:generateContent",
        json={
            "stream": True,
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            }
                        }
                    ]
                }
            ],
        },
    )
    assert resp.status_code == 200
    chunks = [json.loads(c) for c in resp.text.strip().splitlines()]
    assert chunks[0]["candidates"][0]["content"]["parts"] == [
        {"text": "视觉分析", "vision": True}
    ]
    assert chunks[1]["candidates"][0]["content"]["parts"] == [{"text": "你"}]
    assert chunks[2]["candidates"][0]["content"]["parts"] == [{"text": "好"}]


def test_gemini_endpoint_plain_text(use_cfg, monkeypatch):
    async def fake_ask(question, **kw):
        return "你好!"

    monkeypatch.setattr("deepsee_server.app.ask_async", fake_ask)
    resp = client.post(
        "/v1beta/models/m:generateContent",
        json={"contents": [{"parts": [{"text": "你好"}]}]},
    )
    assert resp.status_code == 200
    parts = resp.json()["candidates"][0]["content"]["parts"]
    assert parts == [{"text": "你好!"}]


def test_messages_endpoint_malformed_400(use_cfg):
    resp = client.post("/v1/messages", json={"messages": [None]})
    assert resp.status_code == 400
    assert resp.json()["type"] == "error"


def test_messages_endpoint_invalid_json_400_anthropic_shape(use_cfg):
    resp = client.post("/v1/messages", content=b"not json")
    assert resp.status_code == 400
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


def test_messages_endpoint_body_too_large_413_anthropic_shape(use_cfg, monkeypatch):
    monkeypatch.setattr("deepsee_server.app._MAX_REQUEST_BODY", 64)
    resp = client.post("/v1/messages", json={"model": "m", "messages": [{"role": "user", "content": "x" * 4096}]})
    assert resp.status_code == 413
    assert resp.json()["type"] == "error"


def test_gemini_endpoint_invalid_json_400_gemini_shape(use_cfg):
    resp = client.post(
        "/v1beta/models/m:generateContent", content=b"not json"
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == 400


def test_gemini_endpoint_body_too_large_413_gemini_shape(use_cfg, monkeypatch):
    monkeypatch.setattr("deepsee_server.app._MAX_REQUEST_BODY", 64)
    resp = client.post(
        "/v1beta/models/m:generateContent",
        json={"contents": [{"parts": [{"text": "x" * 4096}]}]},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == 413


def test_messages_endpoint_image_error_400(use_cfg, monkeypatch):
    from deepsee.errors import ImageError

    async def boom(image, question, **kw):
        raise ImageError("图片解码失败")

    monkeypatch.setattr("deepsee_server.app.ask_with_image_async", boom)
    resp = client.post(
        "/v1/messages",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            },
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_gemini_endpoint_image_error_400(use_cfg, monkeypatch):
    from deepsee.errors import ImageError

    async def boom(image, question, **kw):
        raise ImageError("图片解码失败")

    monkeypatch.setattr("deepsee_server.app.ask_with_image_async", boom)
    resp = client.post(
        "/v1beta/models/m:generateContent",
        json={
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            }
                        }
                    ]
                }
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == 400


def test_gemini_endpoint_upstream_error_502(use_cfg, monkeypatch):
    from deepsee.errors import ComposeError

    async def boom(image, question, **kw):
        raise ComposeError("DeepSeek API 请求失败: HTTP 502", model="m", status_code=502)

    monkeypatch.setattr("deepsee_server.app.ask_with_image_async", boom)
    resp = client.post(
        "/v1beta/models/m:generateContent",
        json={
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(_png_bytes()).decode(),
                            }
                        }
                    ]
                }
            ]
        },
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == 502


def test_messages_endpoint_stream_plain_text(use_cfg, monkeypatch):
    """验收矩阵:Anthropic 流式 + 无图。"""
    async def fake_ask(question, **kw):
        async def gen():
            yield "你"
            yield "好"

        return gen()

    monkeypatch.setattr("deepsee_server.app.ask_async", fake_ask)
    resp = client.post(
        "/v1/messages",
        json={"model": "m", "stream": True, "messages": [{"role": "user", "content": "你好"}]},
    )
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    events = [json.loads(ln[6:]) for ln in lines]
    assert events[0]["type"] == "message_start"
    # 无图:不应有 vision_analysis 事件
    assert all(e["type"] != "vision_analysis" for e in events)
    deltas = [e for e in events if e["type"] == "content_block_delta"]
    assert [d["delta"]["text"] for d in deltas] == ["你", "好"]
    assert events[-1]["type"] == "message_stop"


def test_gemini_endpoint_stream_plain_text(use_cfg, monkeypatch):
    """验收矩阵:Gemini 流式 + 无图。"""
    async def fake_ask(question, **kw):
        async def gen():
            yield "你"
            yield "好"

        return gen()

    monkeypatch.setattr("deepsee_server.app.ask_async", fake_ask)
    resp = client.post(
        "/v1beta/models/m:generateContent",
        json={"stream": True, "contents": [{"parts": [{"text": "你好"}]}]},
    )
    assert resp.status_code == 200
    chunks = [json.loads(c) for c in resp.text.strip().splitlines()]
    # 无图:所有 chunk 都不含 vision part
    for chunk in chunks:
        parts = chunk["candidates"][0]["content"]["parts"]
        assert all("vision" not in p for p in parts)
    assert chunks[0]["candidates"][0]["content"]["parts"] == [{"text": "你"}]
    assert chunks[1]["candidates"][0]["content"]["parts"] == [{"text": "好"}]


def test_messages_endpoint_null_container_400(use_cfg):
    """畸形容器(messages: null)必须返回协议形状 400,而非 500。"""
    resp = client.post("/v1/messages", json={"messages": None})
    assert resp.status_code == 400
    assert resp.json()["type"] == "error"


def test_gemini_endpoint_null_container_400(use_cfg):
    """畸形容器(contents: null)必须返回协议形状 400,而非 500。"""
    resp = client.post(
        "/v1beta/models/m:generateContent", json={"contents": None}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == 400


def test_chat_null_container_400(use_cfg):
    """畸形容器(messages: null)必须返回 OpenAI 形状 400,而非 500。"""
    resp = client.post("/v1/chat/completions", json={"messages": None})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_chat_content_invalid_type_400(use_cfg):
    """content 非字符串/数组必须 400,不得静默忽略并回答旧文本。"""
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "user", "content": "先前的文本"},
                {"role": "user", "content": 123},
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_messages_endpoint_content_invalid_type_400(use_cfg):
    resp = client.post(
        "/v1/messages",
        json={
            "messages": [
                {"role": "user", "content": "先前的文本"},
                {"role": "user", "content": 123},
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "error"


def test_chat_ssrf_private_url_400(use_cfg, monkeypatch):
    """私网 URL 图片在真实防护路径被拒绝,且未创建任何网络传输。"""
    sent = []
    orig = image_module._PinnedIPTransport.handle_request

    def tracking(self, request):
        sent.append(request)
        return orig(self, request)

    monkeypatch.setattr(
        image_module._PinnedIPTransport, "handle_request", tracking
    )
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "http://127.0.0.1/x.png"}}
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 400
    assert "已拒绝" in resp.json()["error"]["message"]
    assert sent == []  # 探针:未尝试建立连接


def test_messages_endpoint_ssrf_private_url_400(use_cfg, monkeypatch):
    sent = []
    orig = image_module._PinnedIPTransport.handle_request

    def tracking(self, request):
        sent.append(request)
        return orig(self, request)

    monkeypatch.setattr(
        image_module._PinnedIPTransport, "handle_request", tracking
    )
    resp = client.post(
        "/v1/messages",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "http://169.254.169.254/latest/meta-data/"},
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "error"
    assert "已拒绝" in resp.json()["error"]["message"]
    assert sent == []  # 探针:未尝试建立连接


def test_gemini_endpoint_ssrf_private_uri_400(use_cfg, monkeypatch):
    sent = []
    orig = image_module._PinnedIPTransport.handle_request

    def tracking(self, request):
        sent.append(request)
        return orig(self, request)

    monkeypatch.setattr(
        image_module._PinnedIPTransport, "handle_request", tracking
    )
    resp = client.post(
        "/v1beta/models/m:generateContent",
        json={
            "contents": [
                {"parts": [{"file_data": {"file_uri": "http://127.0.0.1/x.png"}}]}
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == 400
    assert "已拒绝" in resp.json()["error"]["message"]
    assert sent == []  # 探针:未尝试建立连接


def test_messages_endpoint_base64_over_limit_400(use_cfg, monkeypatch):
    """Anthropic base64 图片超限必须 400,且错误来自字节上限检查。"""
    monkeypatch.setattr("deepsee_server.protocols.base.MAX_IMAGE_BYTES", 64)
    resp = client.post(
        "/v1/messages",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(b"x" * 512).decode(),
                            },
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "error"
    assert "图片数据过大" in resp.json()["error"]["message"]


def test_gemini_endpoint_inline_data_over_limit_400(use_cfg, monkeypatch):
    """Gemini inline_data 图片超限必须 400,且错误来自字节上限检查。"""
    monkeypatch.setattr("deepsee_server.protocols.base.MAX_IMAGE_BYTES", 64)
    resp = client.post(
        "/v1beta/models/m:generateContent",
        json={
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(b"x" * 512).decode(),
                            }
                        }
                    ]
                }
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == 400
    assert "图片数据过大" in resp.json()["error"]["message"]


def test_chat_malformed_400_even_without_config(monkeypatch):
    """畸形请求的校验必须先于配置加载:配置缺失时仍返回 400,而非 500。"""
    from deepsee.errors import ConfigError

    def no_config():
        raise ConfigError("缺少 deepseek.api_key")

    monkeypatch.setattr("deepsee_server.app._current_config", no_config)
    resp = client.post("/v1/chat/completions", json={"messages": None})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_messages_malformed_400_even_without_config(monkeypatch):
    """Anthropic 畸形请求在配置缺失时仍返回协议形状 400,而非 500。"""
    from deepsee.errors import ConfigError

    def no_config():
        raise ConfigError("缺少 deepseek.api_key")

    monkeypatch.setattr("deepsee_server.app._current_config", no_config)
    resp = client.post("/v1/messages", json={"messages": None})
    assert resp.status_code == 400
    assert resp.json()["type"] == "error"


def test_gemini_malformed_400_even_without_config(monkeypatch):
    """Gemini 畸形请求在配置缺失时仍返回协议形状 400,而非 500。"""
    from deepsee.errors import ConfigError

    def no_config():
        raise ConfigError("缺少 deepseek.api_key")

    monkeypatch.setattr("deepsee_server.app._current_config", no_config)
    resp = client.post(
        "/v1beta/models/m:generateContent", json={"contents": None}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == 400
