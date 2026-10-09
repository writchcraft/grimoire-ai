from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GRIMOIRE_", extra="ignore")

    ai_provider: Literal["native", "ollama"] = "native"
    embedding_provider: Literal["native", "ollama"] = "native"
    native_model_path: Path | None = None
    native_embedding_model_path: Path | None = None
    native_context_size: int = Field(default=8192, ge=512, le=131072)
    native_embedding_context_size: int = Field(default=2048, ge=512, le=32768)
    native_gpu_layers: int = Field(default=0, ge=-1)
    native_threads: int | None = Field(default=None, ge=1)
    native_chat_format: str | None = None
    max_output_tokens: int = Field(default=2048, ge=1, le=8192)

    @field_validator(
        "native_model_path",
        "native_embedding_model_path",
        "native_threads",
        "native_chat_format",
        mode="before",
    )
    @classmethod
    def empty_optional(cls, value):
        return None if value == "" else value

    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b"
    embedding_model: str = "nomic-embed-text"
    data_dir: Path = Path("data/runtime")
    max_upload_bytes: int = Field(default=2_097_152, ge=1024, le=10_485_760)
    max_chunks: int = Field(default=512, ge=1, le=2048)
    retrieval_threshold: float = Field(default=0.45, ge=0, le=1)
    context_chars: int = Field(default=12_000, ge=2000, le=24_000)
    request_timeout: float = Field(default=120, ge=1, le=600)
    log_level: str = "INFO"
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]"]

    @field_validator("ollama_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        from urllib.parse import urlparse

        url = urlparse(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("Ollama URL must be an HTTP(S) endpoint without credentials")
        if url.query or url.fragment:
            raise ValueError("Ollama URL must not contain query parameters or fragments")
        return value.rstrip("/")
