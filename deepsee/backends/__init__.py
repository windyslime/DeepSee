"""Vision backend factory."""

from deepsee.backends.anthropic import AnthropicBackend
from deepsee.backends.base import VisionBackend, retry_request
from deepsee.backends.gemini import GeminiBackend
from deepsee.backends.openai_compat import OpenAICompatibleBackend
from deepsee.config.loader import VisionConfig
from deepsee.errors import ConfigError


def create_backend(config: VisionConfig, retries: int = 2) -> VisionBackend:
    """Create a vision backend from configuration."""
    kwargs = {
        "api_key": config.api_key,
        "model": config.model,
        "base_url": config.base_url,
        "retries": retries,
    }
    if config.backend == "openai_compatible":
        return OpenAICompatibleBackend(**kwargs)
    if config.backend == "anthropic":
        return AnthropicBackend(**kwargs)
    if config.backend == "gemini":
        return GeminiBackend(**kwargs)
    raise ConfigError(
        f"未知的视觉后端: {config.backend!r};"
        f"当前支持的: openai_compatible, anthropic, gemini"
    )


__all__ = ["VisionBackend", "retry_request", "create_backend"]