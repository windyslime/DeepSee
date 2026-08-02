import io
import os

import pytest
from PIL import Image

# 测试环境可能携带 SOCKS/HTTP 代理,会让 httpx 初始化失败或走真实网络;
# 测试全部 mock 外部 API,不需要代理。
for _proxy_key in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    os.environ.pop(_proxy_key, None)


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
