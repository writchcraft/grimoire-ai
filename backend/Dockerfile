FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GRIMOIRE_DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml requirements-native.lock ./
COPY app ./app
RUN pip install --no-cache-dir -r requirements-native.lock && pip install --no-deps '.[native]' \
    && groupadd --gid 10001 grimoire \
    && useradd --uid 10001 --gid grimoire --no-create-home grimoire \
    && mkdir /app/data /models && chown grimoire:grimoire /app/data
USER grimoire
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
