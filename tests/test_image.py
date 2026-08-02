import base64
import io

import httpx
import pytest
import respx
from PIL import Image

from deepsee.errors import ImageError
from deepsee.pipeline.image import load_image, normalize_image, prepare_image
from deepsee.pipeline.prompts import build_vision_prompt


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


def test_large_image_is_downscaled():
    img = Image.new("RGB", (3000, 1500), color=(10, 20, 30))
    media_type, b64 = normalize_image(img)
    assert media_type == "image/jpeg"
    decoded = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert decoded.size == (2048, 1024)  # long edge capped at 2048, aspect kept


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