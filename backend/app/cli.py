"""Focused operational CLI. Run with grimoire or python -m app.cli."""

import argparse
import asyncio
import json
import sqlite3
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.ai.registry import embedding_provider, generation_provider
from app.core.config import Settings
from app.core.errors import DocumentError, ProviderError
from app.ingestion.parser import parse_document
from app.ingestion.pipeline import ingest
from app.models import LogRequest
from app.services.acceptance import synthetic_acceptance
from app.services.diagnostics import diagnose, inspect_models
from app.services.log_analysis import analyse
from app.services.runtime import application_runtime, embedding_identity, wait_ready
from app.services.serving import serve
from app.services.storage import Storage


def parser():
    root = argparse.ArgumentParser(
        prog="grimoire", description="Grimoire operations and diagnostics"
    )
    commands = root.add_subparsers(dest="command", required=True)
    evaluation = commands.add_parser(
        "evaluate", help="Evaluate synthetic evidence in an isolated temporary index"
    )
    evaluation.add_argument("--json", action="store_true")
    evaluation.add_argument("--retrieval-only", action="store_true")
    evaluation.add_argument("--compare-legacy", action="store_true")
    evaluation.add_argument(
        "--limit-probe", action="store_true", help="Compare old log prose at 384 and 768 tokens"
    )
    evaluation.add_argument(
        "--output", type=Path, help="Save detailed JSON, including safe synthetic outputs"
    )
    for name, help_text in (
        ("serve", "Serve the web application on loopback"),
        ("status", "Inspect configuration readiness without loading models"),
        ("doctor", "Run safe, read-only diagnostics"),
        ("validate", "Run non-destructive acceptance checks"),
        ("demo", "Guide a reviewer through public example data"),
    ):
        command = commands.add_parser(name, help=help_text)
        if name != "serve":
            command.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    models = commands.add_parser("models", help="Inspect or explicitly verify configured models")
    subs = models.add_subparsers(dest="action", required=True)
    for name in ("list", "inspect", "verify"):
        command = subs.add_parser(name)
        command.add_argument("--json", action="store_true")
        if name != "list":
            command.add_argument(
                "--role", choices=("generation", "embedding", "all"), default="all"
            )
    knowledge = commands.add_parser(
        "knowledge", help="Ingest local files or search indexed evidence"
    )
    subs = knowledge.add_subparsers(dest="action", required=True)
    command = subs.add_parser("ingest", help="Ingest one file or a directory (non-recursive)")
    command.add_argument("path", type=Path)
    command.add_argument("--json", action="store_true")
    command = subs.add_parser("search", help="Search existing evidence with hybrid ranking")
    command.add_argument("query")
    command.add_argument("--service", help="Filter by exact indexed service metadata")
    command.add_argument(
        "--debug", action="store_true", help="Show ranking components and evidence selection"
    )
    command.add_argument("--json", action="store_true")
    logs = commands.add_parser("logs", help="Analyse supplied log evidence")
    command = logs.add_subparsers(dest="action", required=True).add_parser("analyse")
    command.add_argument("path", type=Path)
    command.add_argument("--service", default="unknown")
    command.add_argument("--question", default="Explain the observed symptoms.")
    command.add_argument("--json", action="store_true")
    return root


def emit(data, machine):
    if machine:
        print(json.dumps(data, default=str))
    elif "suite_version" in data:
        print("Synthetic local evaluation (deterministic contract checks; manual review separate)")
        for group, result in data["summary"].items():
            print(f"{group}: {json.dumps(result)}")
        print(data["limitations"])
    elif "checks" in data:
        for check in data["checks"]:
            print(f"{check['status']}: {check['check']}: {check['detail']}")
    elif "error" in data:
        print(f"FAIL: {data['error']}", file=sys.stderr)
    elif "version" in data:
        print(f"Grimoire {data['version']} | readiness: {data['readiness']}")
        for role in ("generation", "embedding"):
            model = data[role]
            print(
                f"{role.capitalize()}: {model['provider']} / {model['model'] or 'not configured'}"
                f" ({model['state']})"
            )
        print(f"Storage: {data['database']} / {data['vector_backend']}")
        print(data["scope"])
    elif "models" in data:
        for model in data["models"]:
            print(
                f"{model['role']}: {model['provider']} / {model['model'] or 'not configured'}"
                f" ({model['state']})"
            )
            print(
                f"  Path: {model['path'] or 'n/a'}; bytes: {model['size_bytes']}; "
                f"GGUF header: {model['appears_gguf']}"
            )
            print(
                f"  Context: {model.get('context_size', 'provider managed')}; "
                f"GPU layers: {model.get('gpu_layers', 'provider managed')}"
            )
            if model.get("reason"):
                print(f"  {model['reason']}")
    elif "chunks" in data:
        if "debug" in data:
            print(json.dumps(data["debug"], indent=2))
        for hit in data["chunks"]:
            print(f"{hit['source_path']}:{hit['line_start']}-{hit['line_end']}")
            if "debug" in data:
                print(
                    f"  chunk={hit['id']} score={hit['score']:.4f} "
                    f"components={data['debug']['components'].get(hit['id'], {})}"
                )
            print(hit["text"])
        for service in data["services"]:
            print(f"Service: {service['name']}")
        if not data["chunks"] and not data["services"]:
            print("No matching indexed evidence.")
    elif "documents" in data:
        for doc in data["documents"]:
            print(f"Ingested {doc['source_path']}: {doc['chunk_count']} chunks ({doc['id']})")
    elif "steps" in data:
        print(data["guide"])
        for step in data["steps"]:
            print(f"  {step}")
    elif "explanation" in data:
        print(
            f"{data['service']}: {data['line_count']} lines; "
            f"{data['error_count']} errors, {data['warning_count']} warnings (heuristic)"
        )
        for event in data["events"]:
            print(f"L{event['line']}: {event['text']}")
        print(data["explanation"])
    else:
        print(json.dumps(data, indent=2, default=str))


