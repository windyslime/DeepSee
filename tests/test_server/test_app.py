import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

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
    seen = {}

    async def fake_ask_with_image(image, question, **kw):
        seen["image"] = image
        seen["question"] = question
        return "图里是一只猫"

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
    assert resp.json()["choices"][0]["message"]["content"] == "图里是一只猫"
    assert isinstance(seen["image"], bytes)  # data URL 已解码为 bytes
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
    monkeypatch.setattr("deepsee_server.app.MAX_IMAGE_BYTES", 64)
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
    async def fake_ask_with_image(image, question, **kw):
        return "分析结果"

    monkeypatch.setattr(
        "deepsee_server.app.ask_with_image_async", fake_ask_with_image
    )
    resp = client.post(
        "/analyze", json={"image": _png_data_url(), "question": "这是什么?"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"kind": "auto", "text": "分析结果"}


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

    monkeypatch.setattr("deepsee_server.app.ask_with_image_async", boom)
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
