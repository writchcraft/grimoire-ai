from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, field_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Message(InputModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class SearchHit(BaseModel):
    heading: str = ""
    id: str
    document_id: str
    filename: str
    source_path: str
    document_type: str
    service: str | None
    chunk_number: int
    line_start: int
    line_end: int
    text: str
    score: float


class Citation(SearchHit):
    label: str


class ChatRequest(InputModel):
    message: str = Field(min_length=1, max_length=8000)
    history: list[Message] = Field(default_factory=list, max_length=12)
    mode: Literal["knowledge", "general"] = "knowledge"
    stream: bool = False
    service: str | None = Field(default=None, max_length=100)


class ChatResponse(BaseModel):
    answer: str
    sources: list[Citation]
    grounded: bool
    quality: str = "unvalidated"
    claims: list[dict[str, str]] = Field(default_factory=list)


class IngestRequest(InputModel):
    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2_097_152)
    source_path: str | None = Field(default=None, max_length=300)
    service: str | None = Field(default=None, max_length=100)


class LogRequest(InputModel):
    content: str = Field(min_length=1, max_length=200_000)
    service: str = Field(default="unknown", min_length=1, max_length=100)
    question: str = Field(
        default="Explain the failures and suggest investigation steps.",
        min_length=1,
        max_length=2000,
    )
    use_model: bool = False


class IncidentRequest(LogRequest):
    title: str = Field(default="Homelab incident", min_length=1, max_length=150)
    notes: str = Field(default="", max_length=8000)


class ServiceInput(InputModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[\w .-]+$")
    kind: Literal[
        "server", "vm", "container", "database", "proxy", "monitoring", "network", "other"
    ]
    hostname: str = Field(default="", max_length=253)
    ip_address: IPvAnyAddress | None = None
    ports: list[int] = Field(default_factory=list, max_length=32)
    urls: list[str] = Field(default_factory=list, max_length=16)
    dependencies: list[str] = Field(default_factory=list, max_length=32)
    operating_system: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("ports")
    @classmethod
    def valid_ports(cls, ports: list[int]) -> list[int]:
        if any(p < 1 or p > 65535 for p in ports):
            raise ValueError("Ports must be between 1 and 65535")
        return sorted(set(ports))

    @field_validator("urls")
    @classmethod
    def valid_urls(cls, urls: list[str]) -> list[str]:
        from urllib.parse import urlparse

        for value in urls:
            parsed = urlparse(value)
            if (
                len(value) > 2048
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("URLs must be HTTP(S) URLs without credentials")
        return urls

    @field_validator("tags", "dependencies")
    @classmethod
    def valid_labels(cls, labels: list[str]) -> list[str]:
        if any(not label.strip() or len(label) > 100 for label in labels):
            raise ValueError("Labels must contain 1 to 100 characters")
        return list(dict.fromkeys(label.strip() for label in labels))
