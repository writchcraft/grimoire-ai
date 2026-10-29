"""One local web-product entry point for the development script and CLI."""

import os
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles


def frontend_directory():
    return Path(__file__).resolve().parents[3] / "frontend"


def serve():
    from app.main import create_app

    frontend = frontend_directory()
    if not (frontend / "index.html").is_file():
        raise RuntimeError(
            "Frontend assets missing; run serve from an editable repository install."
        )
    app = create_app()
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    uvicorn.run(
        app, host="127.0.0.1", port=int(os.environ.get("GRIMOIRE_PORT", "7864")), access_log=False
    )
