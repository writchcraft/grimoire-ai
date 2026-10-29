"""Deterministic routing and serialized, lazy generation model ownership."""

import asyncio
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

from app.ai.catalog import load_registry
from app.ai.registry import generation_provider
from app.core.errors import ProviderError

logger = logging.getLogger("grimoire")
route_decision = ContextVar("generation_decision", default=None)
request_route = ContextVar("generation_route", default=("general", "auto"))


@contextmanager
def routing_scope(task, model="auto"):
    token = request_route.set((task, model))
    decision_token = route_decision.set(None)
    try:
        yield
    finally:
        if route_decision.get() is None:
            logger.info(
                "generation_not_used",
                extra={
                    "routing": {
                        "task": task,
                        "selected_model": None,
                        "policy_rule": "no generation required",
                        "fallback_used": False,
                        "provider": None,
                        "readiness": "not_used",
                    }
                },
            )
        route_decision.reset(decision_token)
        request_route.reset(token)


class RoutedProvider:
    def __init__(self, settings, factory=generation_provider):
        self.settings = settings
        self.registry = load_registry(settings)
        self.factory = factory
        self.lock = asyncio.Lock()
        self.active = None
        self.active_id = None
        self.failures = {}
        self.last_generation = {}
        self.last_route = None

    async def start(self):
        pass  # Generation is lazy; registry inspection allocates no model memory.

    async def _close_active(self, *, unload=True):
        if self.active:
            if unload and hasattr(self.active, "unload"):
                await self.active.unload()
            await self.active.close()
            self.active = None
            self.active_id = None

    def _availability(self, model):
        if (
            not self.settings.model_registry_path
            and model.provider == "native"
            and not self.settings.native_model_path
        ):
            return "not_configured"
        return model.availability()

    async def _select(self):
        task, override = request_route.get()
        candidates = self.registry.candidates(task, override)
        skipped = []
        for model_id in candidates:
            model = self.registry.get(model_id)
            state = self.failures.get(model_id) or self._availability(model)
            if task not in model.capabilities:
                state = "incapable"
            if state != "unverified":
                skipped.append({"model": model_id, "readiness": state})
                continue
            started = time.perf_counter()
            if self.active_id != model_id:
                await self._close_active()
                options = self.settings.model_copy(
                    update={
                        "ai_provider": model.provider,
                        "native_model_path": model.model_path,
                        "model": model.model_name or model.id,
                        "native_context_size": model.context_size,
                        "max_output_tokens": min(
                            self.settings.max_output_tokens, model.output_limit
                        ),
                    }
                )
                runtime = self.factory(options)
                self.active, self.active_id = runtime, model_id
                try:
                    await runtime.start()
                    if getattr(runtime, "loading", None) is not None:
                        await asyncio.shield(runtime.loading)
                    health = await runtime.health()
                    if health["state"] != "ready":
                        raise ProviderError(health.get("reason") or "Model unavailable")
                except asyncio.CancelledError:
                    await self._close_active(unload=False)
                    raise
                except (ProviderError, OSError, RuntimeError):
                    self.failures[model_id] = "failed"
                    await self._close_active(unload=False)
                    skipped.append({"model": model_id, "readiness": "failed"})
                    continue
            health = await self.active.health()
            if health["state"] != "ready":
                self.failures[model_id] = health["state"]
                await self._close_active()
                skipped.append({"model": model_id, "readiness": health["state"]})
                continue
            self.last_route = {
                "task": task,
                "selected_model": model_id,
                "display_name": model.display_name,
                "policy_rule": "manual override"
                if override != "auto"
                else (
                    "explicit task policy"
                    if task in self.registry.routing.policies
                    else "efficient default"
                ),
                "mode": override,
                "fallback_used": bool(skipped),
                "attempts": skipped,
                "provider": model.provider,
                "readiness": "ready",
                "selection_seconds": time.perf_counter() - started,
            }
            route_decision.set(self.last_route)
            logger.info("model_route", extra={"routing": self.last_route})
            return self.active
        self.last_route = {
            "task": task,
            "selected_model": None,
            "fallback_used": len(skipped) > 1,
            "attempts": skipped,
            "readiness": "failed",
            "provider": None,
            "policy_rule": "manual override" if override != "auto" else "configured policy/default",
        }
        route_decision.set(self.last_route)
        logger.info("model_route_failed", extra={"routing": self.last_route})
        raise ProviderError(
            "No allowed generation model is ready; inspect models and policy "
            "(native GGUF or configured runtime)."
        )

    async def chat(self, messages):
        async with self.lock:
            runtime = await self._select()
            runtime.settings = runtime.settings.model_copy(
                update={
                    "max_output_tokens": min(
                        self.settings.max_output_tokens,
                        self.registry.get(self.active_id).output_limit,
                    )
                }
            )
            result = await runtime.chat(messages)
            self.last_generation = {
                **getattr(runtime, "last_generation", {}),
                "routing": self.last_route,
            }
            return result

    async def chat_json(self, messages, schema, max_tokens):
        async with self.lock:
            runtime = await self._select()
            limit = min(max_tokens, self.registry.get(self.active_id).output_limit)
            result = await runtime.chat_json(messages, schema, limit)
            self.last_generation = {
                **getattr(runtime, "last_generation", {}),
                "routing": self.last_route,
            }
            return result

    async def stream(self, messages):
        from contextlib import aclosing

        async with self.lock:
            runtime = await self._select()
            async with aclosing(runtime.stream(messages)) as tokens:
                async for token in tokens:
                    yield token

    def inventory(self):
        return [
            {
                **model.model_dump(mode="json"),
                "state": self.failures.get(model.id)
                or ("ready" if self.active_id == model.id else self._availability(model)),
                "default": model.id == self.registry.routing.default,
            }
            for model in self.registry.models
        ]

    async def ensure_ready(self):
        async with self.lock:
            runtime = await self._select()
            return await runtime.health()

    async def health(self):
        if self.active:
            return {**await self.active.health(), "routing": self.last_route}
        model = self.registry.get(self.registry.routing.default)
        state = self._availability(model)
        return {
            "provider": model.provider,
            "backend": "llama.cpp" if model.provider == "native" else "Ollama",
            "model": model.display_name,
            "state": "not_loaded" if state == "unverified" else state,
            "reason": "Lazy generation; inference unverified.",
        }

    async def close(self):
        async with self.lock:
            await self._close_active()
