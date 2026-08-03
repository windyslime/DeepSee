"""Image loading and preprocessing shared by all vision backends."""

from __future__ import annotations

import base64
import io
import ipaddress
import os
import socket
import time
from pathlib import Path
from typing import Iterable, Iterator, Union

import httpcore
import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from deepsee.errors import ImageError

ImageInput = Union[str, os.PathLike, bytes, "Image.Image"]

# 保护性阈值:仅当长边超过该值时等比缩放(规避各家 API 的硬限制)。
# 日常截图远低于此,保持真实输入尺寸以保留细节(用户可能只截单个按钮/栏目)。
PROTECTIVE_MAX_DIMENSION = 8192
SUPPORTED_FORMATS = ("JPEG", "PNG", "WEBP")
_HTTP_TIMEOUT = 30.0

# --- 服务化安全限制(所有入口共用,CLI 同样生效) ---
# 原始图片字节上限:URL 下载与 bytes 输入统一适用,防止大响应/超大输入耗尽内存。
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# 解码像素上限:超过即拒绝。防止"解压炸弹"(文件头声明超大尺寸,解码时才分配内存)。
# 4096x4096 ≈ 1670 万像素,一张 4K 截图完全够用;宽幅整页截图(如 8192x2048)也在内。
MAX_DECODE_PIXELS = 4096 * 4096
_MAX_REDIRECTS = 5
# RFC 6052 NAT64 前缀(Well-Known 64:ff9b::/96 与 Local-Use 64:ff9b:1::/48):
# 嵌入其中的 IPv4 可能指向内网,且 ``is_global`` 判定对这些前缀的覆盖随
# Python 版本而异,因此显式拒绝。部署网络若使用其他自定义 NAT64 前缀,
# 需要自行加入此列表(README 已记录此限制)。
_NAT64_NETWORKS = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断 IP 是否不可作为图片下载目标(私网/loopback/link-local/保留/特殊用途)。

    - IPv4-mapped IPv6(::ffff:a.b.c.d)解包后按 IPv4 判定,防止 ``::ffff:127.0.0.1``
      这类形式绕过检查;
    - NAT64 前缀中嵌入的 IPv4 同样不可信(见 ``_NAT64_NETWORKS``);
    - multicast 在 ``is_global`` 中为 True(实测 224.0.0.1 / ff02::1),必须显式排除。
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _NAT64_NETWORKS):
            return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


