import base64
import io
import ipaddress
import socket
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import httpx
import pytest
from PIL import Image

import deepsee.pipeline.image as im
from deepsee.errors import ImageError
from deepsee.pipeline.image import load_image, normalize_image, prepare_image
from deepsee.pipeline.prompts import (
    AUTO_ROUTE_PROMPT,
    UI_ANALYSIS_PROMPT,
    build_auto_route_prompt,
    build_ui_analysis_prompt,
    build_vision_prompt,
)


# --- 本地 HTTP 测试服务器 ---
# 下载走自定义固定 IP 传输层,respx 无法拦截;用真实本地服务器验证下载路径,
# SSRF 校验逻辑由单独测试覆盖(校验发生在连接之前)。

_JPEG_BYTES: bytes | None = None


def _jpeg_bytes() -> bytes:
    global _JPEG_BYTES
    if _JPEG_BYTES is None:
        buf = io.BytesIO()
        Image.new("RGB", (100, 80), color=(200, 30, 30)).save(buf, format="JPEG")
        _JPEG_BYTES = buf.getvalue()
    return _JPEG_BYTES


class _ImageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/pic.jpg":
            self._send(200, _jpeg_bytes())
        elif path == "/missing.jpg":
            self._send(404, b"not found")
        elif path == "/big.jpg":
            # 无 Content-Length(连接关闭界定),走流式累计路径
            self._send(200, b"x" * 5000, content_length=False)
        elif path == "/huge.jpg":
            # 声明的大 Content-Length 与真实 body 不符,走预判拒绝路径
            self._send(200, b"x" * 50, extra_headers={"Content-Length": "500"})
        elif path == "/gzip.jpg":
            self._send(200, b"x" * 10, extra_headers={"Content-Encoding": "gzip"})
        elif path == "/a.jpg":
            self._send(302, b"", extra_headers={"Location": "http://169.254.169.254/meta"})
        elif path == "/badloc.jpg":
            # 畸形重定向 Location(未闭合 IPv6 字面量),必须转 ImageError 而非裸异常
            self._send(302, b"", extra_headers={"Location": "http://[::1"})
        elif path == "/loop.jpg":
            self._send(302, b"", extra_headers={"Location": "/loop.jpg"})
        else:
            self._send(404, b"not found")

    def _send(self, code, body, content_length=True, extra_headers=None):
        self.send_response(code)
        extra = extra_headers or {}
        # extra_headers 里的 Content-Length 优先,避免重复头
        if content_length and "Content-Length" not in extra:
            self.send_header("Content-Length", str(len(body)))
        for k, v in extra.items():
            self.send_header(k, v)
        if not content_length:
            self.close_connection = True  # body 以连接关闭界定
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args):
        pass  # 静默


@pytest.fixture(scope="module")
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ImageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _allow_local_server(monkeypatch):
    """放行 127.0.0.1(本地测试服务器),其余主机走真实 SSRF 校验。

    固定 IP 传输层不再解析域名,只有 ``_resolve_public_ips`` 做解析;
    这里替换它:测试服务器地址直接放行,其余(如重定向目标 169.254.169.254)
    仍走真实校验。
    """
    real_resolve = im._resolve_public_ips

    def resolver(url):
        if httpx.URL(url).host == "127.0.0.1":
            return ["127.0.0.1"]
        return real_resolve(url)

    monkeypatch.setattr(im, "_resolve_public_ips", resolver)


def _fake_getaddrinfo(ip: str):
    """固定 DNS 解析结果,让 URL 测试不依赖真实网络。

    IP 字面量按自身返回(与真实 getaddrinfo 行为一致,重定向防护依赖这一点),
    域名一律返回给定的 ``ip``。
    """
    def resolver(host, port, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 0))]
        except ValueError:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    return resolver


# --- 基础加载 ---

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


# --- URL 下载(本地服务器) ---

