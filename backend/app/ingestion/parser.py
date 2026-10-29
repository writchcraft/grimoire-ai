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
    heading: str = ""


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
    """Pack semantic units, keeping exact text/line spans; hard bound exceptional units.

    Headings start sections; fences, paragraphs, lists and service/config blocks are
    indivisible below 2400 chars. Long units fall back to lines, never silent truncation.
    """
    lines = text.splitlines()
    units: list[tuple[int, int, bool]] = []
    start, fenced, fence_marker, section_start = 0, False, "", False
    # Config blocks split at peer definitions, not each nested property. Compose-like
    # services are one level below their parent; ordinary root key/value groups stay together.
    config = kind in {"yaml", "yml", "json"}
    definition_indent = None
    if config:
        for line in lines:
            if re.match(
                r'^(?:services|containers):\s*$|^\s*"(?:services|containers)"\s*:\s*\{', line
            ):
                definition_indent = len(line) - len(line.lstrip()) + 2
                break

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        heading = kind == "md" and not fenced and bool(re.match(r"^#{1,6}\s", line))
        ini = kind == "txt" and bool(re.match(r"^\[[^]]+\]\s*$", line))
        peer = (
            config
            and definition_indent is not None
            and len(line) - len(stripped) == definition_indent
            and bool(re.match(r'(?:[\w.-]+|"[^"\n]+")\s*:', stripped))
        )
        if heading or ini or peer:
            if i > start:
                units.append((start, i, section_start))
            start, section_start = i, True
        log_entry = kind == "log" and bool(
            re.match(
                r"^(?:\d{4}-\d{2}-\d{2}[T ]|(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)\b)", line
            )
        )
        if log_entry and i > start:
            units.append((start, i, section_start))
            start, section_start = i, False
        if not fenced and not stripped and i >= start:
            units.append((start, i + 1, section_start))
            start, section_start = i + 1, False
        if kind == "md" and stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not fenced:
                fenced, fence_marker = True, marker
            elif marker == fence_marker:
                fenced = False
    if start < len(lines):
        units.append((start, len(lines), section_start))

    chunks: list[Chunk] = []
    current_start, current_end, size = 0, 0, 0

    def flush():
        nonlocal size
        if size and "\n".join(lines[current_start:current_end]).strip():
            chunks.append(
                Chunk("\n".join(lines[current_start:current_end]), current_start + 1, current_end)
            )
        size = 0

    for begin, end, force in units:
        unit_size = sum(len(line) + 1 for line in lines[begin:end])
        if size and (force or size + unit_size > 2400 or size >= target):
            flush()
        if unit_size <= 2400:
            if not size:
                current_start = begin
            current_end = end
            size += unit_size
            continue
        flush()
        # Bounded fallback for unusually large blocks and minified JSON. Original line
        # metadata is retained even for line fragments; complete-line validation may refuse.
        for i in range(begin, end):
            line = lines[i]
            if len(line) > 2400:
                flush()
                for offset in range(0, len(line), 2000):
                    chunks.append(Chunk(line[offset : offset + 2000], i + 1, i + 1))
                continue
            if size and size + len(line) + 1 > 2400:
                flush()
            if not size:
                current_start = i
            current_end = i + 1
            size += len(line) + 1
        flush()
    flush()
    # Metadata is separate from quoted text: inherited headings cannot become invented lines.
    headings, fenced = [], False
    contexts = []
    for line in lines:
        stripped = line.lstrip()
        if kind == "md" and stripped.startswith(("```", "~~~")):
            fenced = not fenced
        match = re.match(r"^(#{1,6})\s+(.+)", line) if kind == "md" and not fenced else None
        if match:
            level = len(match.group(1))
            headings = [(n, value) for n, value in headings if n < level]
            headings.append((level, match.group(2)))
        if kind == "txt" and re.match(r"^\[[^]]+\]$", stripped):
            headings = [(1, stripped)]
        contexts.append(" / ".join(value for _, value in headings))
    for chunk in chunks:
        chunk.heading = contexts[chunk.line_start - 1] if contexts else ""
    return chunks
