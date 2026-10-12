"""Embedding adapters are independent of text generation."""

import math

import httpx

from app.ai.base import ModelStatus
from app.ai.ollama import _OllamaConnection
from app.core.config import Settings
from app.core.errors import ProviderError


class OllamaEmbeddings(_OllamaConnection):
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(settings.model_copy(update={"model": settings.embedding_model}), transport)

    async def health(self) -> ModelStatus:
        status = await super().health()
        # Ollama chooses embedding context internally; do not advertise chat's setting.
        status.pop("context_size", None)
        return status

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        try:
            for start in range(0, len(texts), 32):
                batch = texts[start : start + 32]
                response = await self.client.post(
                    "api/embed",
                    json={
                        "model": self.settings.embedding_model,
                        "input": batch,
                        "truncate": False,
                    },
                )
                response.raise_for_status()
                vectors = response.json()["embeddings"]
                if len(vectors) != len(batch):
                    raise ValueError("Embedding count mismatch")
                for vector in vectors:
                    if (
                        not isinstance(vector, list)
                        or not vector
                        or any(
                            not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector
                        )
                        or not any(vector)
                    ):
                        raise ValueError("Invalid embedding")
                result.extend(vectors)
            if len({len(vector) for vector in result}) > 1:
                raise ValueError("Embedding dimension mismatch")
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ProviderError(
                "Ollama embeddings unavailable. Check the pulled embedding model."
            ) from exc
