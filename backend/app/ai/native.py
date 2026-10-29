"""Local GGUF runtimes. Each instance owns one serialized native worker."""

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from app.ai.base import Messages, ModelStatus
from app.core.config import Settings
from app.core.errors import ProviderError

logger = logging.getLogger("grimoire")
T = TypeVar("T")


def decoding_schema(schema):
    """Avoid llama.cpp expanding long bounded strings into enormous grammar rules.

    The original schema is still enforced by Pydantic after generation. Types, required
    fields, enums and object shape constrain decoding; string lengths are checked later.
    """
    if isinstance(schema, dict):
        return {
            key: decoding_schema(value)
            for key, value in schema.items()
            if key not in {"maxLength", "minLength", "pattern"}
        }
    if isinstance(schema, list):
        return [decoding_schema(item) for item in schema]
    return schema


class NativeRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        embedding: bool = False,
        loader: Callable[..., Any] | None = None,
    ):
        self.settings = settings
        self.embedding = embedding
        self.path = (
            settings.native_embedding_model_path if embedding else settings.native_model_path
        )
        self.context_size = (
            settings.native_embedding_context_size if embedding else settings.native_context_size
        )
        self.model: Any = None
        self.loader = loader
        self.state = "not_configured"
        self.reason = (
            "Configure a local embedding GGUF path."
            if embedding
            else ("Configure GRIMOIRE_NATIVE_MODEL_PATH with a local GGUF file.")
        )
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grimoire-model")
        self.lock = asyncio.Lock()
        self.loading: asyncio.Task | None = None

    async def _run(self, function: Callable[..., T], *args: Any) -> T:
        future = asyncio.get_running_loop().run_in_executor(self.worker, function, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A native call cannot be interrupted safely. Drain before releasing ownership.
            try:
                await asyncio.shield(future)
            finally:
                raise

    async def start(self) -> None:
        if self.loading is None:
            self.state = "loading"
            self.reason = "Loading local model."
            self.loading = asyncio.create_task(self._run(self._load))

    def _load(self) -> None:
        if self.path is None:
            self.state = "not_configured"
            self.reason = (
                "Configure a local embedding GGUF path."
                if self.embedding
                else ("Configure GRIMOIRE_NATIVE_MODEL_PATH with a local GGUF file.")
            )
            return
        try:
            if not self.path.is_file():
                self.state, self.reason = "model_missing", "Configured GGUF file is missing."
                return
            if self.path.suffix.lower() != ".gguf":
                raise ValueError("Expected GGUF")
            loader = self.loader
            if loader is None:
                from llama_cpp import Llama

                loader = Llama
            options = dict(
                model_path=str(self.path),
                n_ctx=self.context_size,
                n_gpu_layers=self.settings.native_gpu_layers,
                embedding=self.embedding,
                verbose=False,
            )
            if self.settings.native_threads is not None:
                options["n_threads"] = self.settings.native_threads
                options["n_threads_batch"] = self.settings.native_threads
            if not self.embedding and self.settings.native_chat_format:
                options["chat_format"] = self.settings.native_chat_format
            self.model = loader(**options)
            self.state, self.reason = "ready", None
            logger.info("native_model_ready", extra={"embedding": self.embedding})
        except (ImportError, OSError):
            self.state = "failed"
            self.reason = "Native runtime unavailable; install Grimoire's native dependencies."
            logger.error("native_runtime_unavailable")
        except Exception:
            self.state = "failed"
            self.reason = "GGUF loading failed; check model compatibility and available memory."
            logger.error("native_model_load_failed")

    async def health(self) -> ModelStatus:
        return {
            "provider": "native",
            "backend": "llama.cpp",
            "model": self.path.name if self.path else None,
            "state": self.state,
            "reason": self.reason,
            "context_size": self.context_size,
            "gpu_layers": self.settings.native_gpu_layers,
        }

    def _require_ready(self) -> Any:
        if self.state != "ready" or self.model is None:
            raise ProviderError(self.reason or "Native model unavailable.")
        return self.model

    async def close(self) -> None:
        if self.loading:
            await self.loading
        async with self.lock:
            if self.model is not None:
                await self._run(self.model.close)
                self.model = None
            self.state, self.reason = "not_configured", "Runtime stopped."
        self.worker.shutdown(wait=False)


class NativeProvider(NativeRuntime):
    def _completion(self, messages: Messages, stream: bool, schema=None, max_tokens=None):
        return self._require_ready().create_chat_completion(
            messages=messages,
            stream=stream,
            temperature=0.15,
            max_tokens=max_tokens or self.settings.max_output_tokens,
            seed=0,
            **({"response_format": {"type": "json_object", "schema": schema}} if schema else {}),
        )

    async def chat(self, messages: Messages) -> str:
        return await self._chat(messages)

    async def chat_json(self, messages: Messages, schema: dict, max_tokens: int) -> str:
        return await self._chat(messages, decoding_schema(schema), max_tokens)

    async def _chat(self, messages: Messages, schema=None, max_tokens=None) -> str:
        async with self.lock:
            try:
                self.last_generation = {}
                result = await self._run(self._completion, messages, False, schema, max_tokens)
                self.last_generation = {
                    "finish_reason": result["choices"][0].get("finish_reason"),
                    "usage": result.get("usage", {}),
                    "max_tokens": max_tokens or self.settings.max_output_tokens,
                }
                if self.last_generation["finish_reason"] == "length":
                    raise ProviderError(
                        "Generation reached its output limit; incomplete answer rejected."
                    )
                content = result["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Empty response")
                return content
            except ProviderError:
                raise
            except Exception as exc:
                logger.warning("native_generation_failed")
                raise ProviderError(
                    "Native generation failed; check context and model settings."
                ) from exc

    async def stream(self, messages: Messages) -> AsyncIterator[str]:
        async with self.lock:
            iterator = None
            try:
                iterator = await self._run(self._completion, messages, True)
                received = False
                finished = False
                while True:
                    # StopIteration must not cross an asyncio Future boundary.
                    chunk = await self._run(next, iterator, None)
                    if chunk is None:
                        break
                    choice = chunk["choices"][0]
                    content = choice.get("delta", {}).get("content", "")
                    if not isinstance(content, str):
                        raise ValueError("Invalid token")
                    if content:
                        received = received or bool(content.strip())
                        yield content
                    if choice.get("finish_reason") is not None:
                        if choice["finish_reason"] == "length":
                            raise ProviderError(
                                "Generation reached its output limit; stream incomplete."
                            )
                        finished = True
                        break
                if not received or not finished:
                    raise ValueError("Incomplete stream")
            except ProviderError:
                raise
            except Exception as exc:
                logger.warning("native_stream_failed")
                raise ProviderError(
                    "Native stream failed; check context and model settings."
                ) from exc
            finally:
                if iterator is not None and hasattr(iterator, "close"):
                    await self._run(iterator.close)


class NativeEmbeddings(NativeRuntime):
    def __init__(self, settings: Settings, *, loader=None):
        super().__init__(settings, embedding=True, loader=loader)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        def calculate():
            model = self._require_ready()
            vectors = model.embed(texts, normalize=True, truncate=False)
            if (
                len(vectors) != len(texts)
                or any(
                    not isinstance(v, list)
                    or not v
                    or not any(v)
                    or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in v)
                    for v in vectors
                )
                or len({len(v) for v in vectors}) > 1
            ):
                raise ValueError("Invalid embedding vectors; use a pooled embedding GGUF")
            return vectors

        async with self.lock:
            try:
                return await self._run(calculate)
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError(
                    "Native embeddings failed; check the embedding GGUF and context."
                ) from exc