def test_load_from_url(sample_image_bytes, monkeypatch, http_server):
    _allow_local_server(monkeypatch)
    img = load_image(f"{http_server}/pic.jpg")
    assert img.size == (100, 80)


def test_load_url_failure_raises_image_error(monkeypatch, http_server):
    _allow_local_server(monkeypatch)
    with pytest.raises(ImageError):
        load_image(f"{http_server}/missing.jpg")


def test_download_connects_to_pinned_ip(monkeypatch, http_server):
    """固定 IP 连接:host 是不存在的域名,解析函数固定返回 127.0.0.1。

    若传输层二次解析域名,``example.test`` 无法解析必然失败;成功即证明
    连接直接打到已校验的 IP,不存在 DNS rebinding 窗口。
    """
    monkeypatch.setattr(im, "_resolve_public_ips", lambda url: ["127.0.0.1"])
    img = load_image(f"http://example.test:{http_server.rsplit(':', 1)[1]}/pic.jpg")
    assert img.size == (100, 80)


def test_download_trust_env_disabled(monkeypatch, http_server):
    """httpx.Client 必须 trust_env=False,防止环境代理绕过本地 IP 校验。"""
    _allow_local_server(monkeypatch)
    captured = {}
    orig_client = httpx.Client

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(im.httpx, "Client", spy)
    load_image(f"{http_server}/pic.jpg")
    assert captured.get("trust_env") is False


def test_download_gzip_rejected(monkeypatch, http_server):
    """压缩响应必须拒绝:解压后的实际大小不受原始字节上限约束。"""
    _allow_local_server(monkeypatch)
    with pytest.raises(ImageError, match="压缩"):
        load_image(f"{http_server}/gzip.jpg")


def test_download_streaming_over_limit_rejected(monkeypatch, http_server):
    _allow_local_server(monkeypatch)
    monkeypatch.setattr(im, "MAX_IMAGE_BYTES", 100)
    with pytest.raises(ImageError, match="过大"):
        load_image(f"{http_server}/big.jpg")  # 无 Content-Length,流式累计


def test_download_content_length_precheck_rejected(monkeypatch, http_server):
    _allow_local_server(monkeypatch)
    monkeypatch.setattr(im, "MAX_IMAGE_BYTES", 100)
    with pytest.raises(ImageError, match="过大"):
        load_image(f"{http_server}/huge.jpg")


# --- SSRF 防护(真实校验,连接前拒绝) ---

def test_load_url_rejects_loopback_literal():
    with pytest.raises(ImageError, match="内网|保留"):
        load_image("http://127.0.0.1/secret.jpg")


def test_load_url_rejects_link_local_metadata():
    # 云元数据地址(169.254.169.254)必须被拒绝
    with pytest.raises(ImageError, match="内网|保留"):
        load_image("http://169.254.169.254/latest/meta-data/")


def test_load_url_rejects_host_resolving_to_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.1"))
    with pytest.raises(ImageError, match="内网"):
        load_image("https://internal.example.com/pic.jpg")


def test_load_url_rejects_ipv4_mapped_loopback(monkeypatch):
    # ::ffff:127.0.0.1 形式的 IPv4-mapped IPv6 必须解包后拒绝
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo("::ffff:127.0.0.1")
    )
    with pytest.raises(ImageError, match="内网|保留"):
        load_image("https://mapped.example.com/pic.jpg")


def test_load_url_rejects_nat64_prefix(monkeypatch):
    # RFC 6052 Local-Use 前缀 64:ff9b:1::/48 中的地址必须拒绝
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("64:ff9b:1::1"))
    with pytest.raises(ImageError, match="内网|保留"):
        load_image("https://nat64.example.com/pic.jpg")


def test_load_url_rejects_redirect_to_private(monkeypatch, http_server):
    _allow_local_server(monkeypatch)
    with pytest.raises(ImageError, match="内网|保留"):
        load_image(f"{http_server}/a.jpg")  # 302 → http://169.254.169.254/meta


