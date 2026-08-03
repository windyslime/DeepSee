"""VisionBackend abstraction and shared HTTP helpers."""

from __future__ import annotations

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
        self._client = httpx.Client(timeout=60.0)

    @abstractmethod
    def describe(self, image: ImageInput, prompt: str, **opts) -> str:
        """Describe the image given a prompt. Returns plain text."""

    def close(self) -> None:
        self._client.close()


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