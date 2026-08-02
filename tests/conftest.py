import io

import pytest
from PIL import Image


@pytest.fixture
def sample_image_bytes() -> bytes:
    """100x80 RGB JPEG."""
    buf = io.BytesIO()
    Image.new("RGB", (100, 80), color=(200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def sample_png_bytes() -> bytes:
    """60x40 RGBA PNG (used to test alpha-channel handling)."""
    buf = io.BytesIO()
    Image.new("RGBA", (60, 40), color=(0, 0, 255, 128)).save(buf, format="PNG")
    return buf.getvalue()
