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

from app.ai.catalog import load_registry
from app.ai.registry import embedding_provider, generation_provider
from app.ai.routing import RoutedProvider, routing_scope
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
    host = commands.add_parser("host", help="Read-only local Windows host inspection")
    host_actions = host.add_subparsers(dest="action", required=True)
    for action in ("status", "system", "resources", "disks", "network", "services", "history"):
        command = host_actions.add_parser(action)
        command.add_argument("--json", action="store_true")
        if action == "services":
            command.add_argument("service", nargs="?")
            command.add_argument("--name")
            command.add_argument("--state")
            command.add_argument("--start-type")
            command.add_argument("--limit", type=int, default=100)
    services = commands.add_parser("services", help="Manual read-only HTTP service monitoring")
    actions = services.add_subparsers(dest="action", required=True)
    for action in ("list", "status", "probe", "dns", "tls", "add"):
        command = actions.add_parser(action)
        command.add_argument("--json", action="store_true")
        if action in {"status", "probe", "dns", "tls"}:
            command.add_argument("service", nargs="?", help="Exact service name or ID")
        if action == "add":
            command.add_argument("path", type=Path, help="Service configuration JSON file")
    events = commands.add_parser("events", help="Read-only local Windows Event Log inspection")
    actions = events.add_subparsers(dest="action", required=True)
    for action in ("channels", "recent", "search", "inspect", "analyse"):
        command = actions.add_parser(action)
        command.add_argument("--json", action="store_true")
        if action == "channels":
            continue
        command.add_argument(
            "--channel", choices=("System", "Application", "Security"), default="System"
        )
        command.add_argument(
            "--level",
            choices=("critical", "error", "warning", "info", "verbose", "all"),
            default="warning",
        )
        command.add_argument("--provider")
        command.add_argument("--event-id", type=int)
        command.add_argument("--start")
        command.add_argument("--end")
        command.add_argument("--around")
        command.add_argument("--minutes", type=int, default=5)
        command.add_argument("--limit", type=int, default=100)
        command.add_argument("--before-record", type=int)
        if action == "inspect":
            command.add_argument("record_id", type=int)
        if action == "analyse":
            command.add_argument("--question", default="Explain these Windows events.")
            command.add_argument("--service", default="unknown")
            command.add_argument("--use-model", action="store_true")
    backup = commands.add_parser("backup", help="Snapshot or restore application data (no secrets)")
    actions = backup.add_subparsers(dest="action", required=True)
    command = actions.add_parser("create", help="Create a consistent SQLite snapshot")
    command.add_argument("destination", type=Path)
    command.add_argument("--json", action="store_true")
    command = actions.add_parser("restore", help="Restore into a new, offline data directory")
    command.add_argument("backup", type=Path)
    command.add_argument("destination", type=Path)
    command.add_argument("--json", action="store_true")
    evaluation = commands.add_parser(
        "evaluate", help="Evaluate synthetic evidence in an isolated temporary index"
    )
    evaluation.add_argument("--json", action="store_true")
    evaluation.add_argument("--model", default="auto")
    evaluation.add_argument(
        "--no-clobber", action="store_true", help="Refuse to overwrite an existing output report"
    )
    evaluation.add_argument(
        "--suite", choices=("development", "heldout", "all"), default="development"
    )
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
    for name in ("list", "inspect", "verify", "policy", "test"):
        command = subs.add_parser(name)
        command.add_argument("--json", action="store_true")
        if name in {"inspect", "test"}:
            command.add_argument("id", nargs="?" if name == "inspect" else None)
        if name in {"inspect", "verify"}:
            command.add_argument(
                "--role", choices=("generation", "embedding", "all"), default="all"
            )
    chat = commands.add_parser("chat", help="Ask through the shared evidence validation pipeline")
    chat.add_argument("message")
    chat.add_argument("--model", default="auto")
    chat.add_argument("--mode", choices=("knowledge", "general"), default="knowledge")
    chat.add_argument(
        "--task",
        choices=(
            "general",
            "config_explanation",
            "config_reasoning",
            "code_reasoning",
            "summarisation",
        ),
        default="general",
    )
    chat.add_argument("--debug", action="store_true")
    chat.add_argument("--json", action="store_true")
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
    command.add_argument("--model", default="auto")
    command.add_argument("--json", action="store_true")
    return root


