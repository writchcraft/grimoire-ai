from app.ai.base import EmbeddingProvider, ModelProvider
from app.ai.embeddings import OllamaEmbeddings
from app.ai.native import NativeEmbeddings, NativeProvider
from app.ai.ollama import OllamaProvider
from app.core.config import Settings


def generation_provider(settings: Settings) -> ModelProvider:
    if settings.ai_provider == "native":
        return NativeProvider(settings)
    if settings.ai_provider == "ollama":
        return OllamaProvider(settings)
    raise ValueError("Unsupported AI provider")


def embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "native":
        return NativeEmbeddings(settings)
    if settings.embedding_provider == "ollama":
        return OllamaEmbeddings(settings)
    raise ValueError("Unsupported embedding provider")
