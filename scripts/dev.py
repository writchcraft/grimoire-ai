"""Serve the backend and static frontend on one local origin for native development."""

from pathlib import Path

import uvicorn
from app.main import create_app
from fastapi.staticfiles import StaticFiles

app = create_app()
app.mount(
    "/",
    StaticFiles(directory=Path(__file__).resolve().parents[1] / "frontend", html=True),
    name="frontend",
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080, access_log=False)
