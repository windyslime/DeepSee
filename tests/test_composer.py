import asyncio
import json

import httpx
import pytest
import respx

from deepsee.backends.base import VisionBackend
from deepsee.composer.deepseek import (
    ask,
    ask_async,
    ask_with_image,
    ask_with_image_async,
    describe_image,
    describe_image_async,
)
from deepsee.config.loader import Config, DeepSeekConfig, VisionConfig
from deepsee.errors import ComposeError, VisionBackendError

FAKE_DESCRIPTION = "图片里有一只白色的猫在窗台上。"

UI_JSON = {
    "is_ui": True,
    "reason": "网页界面截图",
    "analysis": {
        "ui_type": "web_page",
        "layout": "顶部导航栏;主内容区",
        "elements": [
            {
                "id": 1,
                "type": "button",
                "text": "提交",
                "location": "右上角,紧贴搜索框右侧",
                "size": "约 120x40px",
                "style": "蓝色背景(#2563eb),白色文字,圆角,有 hover 变深效果",
                "state": "normal",
            }
        ],
        "target_found": True,
        "rescreenshot_advice": "",
        "answer_to_user": "要移动的是元素 #1(提交按钮),位于右上角搜索框右侧",
    },
}

UI_JSON_TARGET_MISSING = {
    "is_ui": True,
    "reason": "网页界面截图",
    "analysis": {
        "ui_type": "web_page",
        "layout": "主内容区",
        "elements": [],
        "target_found": False,
        "rescreenshot_advice": "用户要求的'提交按钮'未出现在这张截图中,请重新截图包含该按钮",
        "answer_to_user": "",
    },
}


