import pytest

from app.core.errors import DocumentError
from app.ingestion.parser import chunk_document, parse_document, safe_filename


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../../etc/test.md", "test.md"),
        (r"C:\users\test.yaml", "test.yaml"),
        ("note with spaces.TXT", "note_with_spaces.TXT"),
    ],
)
def test_sanitizes_upload_name(name, expected):
    assert safe_filename(name) == expected


@pytest.mark.parametrize("name", ["a.exe", "no_extension", "..", "a.md.exe"])
def test_rejects_unsupported_names(name):
    with pytest.raises(DocumentError):
        safe_filename(name)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("test.md", b"# Runbook\nRestart service"),
        ("test.txt", b"hello"),
        ("test.json", b'{"service":"grafana", "port":3000}'),
        ("test.yaml", b"service: grafana\nport: 3000"),
        ("test.yml", b"items: [1, 2]"),
        ("test.log", b"2026-10-28T12:00:00Z ERROR failed"),
    ],
)
def test_supported_formats(name, data):
    text, kind, _ = parse_document(name, data, 4096)
    assert text and kind == name.split(".")[-1]


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("bad.json", b'{"x":'),
        ("bad.yaml", b"x: [a,"),
        ("bad.yaml", b"!python/object/apply:os.system [echo]"),
        ("bad.txt", b"\xff"),
        ("bad.md", b"\x00abc"),
        ("bad.log", b"  \n "),
    ],
)
def test_invalid_files(name, data):
    with pytest.raises(DocumentError):
        parse_document(name, data, 4096)


def test_yaml_aliases_are_not_expanded_or_traversed():
    text, _, _ = parse_document("cycle.yml", b"cycle: &a [*a]", 4096)
    assert "*a" in text


def test_upload_size_is_bytes():
    with pytest.raises(DocumentError, match="exceeds"):
        parse_document("test.txt", "é".encode() * 100, 100)


def test_markdown_sections_and_line_numbers():
    text = "# Grafana\nPort 3000\n\n# Prometheus\nPort 9090"
    chunks = chunk_document(text, "md")
    assert [(c.line_start, c.line_end) for c in chunks] == [(1, 3), (4, 5)]
    assert "Port 3000" in chunks[0].text
    assert "Prometheus" not in chunks[0].text


def test_fenced_heading_is_not_a_new_section():
    chunks = chunk_document("# Runbook\n```sh\n# a shell comment\necho hi\n```", "md")
    assert len(chunks) == 1


def test_long_lines_and_logs_are_bounded_and_keep_evidence():
    chunks = chunk_document("a" * 5000 + "\nERROR failure", "log")
    assert max(len(c.text) for c in chunks) <= 2400
    assert chunks[0].line_start == chunks[0].line_end == 1
    assert chunks[-1].line_start == 2


def test_detects_service_metadata():
    assert parse_document("x.json", b'{"service":"grafana"}', 4096)[2] == "grafana"
    assert parse_document("x.yaml", b"service: nginx", 4096)[2] == "nginx"