def test_load_url_rejects_too_many_redirects(monkeypatch, http_server):
    _allow_local_server(monkeypatch)
    with pytest.raises(ImageError, match="重定向次数过多"):
        load_image(f"{http_server}/loop.jpg")


# --- 输入字节 / 解码像素上限 ---

def test_bytes_over_limit_rejected(monkeypatch):
    monkeypatch.setattr(im, "MAX_IMAGE_BYTES", 100)
    with pytest.raises(ImageError, match="过大"):
        load_image(b"x" * 500)


def test_decompression_bomb_error_mapped():
    """IHDR 声明 50000x50000 的最小 PNG:Pillow 在 open 阶段抛
    DecompressionBombError(不是 OSError),必须转换为项目 ImageError。"""
    bomb = _bomb_png()
    with pytest.raises(ImageError, match="无法解码"):
        load_image(bomb)


def test_pil_input_pixel_limit_enforced(monkeypatch):
    # PIL Image 输入同样受像素上限约束,不因输入形态绕过
    monkeypatch.setattr(im, "MAX_DECODE_PIXELS", 100)
    img = Image.new("RGB", (20, 20))  # 400 像素 > 100
    with pytest.raises(ImageError, match="尺寸过大"):
        load_image(img)


def test_decompression_bomb_rejected(sample_image_bytes, monkeypatch):
    # 100x80 = 8000 像素 > 上限 100,应在解码前拒绝
    monkeypatch.setattr(im, "MAX_DECODE_PIXELS", 100)
    with pytest.raises(ImageError, match="尺寸过大"):
        load_image(sample_image_bytes)


def test_file_url_scheme_rejected():
    with pytest.raises(ImageError, match="file://"):
        load_image("file:///etc/passwd")


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


def test_bmp_rejected_before_decode():
    # BMP 不支持,但必须是"格式"错误而不是解码错误(格式检查先于解码)
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="BMP")
    with pytest.raises(ImageError, match="格式"):
        load_image(buf.getvalue())


def _bomb_png() -> bytes:
    """IHDR 声明 50000x50000 的最小合法 PNG(约 66 字节)。"""
    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 50000, 50000, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


# --- normalize / prepare ---

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


# --- prompts ---

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


# --- 多 IP 故障回退 ---

def test_pinned_backend_fails_over_to_next_ip(monkeypatch):
    """一次 connect_tcp 内:首个 IP 连接失败后按剩余预算尝试下一个 IP。

    必须 mock 生产真实异常形态:httpcore 把底层 socket 异常映射为
    ``ConnectError``/``ConnectTimeout``(不是 OSError)。
    """
    attempts = []

    def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        attempts.append((host, timeout))
        if host == "1.2.3.4":
            raise httpcore.ConnectError("refused")
        return "sock-ok"

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake_connect)
    backend = im._PinnedIPBackend(["1.2.3.4", "5.6.7.8"])
    assert backend.connect_tcp("example.com", 443, timeout=10.0) == "sock-ok"
    assert [h for h, _ in attempts] == ["1.2.3.4", "5.6.7.8"]  # 两个 IP 都尝试过
    # 均分预算:2 个地址时第一个 IP 拿 timeout/2,第二个 IP 拿剩余全部
    assert attempts[0][1] == pytest.approx(5.0)
    assert attempts[1][1] == pytest.approx(10.0)


def test_pinned_backend_connect_timeout_fails_over(monkeypatch):
    """首个 IP 连接超时(ConnectTimeout)同样触发回退。"""
    attempts = []

    def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        attempts.append(host)
        raise httpcore.ConnectTimeout("timed out")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake_connect)
    backend = im._PinnedIPBackend(["1.2.3.4", "5.6.7.8"])
    with pytest.raises(httpcore.ConnectTimeout):
        backend.connect_tcp("example.com", 443, timeout=10.0)
    assert attempts == ["1.2.3.4", "5.6.7.8"]  # 超时后仍尝试第二个 IP


