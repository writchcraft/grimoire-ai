"""Serve the backend and static frontend on one local origin for native development."""

from app.services.serving import serve

if __name__ == "__main__":
    serve()
