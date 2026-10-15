from unittest.mock import patch

import pytest

from app.core.errors import DocumentError
from app.ingestion.parser import Chunk
from app.services.storage import Storage


def test_partial_vector_write_rolls_back_sql_and_points(tmp_path):
    store = Storage(tmp_path, "test")
    original = store.vectors.upsert

    def partial_write_then_fail(name, points):
        original(name, points[:1])
        raise RuntimeError("Simulated vector write failure")

    try:
        with patch.object(store.vectors, "upsert", side_effect=partial_write_then_fail):
            with pytest.raises(RuntimeError, match="Simulated"):
                store.add_document(
                    {
                        "filename": "x.md",
                        "source_path": "x.md",
                        "document_type": "md",
                        "service": None,
                        "content_hash": "test",
                    },
                    [Chunk("first", 1, 1), Chunk("second", 2, 2)],
                    [[1.0, 0.0], [0.0, 1.0]],
                )
        assert store.documents() == []
        assert store.lexical_search("first") == []
        assert store.vectors.count("chunks").count == 0
    finally:
        store.close()


def test_embedding_dimension_change_is_rejected(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        store.ensure_collection(2)
        with pytest.raises(DocumentError, match="dimensions"):
            store.ensure_collection(3)
    finally:
        store.close()


def test_deleted_sql_never_returns_stale_vector_hits(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        doc = store.add_document(
            {
                "filename": "x.md",
                "source_path": "x.md",
                "document_type": "md",
                "service": None,
                "content_hash": "test",
            },
            [Chunk("secret", 1, 1)],
            [[1.0, 0.0]],
        )
        with patch.object(store.vectors, "delete", side_effect=RuntimeError("cleanup failed")):
            with pytest.raises(RuntimeError):
                store.delete_document(doc["id"])
        assert store.semantic_search([1, 0], 5, 0.1) == []
        assert store.lexical_search("secret") == []
    finally:
        store.close()
