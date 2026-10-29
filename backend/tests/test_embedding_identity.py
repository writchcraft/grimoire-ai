import os

import pytest

from app.ingestion.parser import Chunk
from app.services.runtime import embedding_identity
from app.services.storage import Storage


def test_same_path_weights_invalidate_populated_index(settings, tmp_path):
    path = tmp_path / "embed.gguf"
    path.write_bytes(b"GGUF" + b"a" * 300000)
    configured = settings.model_copy(update={"native_embedding_model_path": path})
    first = embedding_identity(configured)
    assert embedding_identity(configured) == first
    store = Storage(tmp_path / "index", first)
    store.add_document(
        {
            "filename": "x.md",
            "source_path": "x.md",
            "document_type": "md",
            "service": None,
            "content_hash": "x",
        },
        [Chunk("Grafana", 1, 1)],
        [[1.0, 0.0]],
    )
    store.close()
    old = path.stat()
    # Same size, path and timestamp: sampled content still detects replacement.
    with path.open("r+b") as stream:
        stream.seek(150000)
        stream.write(b"b")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    changed = embedding_identity(configured)
    assert changed != first
    with pytest.raises(RuntimeError, match="identity differs"):
        Storage.inspect(tmp_path / "index", changed)
    with pytest.raises(RuntimeError, match="Embedding model changed"):
        Storage(tmp_path / "index", changed)


def test_metadata_detects_unsampled_change(settings, tmp_path):
    path = tmp_path / "embed.gguf"
    path.write_bytes(b"a" * 1024 * 1024)
    configured = settings.model_copy(update={"native_embedding_model_path": path})
    first = embedding_identity(configured)
    old = path.stat()
    with path.open("r+b") as stream:
        stream.seek(200000)
        stream.write(b"b")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000000))
    assert embedding_identity(configured) != first
    path.unlink()
    assert embedding_identity(configured).endswith(":unavailable")


def test_ollama_identity_stays_compatible(settings):
    configured = settings.model_copy(update={"embedding_provider": "ollama"})
    assert embedding_identity(configured) == configured.embedding_model
