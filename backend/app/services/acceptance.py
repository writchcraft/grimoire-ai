"""Small synthetic acceptance check using production ingestion, storage and retrieval."""

import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.ingestion.pipeline import ingest
from app.models import ChatRequest
from app.rag.retrieval import prepare_chat
from app.services.runtime import embedding_identity
from app.services.storage import Storage


async def synthetic_acceptance(settings):
    class SyntheticEmbeddings:
        async def embed(self, texts):
            return [[1.0, 0.0] if "grafana" in t.lower() else [0.0, 1.0] for t in texts]

    def denied(*args, **kwargs):
        raise OSError("Network denied during synthetic acceptance")

    with TemporaryDirectory(prefix="grimoire-validate-") as directory:
        isolated = settings.model_copy(update={"data_dir": Path(directory)})
        with (
            patch.object(socket.socket, "connect", denied),
            patch.object(socket, "getaddrinfo", denied),
        ):
            store = Storage(isolated.data_dir, embedding_identity(isolated))
            try:
                embeddings = SyntheticEmbeddings()
                document = await ingest(
                    store,
                    embeddings,
                    isolated,
                    "example.md",
                    b"# Grafana\nGrafana uses port 3000.\n",
                )
                messages, sources = await prepare_chat(
                    ChatRequest(message="Grafana port?"), store, embeddings, isolated
                )
                if not messages or not sources or "3000" not in sources[0].text:
                    raise RuntimeError("Synthetic retrieval failed.")
                if not store.lexical_search("3000", 8):
                    raise RuntimeError("Lexical search failed.")
                _, absent = await prepare_chat(
                    ChatRequest(message="unrelated"), store, embeddings, isolated
                )
                if absent:
                    raise RuntimeError("Unrelated synthetic query returned evidence.")
                if document["chunk_count"] < 1:
                    raise RuntimeError("Ingestion produced no chunks.")
            finally:
                store.close()
            store = Storage(isolated.data_dir, embedding_identity(isolated))
            try:
                hits = store.semantic_search([1.0, 0.0], 6, isolated.retrieval_threshold, None)
                if not hits or "3000" not in hits[0].text:
                    raise RuntimeError("Persisted vector rebuild failed.")
            finally:
                store.close()
