"""Run the local publication gate with the current Python interpreter."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_PARTS = {
    ".venv",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "models",
    "secrets",
    "dist",
    "build",
    "artifacts",
}
BLOCKED_SUFFIXES = {
    ".gguf",
    ".ggml",
    ".safetensors",
    ".onnx",
    ".pt",
    ".pth",
    ".bin",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
    ".pyc",
}
SECRET_PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_" + rb"[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bAKIA" + rb"[A-Z0-9]{16}\b"),
]


def run(*command: str) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def audit_tracked_files() -> None:
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    failures = []
    for raw in filter(None, paths):
        relative = raw.decode("utf-8")
        path = Path(relative)
        name = path.name.lower()
        forbidden = (
            bool(set(path.parts) & BLOCKED_PARTS)
            or path.suffix.lower() in BLOCKED_SUFFIXES
            or (name.startswith(".env") and name != ".env.example")
            or relative.startswith("data/runtime/")
            or name == "credentials.json"
            or bool(re.search(r"\.(?:db|sqlite3?)(?:-.*)?$", name))
            or (name.endswith(".log") and not relative.startswith("data/examples/"))
        )
        absolute = ROOT / path
        if forbidden or absolute.stat().st_size > 5 * 1024 * 1024:
            failures.append(relative)
            continue
        if any(pattern.search(absolute.read_bytes()) for pattern in SECRET_PATTERNS):
            failures.append(relative)
    if failures:
        raise SystemExit("Review blocked tracked files: " + ", ".join(failures))
    print("Tracked-file artifact and credential-pattern audit passed.", flush=True)


def main() -> None:
    audit_tracked_files()
    run("git", "diff", "--check", "HEAD")
    run(sys.executable, "-m", "ruff", "check", "backend", "scripts")
    run(sys.executable, "-m", "ruff", "format", "--check", "backend", "scripts")
    run(sys.executable, "-m", "pytest", "backend/tests", "-q")
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("Node.js/npm is required for the frontend checks.")
    run(npm, "run", "check", "--prefix", "frontend")
    run(npm, "test", "--prefix", "frontend")
    print("Local gate passed. Run Docker and real-model acceptance separately.")


if __name__ == "__main__":
    main()
