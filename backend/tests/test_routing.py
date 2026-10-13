import pytest

from app.ai.embeddings import OllamaEmbeddings
from app.ai.native import NativeEmbeddings, NativeProvider
from app.ai.ollama import OllamaProvider
from app.ai.registry import embedding_provider, generation_provider


@pytest.mark.parametrize(
    ("generation", "embedding", "chat_type", "embed_type"),
    [
        ("native", "ollama", NativeProvider, OllamaEmbeddings),
        ("ollama", "native", OllamaProvider, NativeEmbeddings),
    ],
)
async def test_mixed_provider_routing_is_independent(
    settings, generation, embedding, chat_type, embed_type
):
    settings = settings.model_copy(
        update={"ai_provider": generation, "embedding_provider": embedding}
    )
    chat = generation_provider(settings)
    vectors = embedding_provider(settings)
    try:
        assert isinstance(chat, chat_type)
        assert isinstance(vectors, embed_type)
    finally:
        await chat.close()
        await vectors.close()
