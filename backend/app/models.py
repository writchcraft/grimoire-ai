from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, field_validator

from app.services.probe import check_url


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Message(InputModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class SearchHit(BaseModel):
    heading: str = ""
    lineage: str = ""
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
    evidence_type: Literal["indexed_documentation", "live_observation"] = "indexed_documentation"
    observation: dict | None = None


class ChatRequest(InputModel):
    debug: bool = False
    model: str = Field(default="auto", pattern=r"^(auto|[a-z][a-z0-9_-]{0,63})$")
    task: Literal[
        "general", "config_explanation", "config_reasoning", "code_reasoning", "summarisation"
    ] = "general"
    message: str = Field(min_length=1, max_length=8000)
    history: list[Message] = Field(default_factory=list, max_length=12)
    mode: Literal["knowledge", "general"] = "knowledge"
    stream: bool = False
    service: str | None = Field(default=None, max_length=100)
    probe: bool = False  # Allow a fresh read-only probe when the question asks about live state.
    explain: bool = False  # Add a separate, labelled model interpretation of live observations.


class LiveEvidence(BaseModel):
    """A traceable live observation; the live counterpart of a document Citation."""

    evidence_type: Literal[
        "http_observation", "dns_observation", "tls_observation", "windows_event"
    ] = "http_observation"
    windows_event: dict | None = None
    tls: dict | None = None
    hostname: str | None = None
    addresses: list[str] = Field(default_factory=list)
    record_types: list[str] = Field(default_factory=list)
    records: list[dict[str, str]] = Field(default_factory=list)
    canonical_name: str | None = None
    addresses_changed: bool | None = None
    label: str
    observation_id: str | None = None
    service: str
    environment: str = ""
    endpoint: str | None = None
    observed_at: str | None = None
    state: str
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    reason: str
    probed_now: bool = False
    health_endpoint: dict | None = None
    recent_observations: list[dict] = Field(default_factory=list)


class ChatResponse(BaseModel):
    routing: dict | None = None
    answer: str
    sources: list[Citation]
    live: list[LiveEvidence] = Field(default_factory=list)
    interpretation: str | None = None
    grounded: bool
    quality: str = "unvalidated"
    claims: list[dict[str, str]] = Field(default_factory=list)


class IngestRequest(InputModel):
    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2_097_152)
    source_path: str | None = Field(default=None, max_length=300)
    service: str | None = Field(default=None, max_length=100)


class LogRequest(InputModel):
    debug: bool = False
    model: str = Field(default="auto", pattern=r"^(auto|[a-z][a-z0-9_-]{0,63})$")
    content: str = Field(min_length=1, max_length=200_000)
    service: str = Field(default="unknown", min_length=1, max_length=100)
    question: str = Field(
        default="Explain the failures and suggest investigation steps.",
        min_length=1,
        max_length=2000,
    )
    use_model: bool = False


class EventReference(InputModel):
    channel: Literal["System", "Application", "Security"]
    record_id: int = Field(ge=1, le=2**63 - 1)
    evidence_id: str = Field(pattern=r"^WE-[a-f0-9]{64}$")


class IncidentRequest(LogRequest):
    include_host: bool = False
    event_selection: list[EventReference] = Field(default_factory=list, max_length=50)
    title: str = Field(default="Homelab incident", min_length=1, max_length=150)
    notes: str = Field(default="", max_length=8000)


class MonitorConfig(InputModel):
    """Read-only HTTP(S) monitoring target. Never carries credentials."""

    url: str
    health_url: str | None = None
    expected_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: float = Field(default=5.0, ge=0.1, le=30, allow_inf_nan=False)
    slow_ms: int = Field(default=2000, ge=50, le=60_000)
    environment: str = Field(default="", max_length=50)
    verify_tls: Literal[True] = True

    @field_validator("url", "health_url")
    @classmethod
    def valid_target(cls, value: str | None) -> str | None:
        return None if value is None else check_url(value)


class ServiceInput(InputModel):
    local_windows_service: str | None = Field(default=None, min_length=1, max_length=256)
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
    monitor: MonitorConfig | None = None

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
