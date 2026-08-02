import pytest
from deepsee.errors import (
    ComposeError, ConfigError, DeepSeeError, ImageError, VisionBackendError,
)


def test_all_errors_subclass_deepsee_error():
    for cls in (ConfigError, ImageError, VisionBackendError, ComposeError):
        assert issubclass(cls, DeepSeeError)


def test_error_carries_context():
    err = VisionBackendError(
        "upstream failed",
        backend="gemini",
        model="gemini-2.0-flash",
        status_code=429,
    )
    assert err.backend == "gemini"
    assert err.model == "gemini-2.0-flash"
    assert err.status_code == 429
    assert "upstream failed" in str(err)


def test_config_error_without_context():
    err = ConfigError("missing api_key")
    assert err.backend is None
    assert err.status_code is None
