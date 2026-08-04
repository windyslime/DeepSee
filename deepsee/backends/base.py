"""VisionBackend abstraction and shared HTTP helpers."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

import httpx

from deepsee.pipeline.image import ImageInput

_RETRY_BACKOFF_BASE = 0.5  # seconds


class VisionBackend(ABC):
    """A vision model backend: turns (image, prompt) into text."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None,
        retries: int = 2,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.retries = retries
        # trust_env=False:不读环境代理。SOCKS 代理(如 ALL_PROXY=socks5://)在
        # 无 socksio 包时会直接 ImportError;且代理会把含 API key 的请求转发到
        # 第三方,与 image.py 下载路径(防代理绕过 SSRF 校验)保持一致。
        self._client = httpx.Client(timeout=60.0, trust_env=False)
        self._async_client: httpx.AsyncClient | None = None

    @property
    def async_client(self) -> httpx.AsyncClient:
        """Lazily-created async client (sync paths never allocate one)."""
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=60.0, trust_env=False)
        return self._async_client

    @abstractmethod
    def describe(self, image: ImageInput, prompt: str, **opts) -> str:
        """Describe the image given a prompt. Returns plain text."""

    def close(self) -> None:
        """Close the synchronous client.

        Async resources are released by ``aclose()``, which must run in the
        event loop that used them. The async client is lazily created, so
        pure sync paths never allocate it and leak nothing here.
        """
        self._client.close()

    async def aclose(self) -> None:
        """Close both sync and async clients (async-path teardown)."""
        self._client.close()
        if self._async_client is not None:
            await self._async_client.aclose()


def retry_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 2,
    **kwargs,
) -> httpx.Response:
    """Send a request, retrying 429 and 5xx with exponential backoff.

    Other errors (4xx, network errors) propagate as-is after one attempt.
    """
    for attempt in range(retries + 1):
        response = client.request(method, url, **kwargs)
        code = response.status_code
        if code == 429 or code >= 500:
            if attempt < retries:
                time.sleep(_RETRY_BACKOFF_BASE * (2**attempt))
                continue
        response.raise_for_status()
        return response
    raise AssertionError("unreachable")  # pragma: no cover


def stream_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 2,
    **kwargs,
) -> httpx.Response:
    """Send a streaming request, retrying 429/5xx before body consumption.

    ``client.send(req, stream=True)`` returns once response headers arrive;
    the body is read lazily by the caller via ``iter_lines()``/``iter_bytes()``,
    so the first yielded chunk does not wait for the full response.
    Failed responses are closed before retry/raise so connections are not
    leaked.
    """
    for attempt in range(retries + 1):
        req = client.build_request(method, url, **kwargs)
        resp = client.send(req, stream=True)
        code = resp.status_code
        if code == 429 or code >= 500:
            resp.close()
            if attempt < retries:
                time.sleep(_RETRY_BACKOFF_BASE * (2**attempt))
                continue
        if code >= 400:
            resp.close()
            resp.raise_for_status()  # HTTPStatusError,carries status_code
        return resp
    raise AssertionError("unreachable")  # pragma: no cover


async def retry_request_async(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retries: int = 2,
    **kwargs,
) -> httpx.Response:
    """Async equivalent of ``retry_request`` (asyncio.sleep backoff)."""
    for attempt in range(retries + 1):
        response = await client.request(method, url, **kwargs)
        code = response.status_code
        if code == 429 or code >= 500:
            if attempt < retries:
                await asyncio.sleep(_RETRY_BACKOFF_BASE * (2**attempt))
                continue
        response.raise_for_status()
        return response
    raise AssertionError("unreachable")  # pragma: no cover


async def stream_request_async(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retries: int = 2,
    **kwargs,
) -> httpx.Response:
    """Async equivalent of ``stream_request`` (lazy body via ``aiter_lines``)."""
    for attempt in range(retries + 1):
        req = client.build_request(method, url, **kwargs)
        resp = await client.send(req, stream=True)
        code = resp.status_code
        if code == 429 or code >= 500:
            await resp.aclose()
            if attempt < retries:
                await asyncio.sleep(_RETRY_BACKOFF_BASE * (2**attempt))
                continue
        if code >= 400:
            await resp.aclose()
            resp.raise_for_status()
        return resp
    raise AssertionError("unreachable")  # pragma: no cover