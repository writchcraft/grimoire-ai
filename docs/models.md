# Generation models and deterministic selection

Set `GRIMOIRE_MODEL_REGISTRY_PATH=./docs/models.example.yaml` to use the checked-in example, or copy it to a local configuration file. Paths inside the registry resolve relative to that file. No weights are downloaded. Existing single-model environment configuration still works through a compatibility entry named `default`; embedding settings remain separate.

The YAML/JSON document has a `models` list and a `routing` object. Entries contain a unique stable `id`, `display_name`, `provider` (`native` or explicitly configured `ollama`), `model_path` (native) or `model_name` (Ollama), task `capabilities`, `context_size`, `output_limit`, `enabled`, `resource_class` (`light`, `medium`, `heavy`), optional `tags`, and `readiness`. General generation respects the lower of the global output limit and the entry limit; structured validators retain their existing request caps, bounded by the entry limit. The legacy adapter preserves the existing 512-token factual contract even when the general prose setting is 384. Unknown fields, duplicate IDs/YAML keys, embedding capabilities, bad limits and unknown policy/fallback references are rejected. `readiness: auto` means inspect availability and verify the runtime on first use; it does not assert readiness. Operators can explicitly quarantine a model with `failed`, `incompatible` or `loading`. Repair configuration and restart to clear quarantines or cached load failures.

```yaml
routing:
  default: qwen-small
  policies: {}
  fallbacks:
    qwen-large: [qwen-small]
```

The shipped policy deliberately keeps every Auto task on the configured light default. Models are never selected by size, benchmark score or an LLM. A policy such as `config_reasoning: qwen-large` under `policies` is an explicit experiment: it requires that registered model to declare that capability. Resource classes describe cost; explicit mappings, default and ordered fallback lists express the operator's resource preference. The router does not scan for arbitrary available alternatives or automatically rank models by parameter count. This avoids paying for an unproven capability advantage. The example's medium 3B entry is available for manual experiments, with no automatic promotion.

Task names are `general`, `rag`, `logs`, `incidents`, `config_explanation`, `config_reasoning`, `code_reasoning` and `summarisation`. The API uses endpoint/mode metadata, not keyword guesses or model interpretation: knowledge chat is always `rag`, logs are `logs`, incidents are `incidents`. General chat can explicitly declare a task using its `task` field. Configuration and code reasoning in general mode remain unvalidated prose; this milestone does not claim a new evidence validation contract for them. Indexed configuration QA remains `rag` and retains every existing safeguard.

Selection tries the preferred model, its explicitly ordered fallback list, then the default, deduplicated. It checks capability, enabled state, native file/header and runtime readiness. The list is bounded and non-recursive even if configuration contains cycles. No generation request is replayed after an inference/validation failure. Unknown manual IDs fail clearly; a known unavailable override may use the configured fallback/default and records that decision. No arbitrary model, implicit Ollama provider or cloud fallback is allowed. If no candidate is usable, inference fails clearly; evidence validators may represent provider errors as their existing rejected-output response. Read-only status does not prove inference readiness.

Generation loads lazily. A single serialized owner holds the provider through each complete generation/stream, closes the previous generation model before loading another, and drains cancelled native loading/inference before releasing ownership. Providers handle execution; the registry contains descriptions and the router translates selected entries into provider settings. There are no retained provider references in the validation services. Native llama.cpp remains first-class. Embeddings may remain loaded alongside the one generation model, and their identity/index compatibility rules are unchanged. No embedding model can appear in this generation registry.

For optional Ollama generation, the router sends an explicit unload request before closing a generation client and switching. If unloading fails, switching aborts. This controls Grimoire's own generation models only: an independently managed daemon or other clients may retain other models. Use native execution for strict process-local ownership; no daemon-wide GPU scheduling or embedding unloading is attempted.

```powershell
$env:GRIMOIRE_MODEL_REGISTRY_PATH = './docs/models.example.yaml'
grimoire models list --json
grimoire models inspect qwen-large --json
grimoire models policy --json
grimoire models test qwen-small --json
grimoire chat 'What port does Grafana use?' --model qwen-small --debug --json
grimoire chat 'Explain this configuration...' --mode general --task config_reasoning
grimoire logs analyse ./data/examples/proxy-incident.log --model qwen-small --json
grimoire evaluate --suite all --model auto --json
```

Existing `models inspect --role ...` and `models verify --role ...` commands remain available. Registry-aware status/doctor inspect the default generation entry. `models test` explicitly performs inference and reports the actual selected model/fallback; it is not a quality benchmark. List/inspect/policy never load weights. Chat uses the same preparation and factual validation services as HTTP; general mode must be explicitly requested.

HTTP chat/log/incident bodies accept `model: "auto"` or a registered ID. Chat additionally accepts the task declaration above. `debug: true` includes concise routing metadata; default answer payloads omit it. Internal structured logs record task, selected ID, policy rule, fallback attempts, provider and readiness; no evidence/prompt text is added. Requests answered/refused without generation record `no generation required`. Debug state is scoped to each asynchronous request. The status bar shows Auto plus the last selected display name, or the manually selected model. A collapsed Model control in the composer offers Auto and configured models and also applies to model-assisted logs/incidents. It is a selection control, not a management dashboard.

The 3B model previously matched supported aggregate QA but had worse oracle QA, greater latency/memory and different failure modes (see the existing model-comparison artifacts). Declaring a capability is a configuration assertion, not proof of superiority. See [routing validation and value](routing/report.md) for this milestone's measured outcomes and limitations.