def emit(data, machine):
    if machine:
        print(json.dumps(data, default=str))
    elif "host" in data and "records" in data:
        from app.services.host import human

        print(human(data))
    elif "channels" in data:
        for channel in data["channels"]:
            print(
                f"{channel['channel']} log: {channel['status']}"
                + (f" ({channel['reason']})" if channel.get("reason") else "")
            )
    elif "records" in data and "channel" in data:
        print(
            f"{data['channel']} log: {data['status']}"
            + (f" ({data['reason']})" if data.get("reason") else "")
        )
        for event in data["records"]:
            print(
                f"{event['channel']} / {event['severity']} / {event['provider']} / "
                f"Event ID {event['event_id']} / Record {event['record_id']} / "
                f"{event['timestamp']}\n{event['message'] or 'Rendered message unavailable'}"
                f"\nEvidence: {event['evidence_id']}"
            )
        for issue in data.get("issues", []):
            print(issue)
    elif "answer" in data and "events" in data:
        print(data["answer"])
        print(json.dumps(data.get("correlations", []), indent=2))
    elif "tls_results" in data:
        for item in data["tls_results"]:
            print(f"{item['service']}\nTLS\nStatus: {item['state'].upper()}")
            for key in (
                "hostname",
                "port",
                "issuer",
                "expires_at",
                "remaining_days",
                "hostname_match",
                "trust",
                "trust_reason",
                "validity",
                "protocol",
                "observed_at",
                "reason",
            ):
                print(f"{key.replace('_', ' ').title()}: {item.get(key, 'unavailable')}")
    elif "dns_results" in data:
        for item in data["dns_results"]:
            print(
                f"{item['service']}\nHostname: {item['hostname']}\nStatus: {item['status'].upper()}"
            )
            print(f"A: {', '.join(item['a']) or 'none'}\nAAAA: {', '.join(item['aaaa']) or 'none'}")
            print(
                f"Canonical: {item['canonical_name'] or 'none'}\nLatency: {item['latency_ms']} ms"
            )
            print(f"Observed: {item['observed_at']}\n{item['reason']}")
    elif "service_results" in data:
        for item in data["service_results"]:
            latest = item.get("latest") or item
            primary = latest.get("endpoint", {})
            print(
                f"{item.get('name', latest.get('service', item.get('service_id', '')))}: "
                f"{item.get('state', 'unknown')} | "
                f"HTTP {primary.get('status_code')} | {primary.get('latency_ms')} ms | "
                f"{primary.get('error') or latest.get('checked_at', 'not checked')}"
            )
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
    if args.command == "host":
        from app.services.host import ServiceQuery, history, inspect_and_retain

        if args.action == "history":
            return {"host_history": history(settings.data_dir)}, 0
        query = None
        if args.action == "services":
            query = ServiceQuery(
                name=args.service or args.name,
                state=args.state,
                start_type=args.start_type,
                limit=args.limit,
            )
        result = inspect_and_retain(
            settings, args.action, query, bool(getattr(args, "service", None))
        )
        return result, 0 if result["status"] != "unavailable" else 1
    if args.command == "events":
        from app.services.windows_events import EventQuery, analyse_events, channels, read_events

        if args.action == "channels":
            return channels(), 0
        query = EventQuery(
            **{key: getattr(args, key) for key in EventQuery.model_fields if hasattr(args, key)}
        )
        if args.action == "inspect":
            query = EventQuery(channel=args.channel, record_id=args.record_id, level="all", limit=1)
        result = read_events(query)
        if args.action == "analyse":
            async with application_runtime(
                settings, generation=args.use_model, embedding=False
            ) as state:
                with routing_scope("logs"):
                    analysis = await analyse_events(
                        result["records"],
                        args.question,
                        getattr(state, "provider", None),
                        args.use_model,
                        service=args.service,
                        storage=state.storage,
                    )
                return {
                    **analysis,
                    "retrieval": {k: v for k, v in result.items() if k != "records"},
                }, 0
        return result, 0
    if args.command == "services":
        from app.models import ServiceInput
        from app.services.live import probe_and_record

        async with application_runtime(settings, generation=False, embedding=False) as state:
            store = state.storage
            if args.action == "add":
                target = ServiceInput.model_validate_json(read_bounded(args.path, 32_768))
                return store.save_service(target), 0
            targets = (
                [store.service(args.service)]
                if getattr(args, "service", None)
                else store.services()
            )
            if any(s is None for s in targets):
                raise ValueError("Service not found")
            if args.action == "list":
                return {"services": targets}, 0
            if args.action == "tls":
                from app.services.tls import inspect_and_record, inspect_many

                results = (
                    [await inspect_and_record(store, targets[0])]
                    if getattr(args, "service", None)
                    else await inspect_many(store, targets)
                )
                return {"tls_results": results}, 0
            if args.action == "dns":
                from app.services.dns import inspect_and_record, inspect_many

                results = (
                    [await inspect_and_record(store, targets[0])]
                    if getattr(args, "service", None)
                    else await inspect_many(store, targets)
                )
                return {"dns_results": results}, 0
            if args.action == "probe":
                if not getattr(args, "service", None):
                    targets = [s for s in targets if s.get("monitor")]
                if len(targets) > 50:
                    raise ValueError("Batch limit is 50 services; probe individually")
                return {"service_results": [await probe_and_record(store, s) for s in targets]}, 0
            return {
                "service_results": [
                    {
                        "name": s["name"],
                        "state": (store.observations(s["id"], 1) or [{"state": "unknown"}])[0][
                            "state"
                        ],
                        "latest": (store.observations(s["id"], 1) or [None])[0],
                    }
                    for s in targets
                ]
            }, 0
    if args.command == "backup":
        from app.services.backup import create_backup, restore_backup

        result = (
            create_backup(settings.data_dir, args.destination)
            if args.action == "create"
            else restore_backup(args.backup, args.destination)
        )
        return result, 0
    if args.command == "chat":
        from app.models import ChatRequest
        from app.rag.retrieval import NO_EVIDENCE, prepare_chat
        from app.services.grounding import grounded_answer

        body = ChatRequest(message=args.message, mode=args.mode, task=args.task, model=args.model)
        from app.services.live import live_answer, plan_live

        if body.mode == "knowledge":
            async with application_runtime(settings, generation=False, embedding=False) as state:
                from app.services.windows_events import event_chat

                event_result = await event_chat(body, state.storage)
                if event_result is not None:
                    return {
                        **event_result,
                        "live": [e.model_dump() for e in event_result["live"]],
                    }, 0
                if plan_live(body.message, state.storage.services()) is not None:
                    result = await live_answer(body, state.storage)
                    return {**result, "live": [e.model_dump() for e in result["live"]]}, 0
        knowledge = body.mode == "knowledge"
        async with application_runtime(settings, embedding=knowledge, storage=knowledge) as state:
            if knowledge:
                await wait_ready(state.embeddings)
            with routing_scope("rag" if body.mode == "knowledge" else body.task, body.model):
                state.provider.registry.candidates(
                    "rag" if body.mode == "knowledge" else body.task, body.model
                )
                messages, sources = await prepare_chat(
                    body,
                    getattr(state, "storage", None),
                    getattr(state, "embeddings", None),
                    settings,
                )
                result = (
                    await grounded_answer(state.provider, messages, sources, body.message)
                    if messages and body.mode == "knowledge"
                    else {
                        "answer": await state.provider.chat(messages) if messages else NO_EVIDENCE
                    }
                )
                result = {**result, "sources": [s.model_dump() for s in result.get("sources", [])]}
                if args.debug:
                    result["routing"] = state.provider.last_route
                return result, 0
    if args.command == "evaluate":
        from app.evaluation.runner import evaluate

        if args.no_clobber and args.output and args.output.exists():
            raise ValueError("Output report already exists; choose a new path")
        results = []
        for name in ("development", "heldout") if args.suite == "all" else (args.suite,):
            results.append(
                await evaluate(
                    settings,
                    suite_name=name,
                    retrieval_only=args.retrieval_only,
                    compare_legacy=args.compare_legacy,
                    limit_probe=args.limit_probe,
                    progress=lambda case: print(f"Evaluated {case}", file=sys.stderr, flush=True),
                )
            )
        result = results[0] if args.suite != "all" else {"suites": results}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x" if args.no_clobber else "w", encoding="utf-8") as stream:
                stream.write(json.dumps(result, indent=2))
        failed = any(
            "FAIL" in value
            for report in results
            for key, value in report["summary"].items()
            if key != "retrieval"
        )
        missed = any(
            report["summary"]["retrieval"]["hit@5"]["found"]
            < report["summary"]["retrieval"]["hit@5"]["total"]
            for report in results
        )
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
        if args.action == "policy":
            registry = load_registry(settings)
            return registry.routing.model_dump(), 0
        if args.action == "test":
            runtime = RoutedProvider(settings)
            try:
                model = runtime.registry.get(args.id)
                with routing_scope(sorted(model.capabilities)[0], args.id):
                    output = await runtime.chat([{"role": "user", "content": "Reply with OK."}])
                return {"output": output, "routing": runtime.last_route}, 0
            finally:
                await runtime.close()
        if args.action in {"list", "inspect"} and (
            settings.model_registry_path or getattr(args, "id", None)
        ):
            registry = load_registry(settings)
            models = [registry.get(args.id)] if getattr(args, "id", None) else registry.models
            return {
                "generation_models": [
                    {
                        **m.model_dump(mode="json"),
                        "state": m.availability(),
                        "default": m.id == registry.routing.default,
                    }
                    for m in models
                ],
                "routing": registry.routing.model_dump(),
            }, 0
        if args.action != "verify":
            models = await inspect_models(settings)
            return {
                "models": [m for m in models if getattr(args, "role", "all") in {"all", m["role"]}]
            }, 0
        results = []
        for role, factory in (
            ("generation", RoutedProvider if settings.model_registry_path else generation_provider),
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
                raise DocumentError("Query must contain 1â€“500 characters.")
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
                "Supply 1â€“512 supported files; directory ingestion is non-recursive."
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
            with routing_scope("logs", args.model):
                await wait_ready(state.provider)
                return await analyse(
                    body.content, body.service, body.question, True, state.provider
                ), 0
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
        with routing_scope("general", getattr(args, "model", "auto")):
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
