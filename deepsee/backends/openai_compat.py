"""OpenAI-compatible chat completions vision backend.

Covers Qwen-VL (DashScope compatible mode), GPT-4o, GLM-4V, Moonshot and
any other service exposing ``POST /chat/completions`` with image_url
content blocks.
"""

from __future__ import annotations

import httpx

from deepsee.backends.base import VisionBackend, retry_request
from deepsee.errors import VisionBackendError
from deepsee.pipeline.image import ImageInput, prepare_image


class OpenAICompatibleBackend(VisionBackend):
    backend_name = "openai_compatible"

    def describe(self, image: ImageInput, prompt: str, **opts) -> str:
        media_type, b64 = prepare_image(image)
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload = {
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
        try:
            resp = retry_request(
                self._client,
                "POST",
                url,
                retries=self.retries,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
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
        except (KeyError, ValueError, TypeError) as exc:
            raise VisionBackendError(
                f"视觉后端 {self.backend_name} 响应解析失败",
                backend=self.backend_name,
                model=self.model,
            ) from exc