class FakeBackend(VisionBackend):
    """Records calls; returns a configurable reply."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []
        self.reply = FAKE_DESCRIPTION

    def describe(self, image, prompt, **opts):
        self.calls.append((image, prompt))
        return self.reply

    async def describe_async(self, image, prompt, **opts):
        self.calls.append((image, prompt))
        return self.reply


@pytest.fixture
def config():
    return Config(
        deepseek=DeepSeekConfig(
            api_key="sk-ds",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        ),
        vision=VisionConfig(
            backend="openai_compatible",
            api_key="sk-v",
            model="qwen-vl-max",
            base_url="https://vision.example.com/v1",
        ),
        retries=0,
    )


def _install_fake(monkeypatch, reply):
    fake = FakeBackend("k", "m", "https://x")
    fake.reply = reply
    monkeypatch.setattr(
        "deepsee.composer.deepseek.create_backend", lambda cfg, retries: fake
    )
    return fake


def test_describe_image_uses_backend(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(monkeypatch, FAKE_DESCRIPTION)
    result = describe_image(sample_image_bytes, "有什么?", config=config)
    assert result == FAKE_DESCRIPTION
    assert fake.calls[0][1] == "有什么?"


def test_ask_with_image_general_routes(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "reason": "自然照片", "analysis": FAKE_DESCRIPTION}),
    )
    with respx.mock:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "是一只白猫。"}}]}
            )
        )
        answer = ask_with_image(sample_image_bytes, "图里是什么动物?", config=config)
    assert answer == "是一只白猫。"
    assert "is_ui" in fake.calls[0][1]  # 自动路由提示词
    payload = route.calls[0].request.read()
    body = json.loads(payload)
    system = body["messages"][0]["content"]
    assert "多模态助手" in system
    assert FAKE_DESCRIPTION not in system  # 视觉输出不再进 system
    user = body["messages"][1]["content"]
    assert FAKE_DESCRIPTION in user
    assert "不可信数据" in user
    assert body["messages"][1]["role"] == "user"


def test_ask_with_image_ui_injects_element_map(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(monkeypatch, json.dumps(UI_JSON))
    with respx.mock:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "好的,已定位。"}}]}
            )
        )
        ask_with_image(sample_image_bytes, "把提交按钮往右移", config=config)
    body = json.loads(route.calls[0].request.read())
    user = body["messages"][1]["content"]
    assert "元素地图" in user
    assert "提交" in user
    assert "右上角,紧贴搜索框右侧" in user
    assert "蓝色背景(#2563eb)" in user


def test_ask_with_image_target_missing_advises_rescreenshot(
    config, sample_image_bytes, monkeypatch
):
    fake = _install_fake(monkeypatch, json.dumps(UI_JSON_TARGET_MISSING))
    with respx.mock:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "请重新截图。"}}]}
            )
        )
        ask_with_image(sample_image_bytes, "把提交按钮往右移", config=config)
    body = json.loads(route.calls[0].request.read())
    user = body["messages"][1]["content"]
    assert "未在截图中找到" in user
    assert "重新截图" in user


def test_ask_with_image_mode_ui_skips_classification(
    config, sample_image_bytes, monkeypatch
):
    fake = _install_fake(monkeypatch, json.dumps(UI_JSON["analysis"]))
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        )
        ask_with_image(sample_image_bytes, "改样式", config=config, mode="ui")
    assert "前端 UI 分析器" in fake.calls[0][1]
    assert "is_ui" not in fake.calls[0][1]


def test_ask_with_image_mode_general_skips_classification(
    config, sample_image_bytes, monkeypatch
):
    fake = _install_fake(monkeypatch, FAKE_DESCRIPTION)
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        )
        ask_with_image(sample_image_bytes, "描述一下", config=config, mode="general")
    assert "请仔细查看这张图片" in fake.calls[0][1]
    assert "is_ui" not in fake.calls[0][1]


def test_ask_with_image_parse_failure_falls_back_to_raw(
    config, sample_image_bytes, monkeypatch
):
    fake = _install_fake(monkeypatch, "不是 JSON 的输出内容")
    with respx.mock:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        )
        ask_with_image(sample_image_bytes, "q", config=config)
    body = json.loads(route.calls[0].request.read())
    user = body["messages"][1]["content"]
    assert "未经结构化校验" in user
    assert "不是 JSON 的输出内容" in user


def test_ask_with_image_streams(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )
    sse_body = (
        'data: {"choices": [{"delta": {"content": "是"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "白猫"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, content=sse_body.encode())
        )
        chunks = list(
            ask_with_image(sample_image_bytes, "是什么?", config=config, stream=True)
        )
    assert chunks == ["是", "白猫"]


def test_deepseek_500_maps_to_compose_error(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        with pytest.raises(ComposeError) as exc_info:
            ask_with_image(sample_image_bytes, "q", config=config)
    assert exc_info.value.status_code == 500
    assert exc_info.value.model == "deepseek-chat"


def test_vision_failure_propagates(config, sample_image_bytes, monkeypatch):
    def failing_backend(cfg, retries):
        class Boom(FakeBackend):
            def describe(self, image, prompt, **opts):
                raise VisionBackendError("vision down", backend="openai_compatible")

        return Boom("k", "m", "https://x")

    monkeypatch.setattr("deepsee.composer.deepseek.create_backend", failing_backend)
    with pytest.raises(VisionBackendError):
        ask_with_image(sample_image_bytes, "q", config=config)


def test_public_api_exports():
    import deepsee

    assert callable(deepsee.ask)
    assert callable(deepsee.ask_with_image)
    assert callable(deepsee.describe_image)
    assert callable(deepsee.create_backend)
    assert callable(deepsee.load_config)
    assert deepsee.__version__ == "0.1.0"


def test_ask_plain_text(config, monkeypatch):
    with respx.mock:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "你好!"}}]}
            )
        )
        answer = ask("你好", config=config)
    assert answer == "你好!"
    body = json.loads(route.calls[0].request.read())
    assert body["model"] == "deepseek-chat"
    assert body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": "你好"}]


def test_ask_streams(config, monkeypatch):
    sse_body = (
        'data: {"choices": [{"delta": {"content": "你"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "好"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, content=sse_body.encode())
        )
        chunks = list(ask("你好", config=config, stream=True))
    assert chunks == ["你", "好"]


def test_ask_500_maps_to_compose_error(config, monkeypatch):
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        with pytest.raises(ComposeError) as exc_info:
            ask("你好", config=config)
    assert exc_info.value.status_code == 500


def test_ask_with_image_invalid_mode_raises(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(monkeypatch, FAKE_DESCRIPTION)
    with pytest.raises(ValueError, match="非法 mode"):
        ask_with_image(sample_image_bytes, "q", config=config, mode="bogus")


def test_ask_with_image_network_error_wrapped(
    config, sample_image_bytes, monkeypatch
):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(ComposeError) as exc_info:
            ask_with_image(sample_image_bytes, "q", config=config)
    assert "网络错误" in str(exc_info.value)


def test_ask_with_image_empty_choices_wrapped(
    config, sample_image_bytes, monkeypatch
):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        with pytest.raises(ComposeError) as exc_info:
            ask_with_image(sample_image_bytes, "q", config=config)
    assert "响应解析失败" in str(exc_info.value)


def test_ask_with_image_ui_dirty_elements_no_typeerror(
    config, sample_image_bytes, monkeypatch
):
    fake = _install_fake(
        monkeypatch,
        json.dumps(
            {
                "is_ui": True,
                "analysis": {"ui_type": "web_page", "elements": 1, "target_found": "false"},
            }
        ),
    )
    with respx.mock:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        )
        ask_with_image(sample_image_bytes, "改样式", config=config)
    user = json.loads(route.calls[0].request.read())["messages"][1]["content"]
    assert "web_page" in user
    assert "元素地图" in user


def test_ask_with_image_streams_system_static(
    config, sample_image_bytes, monkeypatch
):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )
    sse_body = (
        'data: {"choices": [{"delta": {"content": "是"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "白猫"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    with respx.mock:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, content=sse_body.encode())
        )
        chunks = list(
            ask_with_image(sample_image_bytes, "是什么?", config=config, stream=True)
        )
    assert chunks == ["是", "白猫"]
    body = json.loads(route.calls[0].request.read())
    assert FAKE_DESCRIPTION not in body["messages"][0]["content"]
    assert FAKE_DESCRIPTION in body["messages"][1]["content"]


def test_ask_with_image_async_non_stream(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )

    async def _run():
        async with respx.mock:
            route = respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(
                    200, json={"choices": [{"message": {"content": "是一只白猫。"}}]}
                )
            )
            answer = await ask_with_image_async(
                sample_image_bytes, "图里是什么动物?", config=config
            )
            return route, answer

    route, answer = asyncio.run(_run())
    assert answer == "是一只白猫。"
    body = json.loads(route.calls[0].request.content)
    assert FAKE_DESCRIPTION not in body["messages"][0]["content"]
    assert FAKE_DESCRIPTION in body["messages"][1]["content"]


def test_ask_with_image_async_stream(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )
    sse_body = (
        'data: {"choices": [{"delta": {"content": "是"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "白猫"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    async def _run():
        async with respx.mock:
            route = respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(200, content=sse_body.encode())
            )
            chunks = []
            async for chunk in await ask_with_image_async(
                sample_image_bytes, "是什么?", config=config, stream=True
            ):
                chunks.append(chunk)
            return route, chunks

    route, chunks = asyncio.run(_run())
    assert chunks == ["是", "白猫"]
    assert json.loads(route.calls[0].request.content)["stream"] is True


def test_ask_async_plain(config, monkeypatch):
    async def _run():
        async with respx.mock:
            route = respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=httpx.Response(
                    200, json={"choices": [{"message": {"content": "你好!"}}]}
                )
            )
            answer = await ask_async("你好", config=config)
            return route, answer

    route, answer = asyncio.run(_run())
    assert answer == "你好!"
    body = json.loads(route.calls[0].request.content)
    assert body["messages"] == [{"role": "user", "content": "你好"}]


def test_describe_image_async_uses_backend(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(monkeypatch, FAKE_DESCRIPTION)
    result = asyncio.run(
        describe_image_async(sample_image_bytes, "有什么?", config=config)
    )
    assert result == FAKE_DESCRIPTION
    assert fake.calls[0][1] == "有什么?"


def test_ask_with_image_async_invalid_mode(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(monkeypatch, FAKE_DESCRIPTION)
    with pytest.raises(ValueError, match="非法 mode"):
        asyncio.run(
            ask_with_image_async(sample_image_bytes, "q", config=config, mode="bogus")
        )


def test_ask_with_image_async_network_error(config, sample_image_bytes, monkeypatch):
    fake = _install_fake(
        monkeypatch,
        json.dumps({"is_ui": False, "analysis": FAKE_DESCRIPTION}),
    )

    async def _run():
        async with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                side_effect=httpx.ConnectError("connection refused")
            )
            await ask_with_image_async(sample_image_bytes, "q", config=config)

    with pytest.raises(ComposeError) as exc_info:
        asyncio.run(_run())
    assert "网络错误" in str(exc_info.value)
