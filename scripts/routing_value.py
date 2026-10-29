"""Opt-in local routing comparison; fresh process per mode, no downloads.

Source .env.native.ps1. Run --mode fixed|policy|log-experiment|switch --output PATH.
The log experiment tests a declared structured-classification capability, not presumed
superiority. It does not change the shipped conservative policy or frozen suites.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.ai.native import NativeProvider
from app.ai.routing import RoutedProvider, routing_scope
from app.core.config import Settings
from app.evaluation.runner import evaluate
from compare_models import process_memory, system_memory

ROOT = Path(__file__).resolve().parents[1]


async def run(mode, output):
    if output.exists():
        raise ValueError("Refusing to overwrite an existing measurement")
    settings = Settings()
    if settings.ai_provider != "native" or settings.embedding_provider != "native":
        raise ValueError(
            "This comparison requires explicitly configured native generation/embeddings"
        )
    if system_memory()["available"] < 7 * 1024**3:
        raise RuntimeError("Comparison requires 7 GiB available RAM")
    settings = settings.model_copy(
        update={"model_registry_path": ROOT / "docs/models.example.yaml"}
    )
    events = []

    class ObservedNative(NativeProvider):
        async def start(self):
            before, start = process_memory(), time.perf_counter()
            await super().start()
            await self.loading
            events.append(
                {
                    "event": "load",
                    "model": self.path.name,
                    "seconds": time.perf_counter() - start,
                    "before": before,
                    "after": process_memory(),
                }
            )

        async def close(self):
            start = time.perf_counter()
            await super().close()
            events.append(
                {
                    "event": "close",
                    "model": self.path.name,
                    "seconds": time.perf_counter() - start,
                    "after": process_memory(),
                }
            )

    report = {
        "mode": mode,
        "scope": "single local CPU run; OS file cache and run order uncontrolled; no significance claim",
        "before": process_memory(),
        "events": events,
        "suites": [],
    }
    started = time.perf_counter()
    for suite in ("development", "heldout") if mode != "switch" else ():
        if mode == "fixed":
            provider = ObservedNative(settings)
        else:
            provider = RoutedProvider(settings, factory=ObservedNative)
            if mode == "log-experiment":
                provider.registry.routing.policies["logs"] = "qwen-large"
        result = await evaluate(
            settings,
            suite_name=suite,
            generation_runtime=provider,
            progress=lambda case, suite=suite: print(mode, suite, case, flush=True),
        )
        report["suites"].append(result)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if mode == "switch":
        provider = RoutedProvider(settings, factory=ObservedNative)
        report["requests"] = []
        try:
            for model_id in ("qwen-small", "qwen-large", "qwen-small"):
                start = time.perf_counter()
                with routing_scope("general", model_id):
                    answer = await provider.chat(
                        [{"role": "user", "content": "Reply with OK."}]
                    )
                report["requests"].append(
                    {
                        "routing": provider.last_route,
                        "answer": answer,
                        "seconds": time.perf_counter() - start,
                        "memory": process_memory(),
                    }
                )
            provider.registry.get("qwen-large").model_path = ROOT / "models/absent.gguf"
            with routing_scope("general", "qwen-large"):
                await provider.chat([{"role": "user", "content": "Reply with OK."}])
            report["fallback"] = provider.last_route
        finally:
            await provider.close()
    report.update(seconds=time.perf_counter() - started, after=process_memory())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"mode": mode, "seconds": report["seconds"], "memory": report["after"]}
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("fixed", "policy", "log-experiment", "switch"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.mode, args.output))
