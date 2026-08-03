"""DeepSee: pluggable vision layer for the DeepSeek API.

Quickstart::

    from deepsee import ask_with_image
    answer = ask_with_image("photo.jpg", "这张图里有什么?")
"""

from deepsee.backends import create_backend
from deepsee.backends.base import VisionBackend
from deepsee.composer.deepseek import ask, ask_with_image, describe_image
from deepsee.config.loader import Config, DeepSeekConfig, VisionConfig, load_config
from deepsee.errors import (
    ComposeError,
    ConfigError,
    DeepSeeError,
    ImageError,
    VisionBackendError,
)

__version__ = "0.1.0"

__all__ = [
    "ask",
    "ask_with_image",
    "describe_image",
    "create_backend",
    "VisionBackend",
    "load_config",
    "Config",
    "DeepSeekConfig",
    "VisionConfig",
    "DeepSeeError",
    "ConfigError",
    "ImageError",
    "VisionBackendError",
    "ComposeError",
    "__version__",
]