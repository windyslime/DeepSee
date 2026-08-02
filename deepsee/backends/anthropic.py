"""Anthropic (Claude) vision backend — native messages API."""

from __future__ import annotations

import httpx

from deepsee.backends.base import VisionBackend, retry_request
from deepsee.errors import VisionBackendError
from deepsee.pipeline.image import ImageInput, prepare_image

_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicBackend(VisionBackend):
    backend_name = "anthropic"

    def describe(self, image: ImageInput, prompt: str, **opts) -> str:
        media_type, b64 = prepare_image(image)
        url = f"{self.base_url.rstrip('/')}/v1/messages"
        payload = {
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
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        try:
            resp = retry_request(
                self._client, "POST", url, retries=self.retries, json=payload, headers=headers
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
        except (KeyError, ValueError, TypeError) as exc:
            raise VisionBackendError(
                f"视觉后端 {self.backend_name} 响应解析失败",
                backend=self.backend_name,
                model=self.model,
            ) from exc