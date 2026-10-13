import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml

from app.core.errors import DocumentError

SUPPORTED = {"md", "txt", "json", "yaml", "yml", "log"}


@dataclass
class Chunk:
    text: str
    line_start: int
    line_end: int


def safe_filename(value: str) -> str:
    name = PurePosixPath(value.replace("\\", "/")).name
    name = re.sub(r"[^a-zA-Z0-9._-]", "_", name).lstrip(".")[:180]
    if not name or "." not in name or name.rsplit(".", 1)[-1].lower() not in SUPPORTED:
        raise DocumentError("Supported files: .md, .txt, .json, .yaml, .yml and .log")
    return name


def parse_document(filename: str, raw: bytes, max_bytes: int) -> tuple[str, str, str | None]:
    if len(raw) > max_bytes:
        raise DocumentError(f"File exceeds the {max_bytes} byte upload limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentError("Files must use UTF-8 text encoding") from exc
    if not text.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise DocumentError("File must contain non-empty text without binary control characters")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kind = filename.rsplit(".", 1)[-1].lower()
    service = None
    try:
        if kind == "json":
            parsed = json.loads(text)
            if isinstance(parsed, dict) and isinstance(parsed.get("service"), str):
                service = parsed["service"][:100]
        elif kind in {"yaml", "yml"}:
            # Parse events without constructing objects or expanding aliases. Reject custom tags.
            # YAML may contain cycles, so avoid recursive tree traversal entirely.
            for event in yaml.parse(text, Loader=yaml.SafeLoader):
                tag = getattr(event, "tag", None)
                if tag and tag not in yaml.SafeLoader.yaml_constructors:
                    raise DocumentError("Custom YAML tags are not supported")
        match = re.search(r"(?im)^service:\s*[\"']?([\w.-]+)", text)
        service = service or (match.group(1) if match else None)
    except (json.JSONDecodeError, yaml.YAMLError, RecursionError, ValueError) as exc:
        raise DocumentError(f"Invalid {kind.upper()} document") from exc
    return text, kind, service


def chunk_document(text: str, kind: str, target: int = 1400) -> list[Chunk]:
    """Keep line evidence; prefer headings, paragraphs, log entries and config keys."""
    chunks: list[Chunk] = []
    current: list[str] = []
    size = 0
    start = 1
    fenced = False

    def flush(end: int) -> None:
        nonlocal current, size
        if current and "\n".join(current).strip():
            chunks.append(Chunk("\n".join(current), start, end))
        current, size = [], 0

    for number, line in enumerate(text.splitlines(), 1):
        heading = kind == "md" and line.startswith("#") and not fenced
        config_key = kind in {"yaml", "yml", "json"} and bool(
            re.match(r'^(?:[\w.-]+\s*:| {2}[\w.-]+\s*:| {2}"[^"\n]+"\s*:)', line)
        )
        boundary = heading or config_key or not line.strip() or kind == "log"
        if current and (heading or (boundary and size >= target) or size + len(line) > 2400):
            flush(number - 1)
        # Exceptional long lines (e.g. one-line JSON) are bounded with accurate line metadata.
        if len(line) > 2400:
            flush(number - 1)
            for offset in range(0, len(line), 2000):
                chunks.append(Chunk(line[offset : offset + 2000], number, number))
            continue
        if not current:
            start = number
        current.append(line)
        size += len(line) + 1
        if kind == "md" and line.lstrip().startswith("```"):
            fenced = not fenced
    flush(len(text.splitlines()))
    return chunks
