"""Guard the publication boundary without creating sensitive fixture files."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        "backend/.env.local",
        "secrets/token.txt",
        "models/chat.gguf",
        "download/model.safetensors",
        "download/model.onnx",
        "data/runtime/store.sqlite3",
        "backup.db",
        "backup.db-wal",
        "backup.sqlite",
        "private.pem",
        "private.key",
        "credentials.json",
        "server.log",
        ".cache/results.json",
        "frontend/node_modules/package/index.js",
        "frontend/dist/index.html",
        "backend/__pycache__/main.pyc",
    ],
)
def test_local_artifacts_are_ignored(path):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path], cwd=ROOT, check=False
    )
    assert result.returncode == 0, path


@pytest.mark.parametrize("path", [".env.example", "data/examples/proxy-incident.log"])
def test_public_examples_remain_trackable(path):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path], cwd=ROOT, check=False
    )
    assert result.returncode == 1, path
