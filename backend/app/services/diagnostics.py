"""Read-only diagnostics: no inference, model downloads, or host changes."""

import importlib.util
import ipaddress
import os
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlparse

from app.ai.catalog import load_registry
from app.ai.registry import embedding_provider, generation_provider
from app.services.runtime import embedding_identity
from app.services.serving import frontend_directory
from app.services.storage import Storage


async def inspect_models(settings):
    result = []
    for role, factory in (("generation", generation_provider), ("embedding", embedding_provider)):
        entry = None
        runtime_settings = settings
        if role == "generation" and settings.model_registry_path:
            registry = load_registry(settings)
            entry = registry.get(registry.routing.default)
            runtime_settings = settings.model_copy(
                update={
                    "ai_provider": entry.provider,
                    "native_model_path": entry.model_path,
                    "model": entry.model_name or entry.id,
                    "native_context_size": entry.context_size,
                    "max_output_tokens": entry.output_limit,
                }
            )
        runtime = factory(runtime_settings)
        try:
            status = dict(await runtime.health())
            status["role"] = role
            path = getattr(runtime, "path", None)
            status.update(
                path=str(path) if path else None,
                filename=path.name if path else None,
                size_bytes=None,
                appears_gguf=None,
                inference_verified=False,
            )
            if status["provider"] == "native":
                if path:
                    try:
                        if not stat.S_ISREG(path.stat().st_mode):
                            raise OSError("Expected a regular model file")
                        with path.open("rb") as stream:
                            status["appears_gguf"] = stream.read(4) == b"GGUF"
                            status["size_bytes"] = os.fstat(stream.fileno()).st_size
                        plausible = status["appears_gguf"] and path.suffix.lower() == ".gguf"
                        status["state"] = "unverified" if plausible else "failed"
                        status["reason"] = (
                            "Readable GGUF header; loading/inference not tested."
                            if plausible
                            else "Expected a .gguf file with a GGUF header."
                        )
                    except FileNotFoundError:
                        status.update(
                            state="model_missing", reason="Configured GGUF file is missing."
                        )
                    except OSError:
                        status.update(
                            state="failed", reason="Model is not a readable regular file."
                        )
                status["runtime_installed"] = importlib.util.find_spec("llama_cpp") is not None
            if entry:
                status.update(
                    id=entry.id,
                    display_name=entry.display_name,
                    capabilities=sorted(entry.capabilities),
                    resource_class=entry.resource_class,
                )
                availability = entry.availability()
                if availability != "unverified":
                    status.update(state=availability, reason="Registry model is unavailable.")
            result.append(status)
        finally:
            await runtime.close()
    return result


def docker_checks():
    executable = shutil.which("docker")
    if not executable:
        return [
            (
                "docker",
                "SKIP",
                "Docker validation unavailable because the Docker CLI is not installed.",
            )
        ]
    checks = [("docker_cli", "PASS", "Docker CLI is installed.")]
    try:
        result = subprocess.run(
            [executable, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        checks.append(
            (
                "docker_daemon",
                "PASS" if result.returncode == 0 else "WARN",
                "Docker daemon responded."
                if result.returncode == 0
                else "Docker daemon unavailable or access denied.",
            )
        )
    except (OSError, subprocess.TimeoutExpired):
        checks.append(("docker_daemon", "WARN", "Docker daemon check unavailable or timed out."))
    return checks


async def diagnose(settings):
    checks = []

    def add(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})

    add("python", "PASS" if sys.version_info >= (3, 12) else "FAIL", sys.version.split()[0])
    add("configuration", "PASS", "Application settings validated.")
    directory = settings.data_dir.resolve()
    ancestor = directory
    while not ancestor.exists():
        ancestor = ancestor.parent
    accessible = ancestor.is_dir() and os.access(ancestor, os.R_OK | os.W_OK | os.X_OK)
    add(
        "data_directory",
        "PASS" if accessible and directory.exists() else "WARN" if accessible else "FAIL",
        f"{directory}; "
        + (
            "accessible"
            if directory.exists() and accessible
            else "not created; parent writable"
            if accessible
            else "permission/path problem"
        ),
    )
    try:
        detail = Storage.inspect(settings.data_dir, embedding_identity(settings))
        add("database", "PASS" if detail["exists"] else "WARN", detail["detail"])
    except (OSError, RuntimeError) as exc:
        add("database", "FAIL", str(exc))
    add(
        "vector_store",
        "SKIP",
        "Embedded Qdrant rebuild not executed by read-only doctor; "
        "validate exercises a temporary index. Configured index accessibility remains unverified.",
    )
    models = await inspect_models(settings)
    for model in models:
        configured = model["state"] in {"ready", "unverified"}
        add(
            model["role"],
            "PASS" if configured else "FAIL",
            f"{model['provider']}: {model.get('model')}; "
            f"{model.get('reason') or 'Endpoint and configured model available.'}",
        )
        if model["provider"] == "native":
            add(
                model["role"] + "_runtime",
                "PASS" if model["runtime_installed"] else "FAIL",
                "Native binding discoverable; loading unverified."
                if model["runtime_installed"]
                else "Native binding missing; install the documented native extra explicitly.",
            )
    if "ollama" not in {settings.ai_provider, settings.embedding_provider}:
        add("ollama", "SKIP", "Optional Ollama is not configured; availability is not required.")
    host = urlparse(settings.ollama_url).hostname
    local = host == "localhost"
    try:
        local = local or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    uses_ollama = "ollama" in {settings.ai_provider, settings.embedding_provider}
    add(
        "network_policy",
        "WARN" if uses_ollama and not local else "PASS",
        "Ollama endpoint is non-loopback; locality/cloud policy cannot be verified."
        if uses_ollama and not local
        else "Configured AI uses local files or a loopback endpoint.",
    )
    add(
        "backend",
        "PASS" if settings.allowed_hosts else "FAIL",
        f"Allowed hosts: {', '.join(settings.allowed_hosts)}",
    )
    frontend = frontend_directory()
    add(
        "frontend",
        "PASS" if (frontend / "index.html").is_file() else "WARN",
        f"Local web assets: {frontend}; serve uses the same origin for UI/API.",
    )
    port = os.environ.get("GRIMOIRE_PORT", "7864")
    add(
        "serve_port",
        "PASS" if port.isdecimal() and 1 <= int(port) <= 65535 else "FAIL",
        f"GRIMOIRE_PORT={port}",
    )
    from app.services.windows_events import channels

    for channel in channels()["channels"]:
        add(
            "Windows Event Log / " + channel["channel"],
            "PASS" if channel["status"] == "available" else "WARN",
            "readable"
            if channel["status"] == "available"
            else channel.get("reason", "unavailable"),
        )
    from app.services.host import inspect_host

    for section in ("system", "disks", "network", "services"):
        result = inspect_host(section)
        add(
            "Windows Host / " + section,
            "PASS" if result["status"] == "available" else "WARN",
            "readable" if result["status"] == "available" else "; ".join(result["issues"]),
        )
    for row in docker_checks():
        add(*row)
    return {"checks": checks, "models": models}
