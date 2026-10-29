import json
import math
import re
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from qdrant_client import QdrantClient, models

from app.core.errors import DocumentError
from app.ingestion.parser import Chunk
from app.models import SearchHit, ServiceInput


class Storage:
    """SQLite is authoritative. Rebuild the embedded vector index on process start."""

    SCHEMA_VERSION = 1
    OBSERVATIONS_PER_SERVICE = 50  # Bounded health history; oldest observations are pruned.

    @classmethod
    def check_schema(cls, db):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, cls.SCHEMA_VERSION}:
            raise RuntimeError(f"Unsupported storage schema {version}; use a compatible Grimoire.")

    @staticmethod
    def check_vectors(rows):
        dimension = None
        for row in rows:
            try:
                vector = json.loads(row["vector"])
                if (
                    not isinstance(vector, list)
                    or not vector
                    or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)
                ):
                    raise ValueError("Invalid vector")
                if dimension is not None and len(vector) != dimension:
                    raise ValueError("Mixed dimensions")
                dimension = len(vector)
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    "Persisted vectors are incompatible; restore or re-ingest."
                ) from exc
        return dimension

    @staticmethod
    def inspect(directory: Path, embedding_model: str) -> dict:
        """Inspect persisted state without opening the mutating application lifecycle."""
        path = directory / "grimoire.sqlite3"
        if not path.exists():
            return {
                "exists": False,
                "detail": "Database not initialized; storage checks unverified.",
            }
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            try:
                Storage.check_schema(db)
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("SQLite integrity check failed.")
                count = db.execute("SELECT count(*) FROM documents").fetchone()[0]
                model = db.execute(
                    "SELECT value FROM metadata WHERE key='embedding_model'"
                ).fetchone()
                if count and (not model or model[0] != embedding_model):
                    raise RuntimeError(
                        "Stored embedding identity differs; follow reindexing guidance."
                    )
                return {
                    "exists": True,
                    "documents": count,
                    "detail": "SQLite readable, integrity and embedding identity checked; "
                    "vector rebuild unverified (run validate).",
                }
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise RuntimeError("SQLite unavailable; check schema, locks and permissions.") from exc

    def __init__(self, directory: Path, embedding_model: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "grimoire.sqlite3"
        self.embedding_model = embedding_model
        self.lock = threading.RLock()
        self.vectors = None
        self.dimension: int | None = None
        with self.connect() as db:
            self.check_schema(db)
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
                CREATE TABLE IF NOT EXISTS originals (
                    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                    content BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_observations (
                    id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id)
                    ON DELETE CASCADE, checked_at TEXT NOT NULL,
                    record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dns_observations (
                    id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id)
                    ON DELETE CASCADE, checked_at TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS dns_recent
                    ON dns_observations (service_id, checked_at DESC);
                CREATE TABLE IF NOT EXISTS tls_observations (
                    id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id)
                    ON DELETE CASCADE, checked_at TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tls_recent
                    ON tls_observations (service_id, checked_at DESC);
                CREATE INDEX IF NOT EXISTS observation_recent
                    ON service_observations (service_id, checked_at DESC);
            """)
            if "heading" not in {row[1] for row in db.execute("PRAGMA table_info(chunks)")}:
                db.execute("ALTER TABLE chunks ADD COLUMN heading TEXT NOT NULL DEFAULT ''")
            if "lineage" not in {row[1] for row in db.execute("PRAGMA table_info(chunks)")}:
                db.execute("ALTER TABLE chunks ADD COLUMN lineage TEXT NOT NULL DEFAULT ''")
            model = db.execute("SELECT value FROM metadata WHERE key='embedding_model'").fetchone()
            count = db.execute("SELECT count(*) FROM documents").fetchone()[0]
            if count and (not model or model[0] != embedding_model):
                raise RuntimeError(
                    "Embedding model changed. Export/delete documents before changing it."
                )
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('embedding_model', ?)", (embedding_model,)
            )
            rows = db.execute("SELECT id, vector FROM chunks").fetchall()
            dimension = self.check_vectors(rows)
            db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            self.vectors = QdrantClient(":memory:")
            if rows:
                self.ensure_collection(dimension)
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
        if self.vectors is not None:
            self.vectors.close()
            self.vectors = None

    def readiness(self) -> dict:
        with closing(
            sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        ) as db:
            db.execute("SELECT 1 FROM metadata LIMIT 1").fetchall()
        return {"state": "ready", "schema_version": self.SCHEMA_VERSION}

    def save_incident(self, record: dict) -> dict:
        result = {"id": str(uuid4()), "created_at": datetime.now(UTC).isoformat(), **record}
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO incidents VALUES (?,?,?)",
                (result["id"], result["created_at"], json.dumps(result)),
            )
        return result

    def incidents(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            ]

    def delete_incident(self, incident_id: str) -> bool:
        with self.lock, self.connect() as db:
            return db.execute("DELETE FROM incidents WHERE id=?", (incident_id,)).rowcount > 0

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

    def add_document(
        self,
        metadata: dict,
        chunks: list[Chunk],
        vectors: list[list[float]],
        original: bytes | None = None,
    ) -> dict:
        with self.lock, self.connect() as db:
            # Recheck under the write lock: concurrent retries should be idempotent.
            existing = db.execute(
                "SELECT * FROM documents WHERE content_hash=?", (metadata["content_hash"],)
            ).fetchone()
            if existing:
                return dict(existing)
            if not chunks or len(chunks) != len(vectors):
                raise DocumentError("Each chunk requires one embedding vector")
            dimension = self.check_vectors([{"vector": json.dumps(v)} for v in vectors])
            self.ensure_collection(dimension)
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
            if original is not None:
                db.execute("INSERT INTO originals VALUES (?,?)", (document["id"], original))
            for number, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True), 1):
                chunk_id = str(uuid4())
                db.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        chunk_id,
                        document["id"],
                        number,
                        chunk.line_start,
                        chunk.line_end,
                        chunk.text,
                        json.dumps(vector),
                        chunk.heading,
                        chunk.lineage,
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

    def section_neighbors(self, hit: SearchHit) -> list[SearchHit]:
        """Only expand Markdown continuation chunks within the same heading section."""
        if hit.document_type != "md":
            return []
        with self.connect() as db:
            rows = db.execute(
                "SELECT c.*, d.filename, d.source_path, d.document_type, d.service "
                "FROM chunks c JOIN documents d ON c.document_id=d.id "
                "WHERE c.document_id=? AND c.chunk_number BETWEEN ? AND ? "
                "ORDER BY c.chunk_number",
                (hit.document_id, hit.chunk_number - 1, hit.chunk_number + 1),
            ).fetchall()
        result = []
        for row in rows:
            sibling = self.hit(row, hit.score * 0.9)
            if sibling.id == hit.id or len(sibling.text) + len(hit.text) > 4000:
                continue
            if hit.heading and sibling.heading != hit.heading:
                continue
            if sibling.chunk_number < hit.chunk_number:
                if hit.text.lstrip().startswith("#"):
                    continue
            elif sibling.text.lstrip().startswith("#"):
                continue
            result.append(sibling)
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

    def service(self, reference: str) -> dict | None:
        """Find a service by id or, failing that, by case-insensitive name."""
        for record in self.services():
            if reference == record["id"]:
                return record
        return next(
            (r for r in self.services() if r["name"].casefold() == reference.casefold()), None
        )

    def delete_service(self, service_id: str) -> bool:
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM service_observations WHERE service_id=?", (service_id,))
            db.execute("DELETE FROM dns_observations WHERE service_id=?", (service_id,))
            db.execute("DELETE FROM tls_observations WHERE service_id=?", (service_id,))
            return db.execute("DELETE FROM services WHERE id=?", (service_id,)).rowcount > 0

    def record_dns(self, observation: dict) -> dict:
        """Persist one observation and prune this service to the newest few."""
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO dns_observations VALUES (?,?,?,?)",
                (
                    observation["id"],
                    observation["service_id"],
                    observation["checked_at"],
                    json.dumps(observation),
                ),
            )
            db.execute(
                "DELETE FROM dns_observations WHERE service_id=? AND id NOT IN ("
                "SELECT id FROM dns_observations WHERE service_id=? "
                "ORDER BY checked_at DESC, rowid DESC LIMIT ?)",
                (
                    observation["service_id"],
                    observation["service_id"],
                    self.OBSERVATIONS_PER_SERVICE,
                ),
            )
            db.execute(
                "DELETE FROM dns_observations WHERE rowid NOT IN "
                "(SELECT rowid FROM dns_observations ORDER BY rowid DESC LIMIT 5000)"
            )
        return observation

    def dns_observations(self, service_id: str, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM dns_observations WHERE service_id=? "
                    "ORDER BY checked_at DESC, rowid DESC LIMIT ?",
                    (service_id, max(1, min(limit, 50))),
                )
            ]

    def record_tls(self, observation: dict) -> dict:
        """Persist one observation and prune this service to the newest few."""
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO tls_observations VALUES (?,?,?,?)",
                (
                    observation["id"],
                    observation["service_id"],
                    observation["checked_at"],
                    json.dumps(observation),
                ),
            )
            db.execute(
                "DELETE FROM tls_observations WHERE service_id=? AND id NOT IN ("
                "SELECT id FROM tls_observations WHERE service_id=? "
                "ORDER BY checked_at DESC, rowid DESC LIMIT ?)",
                (
                    observation["service_id"],
                    observation["service_id"],
                    self.OBSERVATIONS_PER_SERVICE,
                ),
            )
            db.execute(
                "DELETE FROM tls_observations WHERE rowid NOT IN "
                "(SELECT rowid FROM tls_observations ORDER BY rowid DESC LIMIT 5000)"
            )
        return observation

    def tls_observations(self, service_id: str, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM tls_observations WHERE service_id=? "
                    "ORDER BY checked_at DESC, rowid DESC LIMIT ?",
                    (service_id, max(1, min(limit, 50))),
                )
            ]

    def record_observation(self, observation: dict) -> dict:
        """Persist one observation and prune this service to the newest few."""
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO service_observations VALUES (?,?,?,?)",
                (
                    observation["id"],
                    observation["service_id"],
                    observation["checked_at"],
                    json.dumps(observation),
                ),
            )
            db.execute(
                "DELETE FROM service_observations WHERE service_id=? AND id NOT IN ("
                "SELECT id FROM service_observations WHERE service_id=? "
                "ORDER BY checked_at DESC, rowid DESC LIMIT ?)",
                (
                    observation["service_id"],
                    observation["service_id"],
                    self.OBSERVATIONS_PER_SERVICE,
                ),
            )
            db.execute(
                "DELETE FROM service_observations WHERE rowid NOT IN "
                "(SELECT rowid FROM service_observations ORDER BY rowid DESC LIMIT 5000)"
            )
        return observation

    def observations(self, service_id: str, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM service_observations WHERE service_id=? "
                    "ORDER BY checked_at DESC, rowid DESC LIMIT ?",
                    (service_id, max(1, min(limit, 50))),
                )
            ]

    def latest_observations(self) -> dict[str, dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT service_id, record FROM service_observations o WHERE rowid = ("
                "SELECT rowid FROM service_observations WHERE service_id=o.service_id "
                "ORDER BY checked_at DESC, rowid DESC LIMIT 1)"
            ).fetchall()
            return {row[0]: json.loads(row[1]) for row in rows}
