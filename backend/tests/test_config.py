import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/model",
        "http://user:password@localhost:11434",
        "http://localhost:11434?token=private",
        "http://localhost:11434#private",
    ],
)
def test_provider_endpoint_rejects_credentials_and_non_http_schemes(url):
    with pytest.raises(ValidationError):
        Settings(ollama_url=url)


@pytest.mark.parametrize("value", [0, 1023, 10_485_761])
def test_upload_limit_rejects_unsafe_configuration(value):
    with pytest.raises(ValidationError):
        Settings(max_upload_bytes=value)


def test_optional_native_environment_values_can_be_blank():
    settings = Settings(
        native_threads="",
        native_model_path="",
        native_embedding_model_path="",
        native_chat_format="",
    )
    assert settings.native_threads is None
    assert settings.native_model_path is None
    assert settings.native_embedding_model_path is None
    assert settings.native_chat_format is None
