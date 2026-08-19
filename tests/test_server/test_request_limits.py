import pytest

from deepsee_server.request_limits import RequestLimits


@pytest.fixture
def limits():
    return RequestLimits(
        max_messages=2,
        max_images=1,
        max_text_chars=5,
        default_max_output_tokens=3,
        max_output_tokens=4,
    )


def test_openai_limits_messages_images_text_and_output(limits):
    assert limits.validate_openai({"messages": []}) == 3
    assert limits.validate_openai({"messages": [], "max_tokens": 4}) == 4
    with pytest.raises(ValueError, match="消息数量"):
        limits.validate_openai({"messages": [{}, {}, {}]})
    with pytest.raises(ValueError, match="图片数量"):
        limits.validate_openai({
            "messages": [{"content": [
                {"type": "image_url", "image_url": {"url": "https://a"}},
                {"type": "image_url", "image_url": {"url": "https://b"}},
            ]}]
        })
    with pytest.raises(ValueError, match="文本内容"):
        limits.validate_openai({"messages": [{"content": "123456"}]})
    with pytest.raises(ValueError, match="max_tokens"):
        limits.validate_openai({"messages": [], "max_tokens": "4"})
    with pytest.raises(ValueError, match="输出 token"):
        limits.validate_openai({"messages": [], "max_tokens": 5})


def test_anthropic_and_gemini_limits(limits):
    assert limits.validate_anthropic({"messages": [], "max_tokens": 4}) == 4
    with pytest.raises(ValueError, match="图片数量"):
        limits.validate_anthropic({
            "messages": [{"content": [
                {"type": "image", "source": {}},
                {"type": "image", "source": {}},
            ]}]
        })
    with pytest.raises(ValueError, match="文本内容"):
        limits.validate_anthropic({
            "messages": [{"content": [{"type": "text", "text": "123456"}]}]
        })

    assert limits.validate_gemini({"contents": []}) == 3
    assert limits.validate_gemini({
        "contents": [], "generationConfig": {"maxOutputTokens": 4}
    }) == 4
    with pytest.raises(ValueError, match="内容数量"):
        limits.validate_gemini({"contents": [{}, {}, {}]})
    with pytest.raises(ValueError, match="maxOutputTokens"):
        limits.validate_gemini({
            "contents": [], "generationConfig": {"maxOutputTokens": True}
        })


def test_invalid_limit_configuration_is_rejected():
    with pytest.raises(ValueError):
        RequestLimits(
            max_messages=0,
            max_images=1,
            max_text_chars=1,
            default_max_output_tokens=1,
            max_output_tokens=1,
        )


def test_limits_load_from_environment(monkeypatch):
    monkeypatch.setenv("DeepSee_MAX_MESSAGES", "7")
    monkeypatch.setenv("DeepSee_MAX_IMAGES", "2")
    monkeypatch.setenv("DeepSee_MAX_TEXT_CHARS", "1234")
    monkeypatch.setenv("DeepSee_DEFAULT_MAX_OUTPUT_TOKENS", "256")
    monkeypatch.setenv("DeepSee_MAX_OUTPUT_TOKENS", "512")
    assert RequestLimits.from_env() == RequestLimits(7, 2, 1234, 256, 512)


def test_analyze_rejects_non_string_question(limits):
    with pytest.raises(ValueError, match="question"):
        limits.validate_analyze({"question": 123})
    with pytest.raises(ValueError):
        RequestLimits(
            max_messages=1,
            max_images=1,
            max_text_chars=1,
            default_max_output_tokens=2,
            max_output_tokens=1,
        )


def test_openai_tools_and_tool_calls_count_toward_text_limit(limits):
    """工具描述与历史工具调用参数必须计入文本预算,不允许绕过。"""
    big_description = "x" * 6
    with pytest.raises(ValueError, match="文本内容"):
        limits.validate_openai({
            "messages": [],
            "tools": [{
                "type": "function",
                "function": {"name": "lookup", "description": big_description},
            }],
        })
    with pytest.raises(ValueError, match="文本内容"):
        limits.validate_openai({
            "messages": [{
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"123456"}'},
                }],
            }],
        })
    # 正常大小的工具定义与参数仍通过
    relaxed = RequestLimits(
        max_messages=10, max_images=4, max_text_chars=500,
        default_max_output_tokens=3, max_output_tokens=4,
    )
    assert relaxed.validate_openai({
        "messages": [{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        }],
        "tools": [{
            "type": "function",
            "function": {"name": "lookup", "description": "short"},
        }],
    }) == 3


def test_anthropic_system_and_tools_count_toward_text_limit(limits):
    with pytest.raises(ValueError, match="文本内容"):
        limits.validate_anthropic({
            "messages": [],
            "system": "x" * 6,
            "max_tokens": 4,
        })
    with pytest.raises(ValueError, match="文本内容"):
        limits.validate_anthropic({
            "messages": [],
            "tools": [{
                "name": "lookup",
                "description": "x" * 6,
                "input_schema": {"type": "object"},
            }],
            "max_tokens": 4,
        })
    relaxed = RequestLimits(
        max_messages=10, max_images=4, max_text_chars=500,
        default_max_output_tokens=3, max_output_tokens=4,
    )
    assert relaxed.validate_anthropic({
        "messages": [],
        "system": [{"type": "text", "text": "hi"}],
        "tools": [{"name": "lookup", "description": "short"}],
        "max_tokens": 4,
    }) == 4


def test_gemini_tools_and_function_calls_count_toward_text_limit(limits):
    with pytest.raises(ValueError, match="文本内容"):
        limits.validate_gemini({
            "contents": [],
            "tools": [{
                "functionDeclarations": [{
                    "name": "lookup",
                    "description": "x" * 6,
                }],
            }],
        })
    with pytest.raises(ValueError, match="文本内容"):
        limits.validate_gemini({
            "contents": [{
                "parts": [{"functionCall": {"name": "lookup", "args": {"q": "x" * 6}}}],
            }],
        })
    with pytest.raises(ValueError, match="文本内容"):
        limits.validate_gemini({
            "contents": [],
            "systemInstruction": {"parts": [{"text": "x" * 6}]},
        })
    relaxed = RequestLimits(
        max_messages=10, max_images=4, max_text_chars=500,
        default_max_output_tokens=3, max_output_tokens=4,
    )
    assert relaxed.validate_gemini({
        "contents": [{"parts": [{"functionCall": {"name": "lookup", "args": {}}}]}],
        "tools": [{"functionDeclarations": [{"name": "lookup", "description": "short"}]}],
    }) == 3
