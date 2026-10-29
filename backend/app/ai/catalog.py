"""Validated generation inventory; embeddings intentionally have a separate identity."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RegistryLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently accepting the last value."""


def unique_mapping(loader, node):
    pairs = loader.construct_pairs(node, deep=True)
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate model registry configuration key")
        result[key] = value
    return result


RegistryLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)

TASKS = (
    "general",
    "rag",
    "logs",
    "incidents",
    "config_explanation",
    "config_reasoning",
    "code_reasoning",
    "summarisation",
)


class GenerationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    display_name: str = Field(min_length=1)
    provider: Literal["native", "ollama"] = "native"
    model_path: Path | None = None
    model_name: str | None = None
    capabilities: set[Literal[*TASKS]] = Field(min_length=1)
    context_size: int = Field(default=4096, ge=512, le=131072)
    output_limit: int = Field(default=512, ge=1, le=8192)
    enabled: bool = True
    resource_class: Literal["light", "medium", "heavy"] = "light"
    tags: list[str] = Field(default_factory=list)
    readiness: Literal["auto", "failed", "incompatible", "loading"] = "auto"

    @model_validator(mode="after")
    def valid_runtime(self):
        if self.provider == "native" and (not self.model_path or self.model_name):
            raise ValueError("Native models require model_path only")
        if self.provider == "ollama" and (not self.model_name or self.model_path):
            raise ValueError("Ollama models require model_name only")
        if self.output_limit >= self.context_size:
            raise ValueError("Output limit must be smaller than context")
        return self

    def availability(self):
        if not self.enabled:
            return "disabled"
        if self.readiness != "auto":
            return self.readiness
        if self.provider == "native":
            if not self.model_path.is_file():
                return "model_missing"
            try:
                with self.model_path.open("rb") as stream:
                    if self.model_path.suffix.lower() != ".gguf" or stream.read(4) != b"GGUF":
                        return "incompatible"
            except OSError:
                return "failed"
        return "unverified"


class RoutingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: str
    policies: dict[Literal[*TASKS], str] = Field(default_factory=dict)
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)


class ModelRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[GenerationModel] = Field(min_length=1)
    routing: RoutingPolicy

    @model_validator(mode="after")
    def valid_references(self):
        ids = [m.id for m in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate generation model IDs")
        references = [
            self.routing.default,
            *self.routing.policies.values(),
            *self.routing.fallbacks,
        ]
        references += [v for values in self.routing.fallbacks.values() for v in values]
        if any(ref not in ids for ref in references):
            raise ValueError("Routing references an unknown generation model")
        for task, model_id in self.routing.policies.items():
            if task not in self.get(model_id).capabilities:
                raise ValueError("Policy model lacks the task capability")
        return self

    def get(self, model_id):
        for model in self.models:
            if model.id == model_id:
                return model
        raise ValueError(f"Unknown generation model: {model_id}")

    def candidates(self, task, override="auto"):
        if task not in TASKS:
            raise ValueError("Unknown routing task")
        preferred = (
            override
            if override != "auto"
            else self.routing.policies.get(task, self.routing.default)
        )
        self.get(preferred)  # Unknown manual IDs must not silently fall back.
        # Only this explicit, bounded list is permitted; no recursive fallback graph.
        return list(
            dict.fromkeys(
                [preferred, *self.routing.fallbacks.get(preferred, []), self.routing.default]
            )
        )


def load_registry(settings):
    if settings.model_registry_path:
        path = settings.model_registry_path.resolve()
        try:
            document = yaml.load(path.read_text(encoding="utf-8"), Loader=RegistryLoader)
        except yaml.YAMLError as exc:
            raise ValueError("Invalid generation registry YAML/JSON") from exc
        registry = ModelRegistry.model_validate(document)
        for model in registry.models:
            if model.model_path and not model.model_path.is_absolute():
                model.model_path = path.parent / model.model_path
        return registry
    # Compatibility adapter for existing single-model environment configuration.
    native = settings.ai_provider == "native"
    model = GenerationModel(
        id="default",
        display_name=(
            settings.native_model_path.name
            if native and settings.native_model_path
            else "Unconfigured local model"
            if native
            else settings.model
        ),
        provider=settings.ai_provider,
        model_path=(settings.native_model_path or Path("unconfigured.gguf")) if native else None,
        model_name=None if native else settings.model,
        capabilities=set(TASKS),
        context_size=settings.native_context_size,
        output_limit=min(max(512, settings.max_output_tokens), settings.native_context_size - 1),
    )
    return ModelRegistry(models=[model], routing=RoutingPolicy(default="default"))
