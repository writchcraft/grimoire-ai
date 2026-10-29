import hashlib
import json

from starlette.concurrency import run_in_threadpool

from app.ai.base import EmbeddingProvider
from app.core.config import Settings
from app.core.errors import DocumentError
from app.ingestion.parser import chunk_document, parse_document, safe_filename
from app.services.storage import Storage


async def ingest(
    storage: Storage,
    provider: EmbeddingProvider,
    settings: Settings,
    filename: str,
    raw: bytes,
    source_path: str | None = None,
    service: str | None = None,
) -> dict:
    filename = safe_filename(filename)
    text, kind, detected_service = await run_in_threadpool(
        parse_document, filename, raw, settings.max_upload_bytes
    )
    source_path = source_path or filename
    service = service or detected_service
    digest = hashlib.sha256(
        json.dumps([filename, source_path, service, text], ensure_ascii=False).encode()
    ).hexdigest()
    existing = await run_in_threadpool(storage.document_by_hash, digest)
    if existing:
        return existing
    chunks = await run_in_threadpool(chunk_document, text, kind)
    if not chunks or len(chunks) > settings.max_chunks:
        raise DocumentError(f"Document must contain between 1 and {settings.max_chunks} chunks")
    vectors = await provider.embed([chunk.text for chunk in chunks])
    return await run_in_threadpool(
        storage.add_document,
        {
            "filename": filename,
            "source_path": source_path,
            "document_type": kind,
            "service": service,
            "content_hash": digest,
        },
        chunks,
        vectors,
        raw,
    )
