import base64
import io

import httpx
import pytest
import respx
from PIL import Image

from deepsee.errors import ImageError
from deepsee.pipeline.image import load_image, normalize_image, prepare_image
from deepsee.pipeline.prompts import (
    AUTO_ROUTE_PROMPT,
    UI_ANALYSIS_PROMPT,
    build_auto_route_prompt,
    build_ui_analysis_prompt,
    build_vision_prompt,
)


def test_load_from_bytes(sample_image_bytes):
    img = load_image(sample_image_bytes)
    assert img.size == (100, 80)
    assert img.format == "JPEG"


def test_load_from_path(tmp_path, sample_image_bytes):
    p = tmp_path / "pic.jpg"
    p.write_bytes(sample_image_bytes)
    assert load_image(p).size == (100, 80)


def test_load_from_pil(sample_image_bytes):
    pil = Image.open(io.BytesIO(sample_image_bytes))
    assert load_image(pil) is pil


def test_load_from_url(sample_image_bytes):
    with respx.mock:
        respx.get("https://example.com/pic.jpg").mock(
            return_value=httpx.Response(200, content=sample_image_bytes)
        )
        img = load_image("https://example.com/pic.jpg")
        assert img.size == (100, 80)


def test_load_url_failure_raises_image_error():
    with respx.mock:
        respx.get("https://example.com/missing.jpg").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(ImageError):
            load_image("https://example.com/missing.jpg")


def test_load_missing_file_raises():
    with pytest.raises(ImageError):
        load_image("/nonexistent/definitely-missing.jpg")


def test_unsupported_format_raises(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    with pytest.raises(ImageError, match="格式"):
        load_image(p)


def test_unsupported_format_bytes_raises():
    with pytest.raises(ImageError, match="格式"):
        load_image(b"not an image at all")


def test_large_image_keeps_true_size():
    img = Image.new("RGB", (3000, 1500), color=(10, 20, 30))
    media_type, b64 = normalize_image(img)
    assert media_type == "image/jpeg"
    decoded = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert decoded.size == (3000, 1500)  # true input size preserved


def test_over_protective_threshold_downscaled():
    img = Image.new("RGB", (9000, 5000), color=(10, 20, 30))
    media_type, b64 = normalize_image(img)
    assert media_type == "image/jpeg"
    decoded = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert decoded.size == (8192, 4551)  # long edge capped at 8192, aspect kept


def test_small_image_kept_as_is(sample_image_bytes):
    media_type, b64 = normalize_image(Image.open(io.BytesIO(sample_image_bytes)))
    assert media_type == "image/jpeg"
    decoded = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert decoded.size == (100, 80)


def test_rgba_alpha_flattened_to_jpeg(sample_png_bytes):
    media_type, b64 = normalize_image(Image.open(io.BytesIO(sample_png_bytes)))
    assert media_type == "image/jpeg"
    decoded = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert decoded.mode == "RGB"
    assert decoded.size == (60, 40)


def test_prepare_image_combines(sample_image_bytes):
    media_type, b64 = prepare_image(sample_image_bytes)
    assert media_type == "image/jpeg"
    assert b64


def test_build_vision_prompt_contains_question():
    prompt = build_vision_prompt("画面里有什么动物?")
    assert "画面里有什么动物?" in prompt
    assert "图片" in prompt


def test_build_auto_route_prompt_contains_question_and_json():
    prompt = build_auto_route_prompt("把按钮往右移")
    assert "把按钮往右移" in prompt
    assert "is_ui" in prompt
    assert "target_found" in prompt
    assert "rescreenshot_advice" in prompt


def test_build_ui_analysis_prompt_contains_question_and_schema():
    prompt = build_ui_analysis_prompt("按钮是什么颜色?")
    assert "按钮是什么颜色?" in prompt
    assert "elements" in prompt
    assert "layout" in prompt
    assert "target_found" in prompt


def test_ui_prompts_cover_key_experience_points():
    # 审核补齐的 6 个关键体验点必须出现在提示词里
    for prompt in (AUTO_ROUTE_PROMPT, UI_ANALYSIS_PROMPT):
        assert "局部区域" in prompt          # 1. 局部截图
        assert "模糊" in prompt              # 2. 截图质量
        assert "target_found" in prompt      # 3. 元素不存在
        assert "相似元素" in prompt          # 4. 多相似元素歧义
        assert ("未找到与问题相关的内容" in prompt
                or "确实不在截图内" in prompt)  # 5/3
    assert "布局" in UI_ANALYSIS_PROMPT      # 布局不限一句话
    assert "尽可能详细" in UI_ANALYSIS_PROMPT  # style 详细