def package_version():
    try:
        return version("grimoire-ai")
    except PackageNotFoundError:
        return "0.1.0 (source checkout)"


def read_bounded(path, limit):
    if path.is_symlink() or not path.is_file():
        raise DocumentError(f"Expected a regular file, not a symlink: {path}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise DocumentError(f"File exceeds configured {limit}-byte limit: {path}")
    return raw


async def execute(args, settings):
    if args.command == "evaluate":
        from app.evaluation.runner import evaluate

        result = await evaluate(
            settings,
            retrieval_only=args.retrieval_only,
            compare_legacy=args.compare_legacy,
            limit_probe=args.limit_probe,
            progress=lambda case: print(f"Evaluated {case}", file=sys.stderr, flush=True),
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        failed = any(
            "FAIL" in value for key, value in result["summary"].items() if key != "retrieval"
        )
        hit5 = result["summary"]["retrieval"]["hit@5"]
        missed = hit5["found"] < hit5["total"]
        return result, int(failed or missed)
    if args.command in {"doctor", "validate"}:
        result = await diagnose(settings)
        if args.command == "doctor":
            return result, int(any(c["status"] == "FAIL" for c in result["checks"]))
        mapping = {
            "PASS": "VERIFIED",
            "WARN": "UNVERIFIED",
            "FAIL": "BLOCKED",
            "SKIP": "NOT APPLICABLE",
        }
        for check in result["checks"]:
            check["status"] = (
                "UNVERIFIED"
                if check["check"] in {"docker", "vector_store"}
                else mapping[check["status"]]
            )
        for name, operation in (
            ("application_import", import_application),
            ("synthetic_rag_offline", lambda: synthetic_acceptance(settings)),
        ):
            try:
                await operation()
                result["checks"].append(
                    {
                        "check": name,
                        "status": "VERIFIED",
                        "detail": "Application imported."
                        if name == "application_import"
                        else "Synthetic RAG: ingestion, retrieval, no-evidence, persistence; "
                        "Vector rebuild in temp storage; network denied. Model quality unverified.",
                    }
                )
            except Exception as exc:
                result["checks"].append({"check": name, "status": "BLOCKED", "detail": str(exc)})
        result["checks"].append(
            {
                "check": "real_inference_offline",
                "status": "UNVERIFIED",
                "detail": "Not executed. Use models verify and deployment acceptance.",
            }
        )
        result["checks"].append(
            {
                "check": "configured_vector_rebuild",
                "status": "UNVERIFIED",
                "detail": "Configured index not opened or modified; synthetic index tested.",
            }
        )
        return result, int(any(c["status"] == "BLOCKED" for c in result["checks"]))
    if args.command == "status":
        models = await inspect_models(settings)
        storage = Storage.inspect(settings.data_dir, embedding_identity(settings))
        ready = all(m["state"] == "ready" for m in models) and storage["exists"]
        return {
            "version": package_version(),
            "readiness": "ready" if ready else "unverified/degraded",
            "scope": "Configuration snapshot; inference and running server unverified.",
            "generation": models[0],
            "embedding": models[1],
            "database": "SQLite",
            "vector_backend": "embedded Qdrant",
            "storage": storage,
        }, 0
    if args.command == "models":
        if args.action != "verify":
            models = await inspect_models(settings)
            return {
                "models": [m for m in models if getattr(args, "role", "all") in {"all", m["role"]}]
            }, 0
        results = []
        for role, factory in (
            ("generation", generation_provider),
            ("embedding", embedding_provider),
        ):
            if args.role not in {"all", role}:
                continue
            runtime = factory(settings.model_copy(update={"max_output_tokens": 16}))
            try:
                await runtime.start()
                await wait_ready(runtime)
                if role == "generation":
                    output = await runtime.chat([{"role": "user", "content": "Reply with OK."}])
                else:
                    output = await runtime.embed(["Grimoire verification"])
                if not output:
                    raise ProviderError("Model returned an empty result.")
                results.append(
                    {"role": role, "status": "VERIFIED", "detail": "Minimal inference completed."}
                )
            except (ProviderError, OSError, RuntimeError) as exc:
                results.append({"role": role, "status": "BLOCKED", "detail": str(exc)})
            finally:
                await runtime.close()
        return {"checks": [{"check": r["role"], **r} for r in results]}, int(
            any(r["status"] == "BLOCKED" for r in results)
        )
    if args.command == "knowledge":
        if args.action == "search":
            if not args.query.strip() or len(args.query) > 500:
                raise DocumentError("Query must contain 1–500 characters.")
            if not (settings.data_dir / "grimoire.sqlite3").exists():
                raise DocumentError("No knowledge index; ingest a document first.")
            from app.rag.search import search, select_evidence

            async with application_runtime(settings, generation=False) as state:
                await wait_ready(state.embeddings)
                vector = (await state.embeddings.embed([args.query]))[0]
                hits, debug = search(
                    state.storage, args.query, vector, settings.retrieval_threshold, args.service
                )
                result = {
                    "chunks": [h.model_dump() for h in hits],
                    "services": state.storage.services(args.query),
                    "mode": "hybrid",
                }
                if args.debug:
                    debug["selected_ids"] = [
                        h.id for h in select_evidence(state.storage, args.query, hits)
                    ]
                    result["debug"] = debug
                return result, 0
        path = args.path
        if path.is_symlink():
            raise DocumentError("Symlink ingestion is not supported.")
        files = sorted(path.iterdir()) if path.is_dir() else [path]
        files = [
            p
            for p in files
            if p.suffix.lower() in {".md", ".txt", ".yaml", ".yml", ".json", ".log"}
        ]
        if not files or len(files) > 512:
            raise DocumentError(
                "Supply 1–512 supported files; directory ingestion is non-recursive."
            )
        # Validate every input before opening storage or loading the embedding model.
        for file in files:
            parse_document(
                file.name, read_bounded(file, settings.max_upload_bytes), settings.max_upload_bytes
            )
        async with application_runtime(settings, generation=False) as state:
            await wait_ready(state.embeddings)
            documents = []
            for file in files:
                documents.append(
                    await ingest(
                        state.storage,
                        state.embeddings,
                        settings,
                        file.name,
                        read_bounded(file, settings.max_upload_bytes),
                        str(file),
                    )
                )
            return {"documents": documents}, 0
    if args.command == "logs":
        body = LogRequest(
            content=read_bounded(args.path, settings.max_upload_bytes).decode("utf-8"),
            service=args.service,
            question=args.question,
            use_model=True,
        )
        async with application_runtime(settings, embedding=False, storage=False) as state:
            await wait_ready(state.provider)
            return await analyse(body.content, body.service, body.question, True, state.provider), 0
    if args.command == "demo":
        return {
            "steps": [
                "grimoire doctor",
                "grimoire models verify",
                "grimoire serve",
                "grimoire knowledge ingest ./data/examples/",
                'grimoire knowledge search "grafana port"',
                "grimoire logs analyse ./data/examples/proxy-incident.log",
            ],
            "guide": "Use docs/demo.md for public sample data and expected evidence. "
            "Ingest writes to the configured index. Models must be provisioned explicitly. "
            "No inference or integration has been run by this guide.",
        }, 0
    raise RuntimeError("Unknown command")


async def import_application():
    from app.main import create_app

    create_app()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        settings = Settings()
        if args.command == "serve":
            serve()
            return 0
        data, code = asyncio.run(execute(args, settings))
        emit(data, args.json)
        return code
    except (ValidationError, SettingsError) as exc:
        # Never echo invalid input values: configuration can contain sensitive data.
        errors = (
            [{"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()]
            if isinstance(exc, ValidationError)
            else [{"field": "environment", "type": "invalid_setting_encoding"}]
        )
        emit(
            {
                "checks": [
                    {
                        "check": "configuration",
                        "status": "BLOCKED" if args.command == "validate" else "FAIL",
                        "detail": errors,
                    }
                ]
            },
            getattr(args, "json", False),
        )
        return 2
    except (OSError, RuntimeError, DocumentError, ProviderError, ValueError, sqlite3.Error) as exc:
        emit({"error": str(exc)}, getattr(args, "json", False))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