def test_pinned_backend_all_fail_raises_last_error(monkeypatch):
    def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        raise httpcore.ConnectError(f"refused: {host}")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake_connect)
    backend = im._PinnedIPBackend(["1.2.3.4", "9.9.9.9"])
    with pytest.raises(httpcore.ConnectError, match="9.9.9.9"):
        backend.connect_tcp("example.com", 443, timeout=10.0)


def test_pinned_backend_zero_timeout_raises_connect_timeout():
    # timeout=0/负数:预算耗尽必须抛 httpcore.ConnectTimeout(下载层可转换),
    # 而不是内建 TimeoutError(裸冒泡)
    backend = im._PinnedIPBackend(["1.2.3.4"])
    with pytest.raises(httpcore.ConnectTimeout):
        backend.connect_tcp("example.com", 443, timeout=0)


def test_download_fails_over_to_second_ip(monkeypatch, http_server):
    """真实集成:首个 IP(127.0.0.2)不可达,回退到第二个 IP(本地服务器)成功。

    覆盖 mock 无法验证的端到端路径:httpcore 异常映射 → 均分预算 → 回退。
    """
    monkeypatch.setattr(im, "_resolve_public_ips", lambda url: ["127.0.0.2", "127.0.0.1"])
    monkeypatch.setattr(im, "_HTTP_TIMEOUT", 3)  # 缩短预算,避免真实等待 30 秒
    img = load_image(f"{http_server}/pic.jpg")
    assert img.size == (100, 80)


# --- PIL 输入:格式与惰性解码 ---

def test_pil_bmp_rejected():
    # PIL 对象带不支持格式(BMP)时必须拒绝,不能因输入形态绕过格式检查
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="BMP")
    pil_bmp = Image.open(io.BytesIO(buf.getvalue()))
    with pytest.raises(ImageError, match="格式"):
        load_image(pil_bmp)


def test_pil_truncated_jpeg_load_error_mapped():
    # 头部完整、尾部截断的 JPEG:Image.open 成功(懒加载),load_image 必须
    # 触发完整解码并把 OSError 转为 ImageError,不能延迟到 prepare_image 冒裸异常
    buf = io.BytesIO()
    Image.new("RGB", (60, 40)).save(buf, format="JPEG")
    truncated = buf.getvalue()[:-40]
    pil_trunc = Image.open(io.BytesIO(truncated))
    assert pil_trunc.format == "JPEG"  # open 成功(懒加载)
    with pytest.raises(ImageError, match="无法解码"):
        load_image(pil_trunc)


def test_pil_memory_image_still_accepted():
    # 内存创建的图(format 为空)是合法输入,不应被格式检查误伤
    img = Image.new("RGB", (8, 8))
    assert load_image(img) is img


def test_pil_closed_image_load_error_mapped():
    # 已关闭的 PIL 图:load() 抛 ValueError("Operation on closed image"),
    # 必须转 ImageError 而非裸冒泡
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="JPEG")
    pil = Image.open(io.BytesIO(buf.getvalue()))
    pil.load()
    pil.close()
    with pytest.raises(ImageError, match="无法解码"):
        load_image(pil)


# --- InvalidURL 转换 ---

def test_invalid_url_raises_image_error():
    # 畸形 IPv6 字面量会抛 httpx.InvalidURL(不属于 HTTPError),必须转 ImageError
    with pytest.raises(ImageError, match="无效的图片 URL"):
        load_image("http://[::1/x")


def test_load_url_rejects_invalid_redirect_location(monkeypatch, http_server):
    # 恶意重定向 Location 畸形(http://[::1)必须转 ImageError,不能以裸异常冒出。
    # 实测 httpcore 在读取 302 响应阶段就验证 Location 头并抛
    # RemoteProtocolError(先于我们的 join() 防护),外层转换同样生效。
    _allow_local_server(monkeypatch)
    with pytest.raises(ImageError, match="图片下载失败"):
        load_image(f"{http_server}/badloc.jpg")
