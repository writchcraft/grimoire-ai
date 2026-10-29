# Reproducible homelab demo

Use the fictional files in [data/examples](../data/examples). They contain documentation addresses and no credentials. Grimoire does not connect to these services or apply their configuration.

## Start and configure

Follow [setup](setup.md) to supply separate generation and pooled embedding GGUF files, then run `docker compose up -d --build`. Open [Grimoire](http://127.0.0.1:7864/). Both subsystems should become ready in status. Native models load once per process; missing weights produce setup errors instead of downloads. Ollama is optional.

For a model-free preview, install the base development dependencies and run `python scripts/dev.py`. The interface, inventory, search of existing data and deterministic logs/incidents work; new ingestion and model answers require weights. Degraded status does not establish successful model validation.

## Evidence walkthrough

1. In **Knowledge**, upload `monitoring.md`, `reverse-proxy.yaml` and `database.json`. Keep default metadata. Re-upload `monitoring.md`: the document count should stay at three. Changing content or metadata creates another version; delete obsolete versions explicitly.
2. Search for `3000`. Expect the monitoring endpoint excerpt. Search does not use generation or embeddings.
3. In **Assistant → Indexed knowledge**, ask “What port does Grafana use?” Expand retrieved sources. Expected answer: TCP **3000**, with an `[S…]` label corresponding to `monitoring.md` under **Endpoints**. Compare the excerpt and line range with the original file. Labels are request-local and ranking depends on the embedding model.
4. Ask “How is PostgreSQL configured?” Expected evidence is `database.json`: TCP **5432**, `max_connections: 100`, `shared_buffers: 256MB`. Ask “How do I restart my reverse proxy?” Expected evidence is the advisory command and interruption warning in `reverse-proxy.yaml`. Grimoire must not execute it.
5. Ask “What is the Grafana administrator password?” It is absent. Even if relevant chunks are retrieved, the answer should say evidence is insufficient. Ask “Who won the lunar chess cup?” Below-threshold retrieval refuses without generation; a related hit still needs a model to acknowledge missing facts. Record unsupported answers as failures. Do not lower the threshold just to force an answer.
6. In **Logs & incidents**, open `proxy-incident.log`. Leave **Add local model explanation** unchecked and choose **Analyse logs**. Expect **7 lines, 3 errors, 1 warning**, with error evidence at lines **3, 4 and 6**, and a repeated upstream-refusal pattern. These counts describe supplied text, not a live outage.
7. Add notes from `incident-notes.txt`, title the report “Demo: upstream connection failures”, then **Create incident** and download Markdown. Check the observed window, evidence lines, operator notes and uncertainty. The cause remains undetermined. With generation ready, optionally repeat with model explanation and verify hypotheses against excerpts.
8. In **Services**, add “Demo Grafana”, kind **Monitoring**, hostname `monitoring.lab.example`, address `192.0.2.20`, port `3000`, and notes “Fictional sample; no live discovery.” Search for it in Knowledge. Inventory is searchable but is not automatically embedded into knowledge chat.

## Persistence and deployment acceptance

Run `docker compose restart`, refresh the UI, and repeat document listing, lexical search, a cited answer and inventory lookup. SQLite persists documents, chunks, vectors and services; Qdrant rebuilds in memory. Delete a test document and verify it disappears from document, lexical and semantic results. Use only fictional data for screenshots.

On a Docker host, record `docker compose config --quiet`, `docker compose build`, `docker compose ps`, health status and backend logs. Inspect the backend mount: `/models` must be read-only and point at your host weights; the image must contain no weights. Restart must preserve the named data volume. Check `docker compose --profile ollama config --quiet` separately; normal startup must not start Ollama. These are acceptance instructions, not claims that Docker passed here.

After packages/images/models are present, repeat with outbound access denied in an isolated deployment. Record unexpected DNS/HTTP attempts. Do not disconnect a shared workstation. The [validation record](validation.md) distinguishes automated network-denied checks from a disconnected real-model deployment.

## Record real-model results

Record generation/embedding filenames and version hashes, file sizes, binding/backend version, context sizes, threads, GPU layers, hardware and load behavior. Measure process memory before/after loading where practical. Try two requests and a stream, stop and retry, and test missing/corrupt weights and context overflow. Report timings as observations on that host, not general performance claims. No weights are bundled or downloaded by this walkthrough.