class _PinnedIPBackend(httpcore.SyncBackend):
    """把 TCP 连接目标固定为已校验的 IP,域名不再二次解析。

    - TLS 握手发生在 ``connect_tcp`` 之后且使用 URL 的原始 host,因此 SNI 与
      证书校验不受影响;被固定的只有 TCP 连接目标;
    - 故障回退:一次连接内按共享总超时依次尝试所有已验证 IP。真实
      ``SyncBackend.connect_tcp`` 会把底层 socket 异常映射为
      ``httpcore.ConnectError`` / ``ConnectTimeout``(二者都不是 OSError),
      这里捕获这两个类型;每次尝试按剩余地址数均分剩余预算,单个 IP 的
      耗时不会耗尽整个 deadline。
    """

    def __init__(self, ips: list[str]) -> None:
        self._ips = list(ips)

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable | None = None,
    ) -> httpcore.NetworkStream:
        deadline = None if timeout is None else time.monotonic() + timeout
        last_error: Exception | None = None
        total = len(self._ips)
        for idx, ip in enumerate(self._ips):
            if deadline is None:
                budget = None
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break  # 共享总超时预算耗尽
                budget = remaining / (total - idx)  # 均分:单个 IP 不独占预算
            try:
                return super().connect_tcp(ip, port, budget, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectTimeout(f"连接超时,已尝试 {len(self._ips)} 个地址")


class _ResponseStream(httpx.SyncByteStream):
    """把 httpcore 的迭代器流包装为 httpx 流。

    与 httpx 内部的 ``ResponseStream`` 等价,但避免依赖私有模块
    (``httpx._transports.default``),降低 httpx 升级的破坏风险;
    升级 httpx 后仍需回归验证。
    """

    def __init__(self, iterator: Iterable[bytes]) -> None:
        self._iterator = iterator

    def __iter__(self) -> Iterator[bytes]:
        for part in self._iterator:
            yield part

    def close(self) -> None:
        if hasattr(self._iterator, "close"):
            self._iterator.close()


class _PinnedIPTransport(httpx.BaseTransport):
    """固定 IP 的传输层:域名只解析一次(校验时),连接直接打到已校验的 IP。

    消除 DNS rebinding TOCTOU:httpx/httpcore 不再解析 host,攻击者事后把域名
    改指内网 IP 也不会影响本次连接。仅使用 httpx 公开 API
    (``BaseTransport`` / ``SyncByteStream`` / ``create_ssl_context``)与
    httpcore 1.x 公开接口。
    """

    def __init__(self, ips: list[str], *, verify: bool = True) -> None:
        self._ssl_context = httpx.create_ssl_context(verify=verify, trust_env=False)
        self._pool = httpcore.ConnectionPool(
            ssl_context=self._ssl_context,
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            network_backend=_PinnedIPBackend(ips),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        resp = self._pool.handle_request(req)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_ResponseStream(resp.stream),
            extensions=resp.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def _resolve_public_ips(url: str) -> list[str]:
    """解析 http(s) URL 的主机并校验所有解析结果均为公网地址(SSRF 防护)。

    返回校验通过的 IP 列表,供 ``_PinnedIPTransport`` 固定连接使用——域名
    只在这里解析一次,后续连接不再解析,消除 DNS rebinding TOCTOU。
    """
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        # InvalidURL 不属于 HTTPError,不转换会以裸异常冒泡(服务端 500)
        raise ImageError(f"无效的图片 URL: {url[:80]}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ImageError(f"仅支持 http/https 图片 URL: {url[:80]}")
    host = parsed.host
    if not host:
        raise ImageError(f"无效的图片 URL: {url[:80]}")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ImageError(f"图片 URL 域名无法解析: {host}") from exc
    if not infos:
        raise ImageError(f"图片 URL 域名无法解析: {host}")
    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        ip_str = info[4][0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        if _is_blocked_ip(ipaddress.ip_address(ip_str)):
            raise ImageError(f"图片 URL 指向内网/保留地址,已拒绝: {host}")
        ips.append(ip_str)
    return ips


def _too_large_message(source: str) -> str:
    return f"图片数据过大(超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB): {source}"


def _download_image(url: str) -> bytes:
    """带 SSRF 防护与字节上限的流式下载,手动逐跳处理重定向。

    - 每一跳先解析并校验主机(拒绝内网/保留地址),再用固定 IP 的传输层连接,
      消除 DNS rebinding TOCTOU;``trust_env=False`` 防止环境代理绕过校验;
    - 请求 ``Accept-Encoding: identity`` 并拒绝压缩响应,``iter_raw()`` 读取
      原始字节且每次扩容前检查上限(压缩流即使绕过编码检查也无法先分配大内存);
    - 不使用 ``follow_redirects``:httpx 自动跟随的重定向目标不受校验,
      这里每一跳都重新经过 ``_resolve_public_ips``。
    """
    try:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            ips = _resolve_public_ips(current)
            transport = _PinnedIPTransport(ips)
            with httpx.Client(
                follow_redirects=False,
                timeout=_HTTP_TIMEOUT,
                trust_env=False,
                transport=transport,
            ) as client:
                with client.stream(
                    "GET", current, headers={"Accept-Encoding": "identity"}
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise ImageError(f"重定向缺少 Location: {current}")
                        try:
                            current = str(httpx.URL(current).join(location))
                        except httpx.InvalidURL as exc:
                            raise ImageError(
                                f"无效的重定向目标: {location[:80]}"
                            ) from exc
                        continue
                    resp.raise_for_status()
                    encoding = (resp.headers.get("content-encoding") or "").strip().lower()
                    if encoding and encoding != "identity":
                        raise ImageError(
                            f"不支持的压缩响应(content-encoding: {encoding}): {current}"
                        )
                    length = resp.headers.get("content-length")
                    if length:
                        try:
                            if int(length) > MAX_IMAGE_BYTES:
                                raise ImageError(_too_large_message(current))
                        except ValueError:
                            pass  # 非法 Content-Length,按流式累计判断
                    data = bytearray()
                    for chunk in resp.iter_raw():
                        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                            raise ImageError(_too_large_message(current))
                        data.extend(chunk)
                    return bytes(data)
        raise ImageError(f"重定向次数过多: {url[:80]}")
    except (
        httpx.HTTPError,
        httpcore.NetworkError,
        httpcore.TimeoutException,
        httpcore.ProtocolError,
    ) as exc:
        raise ImageError(f"图片下载失败: {url[:80]} ({exc.__class__.__name__})") from exc


def _check_decode_limits(width: int, height: int) -> None:
    """解码前像素上限检查(文件头信息,不触发完整解码)。"""
    if width * height > MAX_DECODE_PIXELS:
        raise ImageError(
            f"图片尺寸过大({width}x{height}),超过 {MAX_DECODE_PIXELS // (1024 * 1024)} 兆像素限制"
        )


def load_image(image: ImageInput) -> Image.Image:
    """Load an image from a local path, http(s) URL, raw bytes, or PIL Image.

    - http(s) URL:受 SSRF 防护(拒绝内网/保留地址,固定 IP 连接)与字节上限约束;
    - 本地路径:CLI 用途保留(服务端入口不接受本地路径);
    - 解码前检查格式与像素上限,防止解压炸弹在 ``img.load()`` 时耗尽内存;
    - PIL Image 输入同样受像素上限约束(统一验证,不因输入形态绕过)。
    """
    if isinstance(image, Image.Image):
        _check_decode_limits(*image.size)
        # 内存创建的图(format 为空)可接受;有格式时必须受支持,
        # 避免 BMP 等非支持格式绕过统一格式检查。
        if image.format and image.format not in SUPPORTED_FORMATS:
            raise ImageError(
                f"不支持的图片格式: {image.format};仅支持 {', '.join(SUPPORTED_FORMATS)}"
            )
        try:
            # PIL 对象可能是惰性加载的(如截断文件),在此触发完整解码,
            # 避免错误延迟到 prepare_image 阶段以裸 OSError 冒出;
            # ValueError 覆盖已关闭图片(load() 抛 "Operation on closed image")。
            image.load()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ImageError("无法解码图片: 格式不受支持或文件损坏") from exc
        return image

    data: bytes | None = None
    if isinstance(image, bytes):
        data = image
    elif isinstance(image, (str, os.PathLike)):
        text = os.fspath(image)
        if text.startswith("http://") or text.startswith("https://"):
            data = _download_image(text)
        else:
            if text.startswith("file://"):
                raise ImageError("不支持 file:// 协议,请使用本地路径")
            path = Path(text)
            if not path.is_file():
                raise ImageError(f"图片文件不存在: {path}")
            data = path.read_bytes()
    else:
        raise ImageError(f"不支持的图片输入类型: {type(image).__name__}")

    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(_too_large_message("bytes"))

    try:
        img = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageError("无法解码图片: 格式不受支持或文件损坏") from exc

    # 解码前检查:格式与尺寸都来自文件头,不会触发完整解码(解压炸弹在此被拦截)。
    if img.format not in SUPPORTED_FORMATS:
        raise ImageError(
            f"不支持的图片格式: {img.format};仅支持 {', '.join(SUPPORTED_FORMATS)}"
        )
    _check_decode_limits(*img.size)

    try:
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageError("无法解码图片: 格式不受支持或文件损坏") from exc
    return img


def normalize_image(img: Image.Image) -> tuple[str, str]:
    """Normalize to RGB JPEG, preserving the true input size.

    Applies EXIF orientation and flattens transparency onto white before
    converting to RGB (so hidden colors under transparent pixels are not
    exposed). Only when the long edge exceeds ``PROTECTIVE_MAX_DIMENSION``
    (an API hard limit) is the image scaled down. Returns
    ``(media_type, base64_string)``.
    """
    img = ImageOps.exif_transpose(img.copy())
    if img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    width, height = img.size
    long_edge = max(width, height)
    if long_edge > PROTECTIVE_MAX_DIMENSION:
        scale = PROTECTIVE_MAX_DIMENSION / long_edge
        img = img.resize(
            (round(width * scale), round(height * scale)), Image.LANCZOS
        )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")


def prepare_image(image: ImageInput) -> tuple[str, str]:
    """Load and normalize an image in one call."""
    return normalize_image(load_image(image))
