"""Unified exception hierarchy for DeepSee.

Every error carries optional context: which backend, which model, and the
HTTP status code involved — so failures are locatable at a glance.
"""

from __future__ import annotations


class DeepSeeError(Exception):
    """Base class for all DeepSee errors."""

    def __init__(
        self,
        message: str,
        *,
        backend: str | None = None,
        model: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.backend = backend
        self.model = model
        self.status_code = status_code


class ConfigError(DeepSeeError):
    """Configuration is missing or invalid."""


class ImageError(DeepSeeError):
    """Image loading/processing failed (missing file, bad format, ...)."""


class VisionBackendError(DeepSeeError):
    """A vision backend (OpenAI-compatible / Anthropic / Gemini) failed."""


class ComposeError(DeepSeeError):
    """The DeepSeek composition step failed."""
