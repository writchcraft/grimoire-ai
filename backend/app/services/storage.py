import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from qdrant_client import QdrantClient, models

from app.core.errors import DocumentError
from app.ingestion.parser import Chunk
from app.models import SearchHit, ServiceInput


class Storage:
    """SQLite is authoritative. Rebuild the embedded vector index on process start."""

    def __init__(self, directory: Path, embedding_model: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "grimoire.sqlite3"
        self.embedding_model = embedding_model
        self.lock = threading.RLock()
        self.vectors = QdrantClient(":memory:")
        self.dimension: int | None = None
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, source_path TEXT NOT NULL,
                    document_type TEXT NOT NULL, service TEXT, ingested_at TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL, content_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id)
                    ON DELETE CASCADE, chunk_number INTEGER NOT NULL, line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL, text TEXT NOT NULL, vector TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search USING fts5(
                    id UNINDEXED, text, filename, source_path, service,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS services (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    data TEXT NOT NULL
                );
            """)
            model = db.execute("SELECT value FROM metadata WHERE key='embedding_model'").fetchone()
            count = db.execute("SELECT count(*) FROM documents").fetchone()[0]
            if model and model[0] != embedding_model and count:
                raise RuntimeError(
                    "Embedding model changed. Export/delete documents before changing it."
                )
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('embedding_model', ?)", (embedding_model,)
            )
            rows = db.execute("SELECT id, vector FROM chunks").fetchall()
            if rows:
                self.ensure_collection(len(json.loads(rows[0]["vector"])))
                for offset in range(0, len(rows), 128):
                    self.vectors.upsert(
                        "chunks",
                        [
                            models.PointStruct(id=row["id"], vector=json.loads(row["vector"]))
                            for row in rows[offset : offset + 128]
                        ],
                    )

    def connect(self) -> sqlite3.Connection:
        # Context-managed connections need closing as well as commit/rollback.
        class Connection(sqlite3.Connection):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    self.close()

        connection = sqlite3.connect(self.path, factory=Connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def close(self) -> None:
        self.vectors.close()

    def ensure_collection(self, dimension: int) -> None:
        if self.dimension is None:
            self.vectors.create_collection(
                "chunks",
                vectors_config=models.VectorParams(
                    size=dimension,
                    distance=models.Distance.COSINE,
                ),
            )
            self.dimension = dimension
        elif dimension != self.dimension:
            raise DocumentError(
                "Embedding dimensions changed; restore the original embedding model"
            )

    def document_by_hash(self, digest: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE content_hash=?", (digest,)).fetchone()
            return dict(row) if row else None

    def add_document(self, metadata: dict, chunks: list[Chunk], vectors: list[list[float]]) -> dict:
        with self.lock, self.connect() as db:
            # Recheck under the write lock: concurrent retries should be idempotent.
            existing = db.execute(
                "SELECT * FROM documents WHERE content_hash=?", (metadata["content_hash"],)
            ).fetchone()
            if existing:
                return dict(existing)
            self.ensure_collection(len(vectors[0]))
            document = {
                "id": str(uuid4()),
                **metadata,
                "ingested_at": datetime.now(UTC).isoformat(),
                "chunk_count": len(chunks),
            }
            db.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
                tuple(
                    document[key]
                    for key in (
                        "id",
                        "filename",
                        "source_path",
                        "document_type",
                        "service",
                        "ingested_at",
                        "chunk_count",
                        "content_hash",
                    )
                ),
            )
            points = []
            for number, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True), 1):
                chunk_id = str(uuid4())
                db.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                    (
                        chunk_id,
                        document["id"],
                        number,
                        chunk.line_start,
                        chunk.line_end,
                        chunk.text,
                        json.dumps(vector),
                    ),
                )
                db.execute(
                    "INSERT INTO chunk_search VALUES (?,?,?,?,?)",
                    (
                        chunk_id,
                        chunk.text,
                        document["filename"],
                        document["source_path"],
                        document["service"] or "",
                    ),
                )
                points.append(models.PointStruct(id=chunk_id, vector=vector))
            try:
                self.vectors.upsert("chunks", points)
                db.commit()
            except Exception:
                self.vectors.delete("chunks", [str(point.id) for point in points])
                raise
            return document

    def documents(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM documents ORDER BY ingested_at DESC")
            ]

    def delete_document(self, document_id: str) -> bool:
        with self.lock, self.connect() as db:
            rows = db.execute(
                "SELECT id FROM chunks WHERE document_id=?", (document_id,)
            ).fetchall()
            if not db.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone():
                return False
            for row in rows:
                db.execute("DELETE FROM chunk_search WHERE id=?", (row[0],))
            db.execute("DELETE FROM documents WHERE id=?", (document_id,))
            db.commit()
            # A stale vector cannot leak deleted text: results always join authoritative SQL.
            if rows:
                self.vectors.delete("chunks", [row[0] for row in rows])
            return True

    @staticmethod
    def hit(row: sqlite3.Row, score: float) -> SearchHit:
        return SearchHit(
            **{key: row[key] for key in SearchHit.model_fields if key != "score"}, score=score
        )

    def lexical_search(
        self,
        query: str,
        limit: int = 8,
        service: str | None = None,
        document_type: str | None = None,
        source: str | None = None,
    ) -> list[SearchHit]:
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)[:32]
        if not tokens:
            return []
        match = " OR ".join('"' + token + '"' for token in tokens)
        conditions, parameters = ["chunk_search MATCH ?"], [match]
        for column, value in (
            ("service", service),
            ("document_type", document_type),
            ("source_path", source),
        ):
            if value:
                conditions.append(f"d.{column}=?")
                parameters.append(value)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT c.*, d.filename, d.source_path, d.document_type, d.service,
                    bm25(chunk_search) AS rank
                FROM chunk_search JOIN chunks c ON c.id=chunk_search.id
                JOIN documents d ON d.id=c.document_id
                WHERE {" AND ".join(conditions)} ORDER BY rank LIMIT ?
            """,
                [*parameters, limit],
            ).fetchall()
            return [self.hit(row, -row["rank"]) for row in rows]

    def semantic_search(
        self, vector: list[float], limit: int, threshold: float, service: str | None = None
    ) -> list[SearchHit]:
        with self.lock, self.connect() as db:
            if self.dimension is None:
                return []
            self.ensure_collection(len(vector))
            # SQL service filtering happens before vector ranking via candidate IDs.
            query_filter = None
            if service:
                ids = [
                    row[0]
                    for row in db.execute(
                        "SELECT c.id FROM chunks c JOIN documents d ON c.document_id=d.id "
                        "WHERE d.service=?",
                        (service,),
                    )
                ]
                if not ids:
                    return []
                query_filter = models.Filter(must=[models.HasIdCondition(has_id=ids)])
            points = self.vectors.query_points(
                "chunks",
                query=vector,
                limit=limit,
                score_threshold=threshold,
                query_filter=query_filter,
            ).points
            result = []
            for point in points:
                row = db.execute(
                    "SELECT c.*, d.filename, d.source_path, d.document_type, d.service "
                    "FROM chunks c JOIN documents d ON c.document_id=d.id WHERE c.id=?",
                    (str(point.id),),
                ).fetchone()
                if row:
                    result.append(self.hit(row, point.score))
            return result

    def services(self, query: str = "") -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id, data FROM services ORDER BY name").fetchall()
            records = [{"id": row["id"], **json.loads(row["data"])} for row in rows]
            return [r for r in records if query.casefold() in json.dumps(r).casefold()]

    def save_service(self, service: ServiceInput) -> dict:
        with self.lock, self.connect() as db:
            service_id = str(uuid4())
            db.execute(
                "INSERT INTO services VALUES (?,?,?)",
                (service_id, service.name, service.model_dump_json()),
            )
            return {"id": service_id, **service.model_dump(mode="json")}

    def delete_service(self, service_id: str) -> bool:
        with self.lock, self.connect() as db:
            return db.execute("DELETE FROM services WHERE id=?", (service_id,)).rowcount > 0
