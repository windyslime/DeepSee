"""Structured input and output limits shared by gateway protocol adapters."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


def _count_text(value: Any) -> int:
    """递归累计任意透传结构中的字符串字符数。

    工具定义、历史工具调用参数等字段会原样发送到上游并参与 prompt,
    嵌套在字典/列表中的字符串也逐一计入,避免用深层结构绕过字符上限。
    """
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_count_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_count_text(item) for item in value)
    return 0


@dataclass(frozen=True)
class RequestLimits:
    max_messages: int
    max_images: int
    max_text_chars: int
    default_max_output_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        values = (
            self.max_messages,
            self.max_images,
            self.max_text_chars,
            self.default_max_output_tokens,
            self.max_output_tokens,
        )
        if any(isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("request limits must be positive integers")
        if self.default_max_output_tokens > self.max_output_tokens:
            raise ValueError("default output limit cannot exceed maximum")

    @classmethod
    def from_env(cls) -> "RequestLimits":
        def positive_int(name: str, default: int) -> int:
            raw = os.environ.get(name, str(default))
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} 必须是正整数,当前: {raw!r}") from exc
            if value <= 0:
                raise ValueError(f"{name} 必须是正整数,当前: {raw!r}")
            return value

        return cls(
            max_messages=positive_int("DeepSee_MAX_MESSAGES", 100),
            max_images=positive_int("DeepSee_MAX_IMAGES", 4),
            max_text_chars=positive_int("DeepSee_MAX_TEXT_CHARS", 200_000),
            default_max_output_tokens=positive_int(
                "DeepSee_DEFAULT_MAX_OUTPUT_TOKENS", 4096
            ),
            max_output_tokens=positive_int("DeepSee_MAX_OUTPUT_TOKENS", 8192),
        )

    def _output_tokens(self, value: object, field: str) -> int:
        if value is None:
            return self.default_max_output_tokens
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} 必须是正整数")
        if value > self.max_output_tokens:
            raise ValueError(
                f"输出 token 不能超过 {self.max_output_tokens}"
            )
        return value

    def _check_counts(self, *, items: int, images: int, text_chars: int, noun: str) -> None:
        if items > self.max_messages:
            raise ValueError(f"{noun}数量不能超过 {self.max_messages}")
        if images > self.max_images:
            raise ValueError(f"图片数量不能超过 {self.max_images}")
        if text_chars > self.max_text_chars:
            raise ValueError(f"文本内容不能超过 {self.max_text_chars} 个字符")

    def validate_openai(self, body: dict) -> int:
        messages = body.get("messages")
        items = len(messages) if isinstance(messages, list) else 0
        images = 0
        text_chars = 0
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                text_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_chars += len(block["text"])
                    elif block.get("type") == "image_url":
                        images += 1
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                # 历史 assistant 工具调用参数原样透传并参与上游 prompt
                text_chars += _count_text(tool_calls)
        # 工具定义(名称/描述/参数 schema)同样发送给上游并计入文本预算
        text_chars += _count_text(body.get("tools"))
        self._check_counts(
            items=items, images=images, text_chars=text_chars, noun="消息"
        )
        if "max_tokens" in body and "max_completion_tokens" in body:
            raise ValueError("max_tokens 与 max_completion_tokens 不能同时设置")
        field = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
        return self._output_tokens(body.get(field), field)

    def validate_anthropic(self, body: dict) -> int:
        messages = body.get("messages")
        items = len(messages) if isinstance(messages, list) else 0
        images = 0
        text_chars = 0
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                text_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_chars += len(block["text"])
                    elif block.get("type") == "image":
                        images += 1
            tool_uses = message.get("tool_use") or message.get("tool_result")
            if isinstance(tool_uses, (dict, list)):
                text_chars += _count_text(tool_uses)
        # system 提示(字符串或文本块数组)与工具定义都参与上游 prompt
        text_chars += _count_text(body.get("system"))
        text_chars += _count_text(body.get("tools"))
        self._check_counts(
            items=items, images=images, text_chars=text_chars, noun="消息"
        )
        return self._output_tokens(body.get("max_tokens"), "max_tokens")

    def validate_gemini(self, body: dict) -> int:
        contents = body.get("contents")
        items = len(contents) if isinstance(contents, list) else 0
        images = 0
        text_chars = 0
        for content in contents if isinstance(contents, list) else []:
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    text_chars += len(part["text"])
                if "inline_data" in part or "file_data" in part:
                    images += 1
                    continue
                # functionCall / functionResponse 参数原样透传并参与 prompt
                text_chars += _count_text(part)
        # systemInstruction 与工具声明同样发送给上游并计入文本预算
        text_chars += _count_text(body.get("systemInstruction"))
        text_chars += _count_text(body.get("tools"))
        self._check_counts(
            items=items, images=images, text_chars=text_chars, noun="内容"
        )
        config = body.get("generationConfig")
        if config is None:
            value = None
        elif isinstance(config, dict):
            value = config.get("maxOutputTokens")
        else:
            raise ValueError("generationConfig 必须是对象")
        return self._output_tokens(value, "maxOutputTokens")

    def validate_analyze(self, body: dict) -> None:
        question = body.get("question", "")
        if not isinstance(question, str):
            raise ValueError("question 必须是字符串")
        if len(question) > self.max_text_chars:
            raise ValueError(f"文本内容不能超过 {self.max_text_chars} 个字符")
