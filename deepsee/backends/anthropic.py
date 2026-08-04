"""Anthropic (Claude) vision backend — native messages API."""

from __future__ import annotations

import asyncio

import httpx

from deepsee.backends.base import VisionBackend, retry_request, retry_request_async
from deepsee.errors import VisionBackendError
from deepsee.pipeline.image import ImageInput, prepare_image

_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicBackend(VisionBackend):
    backend_name = "anthropic"

    def _build_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/messages"

    def _build_payload(self, media_type: str, b64: str, prompt: str) -> dict:
        return {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }

    def _build_headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }

    def describe(self, image: ImageInput, prompt: str, **opts) -> str:
        media_type, b64 = prepare_image(image)
        try:
            resp = retry_request(
                self._client,
                "POST",
                self._build_url(),
                retries=self.retries,
                json=self._build_payload(media_type, b64, prompt),
                headers=self._build_headers(),
            )
            data = resp.json()
            return data["content"][0]["text"]
        except httpx.HTTPStatusError as exc:
            raise VisionBackendError(
                f"视觉后端 {self.backend_name} 请求失败: HTTP {exc.response.status_code}",
                backend=self.backend_name,
                model=self.model,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionBackendError(
                f"视觉后端 {self.backend_name} 网络错误: {exc.__class__.__name__}",
                backend=self.backend_name,
                model=self.model,
            ) from exc
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise VisionBackendError(
                f"视觉后端 {self.backend_name} 响应解析失败",
                backend=self.backend_name,
                model=self.model,
            ) from exc

    async def describe_async(self, image: ImageInput, prompt: str, **opts) -> str:
        media_type, b64 = await asyncio.to_thread(prepare_image, image)
        try:
            resp = await retry_request_async(
                self.async_client,
                "POST",
                self._build_url(),
                retries=self.retries,
                json=self._build_payload(media_type, b64, prompt),
                headers=self._build_headers(),
            )
            data = resp.json()
            return data["content"][0]["text"]
        except httpx.HTTPStatusError as exc:
            raise VisionBackendError(
                f"视觉后端 {self.backend_name} 请求失败: HTTP {exc.response.status_code}",
                backend=self.backend_name,
                model=self.model,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionBackendError(
                f"视觉后端 {self.backend_name} 网络错误: {exc.__class__.__name__}",
                backend=self.backend_name,
                model=self.model,
            ) from exc
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise VisionBackendError(
                f"视觉后端 {self.backend_name} 响应解析失败",
                backend=self.backend_name,
                model=self.model,
            ) from exc
