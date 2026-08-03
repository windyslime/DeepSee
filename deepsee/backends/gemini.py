"""Google Gemini vision backend — native generateContent API."""

from __future__ import annotations

import httpx

from deepsee.backends.base import VisionBackend, retry_request
from deepsee.errors import VisionBackendError
from deepsee.pipeline.image import ImageInput, prepare_image


class GeminiBackend(VisionBackend):
    backend_name = "gemini"

    def describe(self, image: ImageInput, prompt: str, **opts) -> str:
        media_type, b64 = prepare_image(image)
        url = (
            f"{self.base_url.rstrip('/')}/v1beta/models/{self.model}:generateContent"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": media_type,
                                "data": b64,
                            }
                        },
                        {"text": prompt},
                    ]
                }
            ]
        }
        headers = {"x-goog-api-key": self.api_key}
        try:
            resp = retry_request(
                self._client, "POST", url, retries=self.retries, json=payload, headers=headers
            )
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
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