"""OpenAI-compatible chat completions vision backend.

Covers Qwen-VL (DashScope compatible mode), GPT-4o, GLM-4V, Moonshot and
any other service exposing ``POST /chat/completions`` with image_url
content blocks.
"""

from __future__ import annotations

import asyncio

import httpx

from deepsee.backends.base import VisionBackend, retry_request, retry_request_async
from deepsee.errors import VisionBackendError
from deepsee.pipeline.image import ImageInput, prepare_image


class OpenAICompatibleBackend(VisionBackend):
    backend_name = "openai_compatible"

    def _build_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def _build_payload(self, media_type: str, b64: str, prompt: str) -> dict:
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }

    def _build_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

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
            return data["choices"][0]["message"]["content"]
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
            return data["choices"][0]["message"]["content"]
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