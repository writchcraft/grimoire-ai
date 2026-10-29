import { eventText, eventReference, eventFilters } from "./events.js";
import { hostCards, diskText, interfaceText, serviceText, hostContextText } from "./host.js";
import { tlsText } from "./tls.js";
import { dnsText } from "./dns.js";
import { api, jsonApi, readEvents, modelStatus } from "./api.js";

const $ = (selector) => document.querySelector(selector);
let history = [];
let controller = null;
let reportUrl = null;
const titles = {
  host: "This host",
  chat: "Assistant",
  knowledge: "Knowledge",
  logs: "Logs & incidents",
  services: "Services",
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notice(message = "") {
  $("#notice").textContent = message;
  $("#notice").hidden = !message;
}
function showView(view) {
  for (const section of document.querySelectorAll(".view"))
    section.hidden = section.id !== `view-${view}`;
  for (const nav of document.querySelectorAll("[data-view]")) {
    nav.classList.toggle("active", nav.dataset.view === view);
    if (nav.dataset.view === view) nav.setAttribute("aria-current", "page");
    else nav.removeAttribute("aria-current");
  }
  $("#page-title").textContent = titles[view];
  notice();
  if (view === "knowledge") loadDocuments().catch((e) => notice(e.message));
  if (view === "services") loadServices().catch((e) => notice(e.message));
}
document
  .querySelectorAll("[data-view]")
  .forEach((button) =>
    button.addEventListener("click", () => showView(button.dataset.view)),
  );
document
  .querySelectorAll("[data-go]")
  .forEach((button) =>
    button.addEventListener("click", () => showView(button.dataset.go)),
  );
document.querySelectorAll("[data-prompt]").forEach((button) =>
  button.addEventListener("click", () => {
    $("#chat-input").value = button.dataset.prompt;
    $("#chat-input").focus();
  }),
);

async function refreshStatus() {
  try {
    const status = await jsonApi("/api/status");
    const ready = status.status === "ready";
    $("#connection-dot").classList.toggle("ready", ready);
    const display = modelStatus(status);
    const selector = $("#generation-model");
    const previous = selector.value;
    selector.replaceChildren(new Option("Auto", "auto"));
    for (const model of status.generation_models || []) {
      selector.add(new Option(model.display_name, model.id));
    }
    if ([...selector.options].some(option => option.value === previous)) selector.value = previous;
    $("#connection-label").textContent = display.label;
    $("#model-tag").textContent = display.model;
    $("#nav-count").textContent = status.documents;
    $("#upload-limit").textContent =
      `${(status.upload_limit_bytes / 1048576).toFixed(1)} MiB`;
    $("#connection-label").title = display.detail;
    $("#runtime-detail").textContent = display.detail;
    $("#runtime-detail").hidden = ready;
  } catch {
    $("#connection-label").textContent = "Backend unavailable";
    $("#model-tag").textContent = "Backend unavailable";
    $("#runtime-detail").textContent =
      "Cannot reach the backend. Check the server, then refresh connection status.";
    $("#runtime-detail").hidden = false;
    $("#connection-dot").classList.remove("ready");
  }
}
$("#refresh-status").addEventListener("click", refreshStatus);

function addMessage(role, content) {
  $("#welcome").hidden = true;
  const node = element("article", `message ${role}`);
  const header = element("div", "message-head");
  header.append(
    element("span", "message-icon", role === "user" ? "U" : "◈"),
    element("span", "", role === "user" ? "You" : "Grimoire"),
  );
  const body = element("div", "message-body", content);
  node.append(header, body);
  $("#conversation").append(node);
  scrollChat();
  return { node, body };
}
function scrollChat() {
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
}
function attachSources(node, sources) {
  if (!sources.length) return;
  const details = element("details", "sources");
  details.append(
    element(
      "summary",
      "",
      `${sources.length} retrieved source${sources.length === 1 ? "" : "s"} · inspect evidence`,
    ),
  );
  for (const source of sources) {
    const item = element("div", "source");
    item.append(
      element("strong", "", `[${source.label}] ${source.filename}`),
      element(
        "small",
        "",
        source.evidence_type === "live_observation"
          ? `LIVE OBSERVATION · ${source.observation.checked_at} · ${source.observation.endpoint.url}`
          : `INDEXED DOCUMENTATION · ${source.source_path} · lines ${source.line_start}–${source.line_end} · chunk ${source.chunk_number}`,
      ),
      element("pre", "", source.text),
    );
    details.append(item);
  }
  node.append(details);
}
$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("#chat-input").value.trim();
  if (!message || controller) return;
  notice();
  const mode = $("#chat-mode").value;
  addMessage("user", message);
  $("#chat-input").value = "";
  const assistant = addMessage("assistant", "Searching for context…");
  controller = new AbortController();
  $("#send-chat").hidden = true;
  $("#stop-chat").hidden = false;
  $("#chat-mode").disabled = true;
  $("#clear-chat").disabled = true;
  let answer = "",
    hostEventAnswer = false,
    completed = false;
  try {
    const response = await api("/api/chat", {
      method: "POST",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        model: $("#generation-model").value,
        mode,
        history: history.slice(-12),
        stream: true,
      }),
    });
    for await (const item of readEvents(response)) {
      if (item.type === "sources") {
        assistant.body.textContent = "Thinking locally…";
        attachSources(assistant.node, item.sources);
      } else if (item.type === "token") {
        answer += item.content;
        assistant.body.textContent = answer;
      } else if (item.type === "live") {
        hostEventAnswer ||= item.live.some(e => e.evidence_type === "windows_event");
        const details = element("details", "sources");
        details.append(element("summary", "", "LIVE OBSERVATION - evidence"));
        for (const observation of item.live) details.append(element("pre", "report", JSON.stringify(observation, null, 2)));
        assistant.node.append(details);
      } else if (item.type === "interpretation") {
        const section = element("div", "panel");
        section.append(element("strong", "", "AI INTERPRETATION - unverified suggestions"), element("p", "", item.content));
        assistant.node.append(section);
      } else if (item.type === "error") throw new Error(item.detail);
      else if (item.type === "done") completed = true;
      scrollChat();
    }
    if (!completed)
      throw new Error("The response ended before completion. Please retry.");
    await refreshStatus();
    // Bound individual history turns to the API schema; long answers stay visible in the UI.
    // Host evidence stays out of later model routes, which may use other adapters.
    if (!hostEventAnswer) history.push(
      { role: "user", content: message },
      { role: "assistant", content: answer.slice(-8000) },
    );
  } catch (error) {
    assistant.node.classList.add("error");
    assistant.body.textContent =
      (answer ? answer + "\n\n" : "") +
      (error.name === "AbortError" ? "Response stopped." : error.message);
  } finally {
    controller = null;
    $("#send-chat").hidden = false;
    $("#stop-chat").hidden = true;
    $("#chat-mode").disabled = false;
    $("#clear-chat").disabled = false;
    scrollChat();
  }
});
$("#chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});
$("#stop-chat").addEventListener("click", () => controller?.abort());
$("#clear-chat").addEventListener("click", () => {
  history = [];
  document.querySelectorAll(".message").forEach((node) => node.remove());
  $("#welcome").hidden = false;
  $("#chat-input").focus();
  notice();
});
$("#chat-mode").addEventListener("change", () => {
  history = [];
});

async function loadDocuments() {
  const docs = await jsonApi("/api/documents");
  $("#nav-count").textContent = docs.length;
  $("#document-total").textContent = `${docs.length} documents`;
  const list = $("#documents-list");
  list.replaceChildren();
  if (!docs.length)
    list.append(
      element(
        "div",
        "empty",
        "No documents indexed. Add a runbook or configuration file to begin. Fictional examples are in data/examples/.",
      ),
    );
  for (const doc of docs) {
    const row = element("div", "document-row"),
      info = element("div", "document-info");
    info.append(
      element("strong", "", doc.filename),
      element(
        "small",
        "",
        `${doc.source_path} · ${doc.chunk_count} chunks · ${doc.service || "unlabelled"} · ${new Date(doc.ingested_at).toLocaleDateString()}`,
      ),
    );
    const remove = element("button", "delete-button", "Delete");
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete document ${doc.filename}`);
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        await api(`/api/documents/${doc.id}`, { method: "DELETE" });
        $("#search-results").replaceChildren();
        await loadDocuments();
      } catch (error) {
        notice(error.message);
        remove.disabled = false;
      }
    });
    row.append(element("span", "document-symbol", "▤"), info, remove);
    list.append(row);
  }
}
$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  notice();
  const file = $("#document-file").files[0];
  if (!file) return;
  const data = new FormData();
  data.append("file", file);
  if ($("#document-service").value.trim())
    data.append("service", $("#document-service").value.trim());
  $("#upload-button").disabled = true;
  $("#upload-button").textContent = "Embedding locally…";
  try {
    const result = await (
      await api("/api/documents/upload", { method: "POST", body: data })
    ).json();
    await loadDocuments();
    $("#upload-form").reset();
    notice(
      `Indexed ${result.filename}: ${result.chunk_count} chunks. Identical uploads are deduplicated.`,
    );
  } catch (error) {
    notice(error.message);
  } finally {
    $("#upload-button").disabled = false;
    $("#upload-button").textContent = "Index document ↗";
  }
});
$("#search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  notice();
  const params = new URLSearchParams({ q: $("#search-query").value });
  if ($("#search-type").value)
    params.set("document_type", $("#search-type").value);
  if ($("#search-service").value.trim())
    params.set("service", $("#search-service").value.trim());
  try {
    const result = await jsonApi(`/api/search?${params}`),
      area = $("#search-results");
    area.replaceChildren();
    if (!result.chunks.length && !result.services.length)
      area.append(
        element(
          "div",
          "empty",
          "No matching text or services. Try another term.",
        ),
      );
    for (const hit of result.chunks) {
      const card = element("article", "result-card");
      card.append(
        element("h3", "", hit.filename),
        element(
          "small",
          "",
          `${hit.source_path} · lines ${hit.line_start}–${hit.line_end}`,
        ),
        element("pre", "", hit.text),
      );
      area.append(card);
    }
    for (const service of result.services) area.append(serviceCard(service));
  } catch (error) {
    notice(error.message);
  }
});

$("#log-file").addEventListener("change", async () => {
  const file = $("#log-file").files[0];
  if (!file) return;
  if (file.size > 200000) {
    notice("For log analysis, use a UTF-8 excerpt of up to 200,000 bytes.");
    return;
  }
  try {
    $("#log-content").value = new TextDecoder("utf-8", { fatal: true }).decode(
      await file.arrayBuffer(),
    );
  } catch {
    notice("The log file must use UTF-8 text encoding.");
  }
});
$("#log-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  notice();
  const incident = event.submitter?.value === "incident";
  const body = {
    content: $("#log-content").value,
    service: $("#log-service").value.trim() || "unknown",
    question:
      $("#log-question").value.trim() ||
      "Explain the failures and suggest investigation steps.",
    use_model: $("#use-model").checked,
    model: $("#generation-model").value,
  };
  if (incident)
    Object.assign(body, {
      title: $("#incident-title").value.trim() || "Homelab incident",
      notes: $("#incident-notes").value,
      event_selection: [...selectedWindowsEvents.values()],
      include_host: $("#incident-host").checked,
    });
  const buttons = [...$("#log-form").querySelectorAll("button")];
  buttons.forEach((b) => (b.disabled = true));
  try {
    const result = await jsonApi(
      incident ? "/api/incidents/summarise" : "/api/logs/analyse",
      body,
    );
    const analysis = incident ? result.analysis : result;
    await refreshStatus();
    const area = $("#log-results");
    area.replaceChildren();
    const stats = element("div", "stat-row");
    for (const [key, label] of [
      ["line_count", "Lines inspected"],
      ["error_count", "Error matches"],
      ["warning_count", "Warning matches"],
    ]) {
      const stat = element("div", "stat");
      stat.append(
        element("strong", "", analysis[key]),
        element("span", "", label),
      );
      stats.append(stat);
    }
    area.append(stats);
    const report = element("div", "panel");
    if (incident) {
      if (reportUrl) URL.revokeObjectURL(reportUrl);
      reportUrl = URL.createObjectURL(
        new Blob([result.markdown], { type: "text/markdown;charset=utf-8" }),
      );
      const download = element("a", "text-button", "Download incident.md ↗");
      download.href = reportUrl;
      download.download = "incident.md";
      report.append(download, element("pre", "report", result.markdown));
    } else {
      report.append(
        element("h2", "", "Observed evidence"),
        element(
          "pre",
          "report",
          analysis.events
            .map((e) => `L${e.line} [${e.severity}] ${e.text}`)
            .join("\n") || "No error or warning patterns matched.",
        ),
        element("h2", "", "Repeated patterns"),
        element(
          "pre",
          "report",
          analysis.repeated_patterns
            .map((p) => `${p.count}× ${p.pattern}`)
            .join("\n") || "No repeated error/warning patterns.",
        ),
        element("p", "helper", analysis.limitations),
      );
      if (analysis.events_truncated)
        report.append(
          element(
            "p",
            "helper",
            "Event excerpts capped at 80 entries; counts cover all supplied lines.",
          ),
        );
      if (analysis.explanation)
        report.append(
          element("h2", "", "Evidence-backed interpretation"),
          element("pre", "report", analysis.explanation),
        );
      if (analysis.suggested_investigation?.length)
        report.append(
          element("h2", "", "Suggested investigation"),
          element("pre", "report", analysis.suggested_investigation.join("\n")),
        );
    }
    area.append(report);
  } catch (error) {
    notice(error.message);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
});

function serviceCard(service, allowDelete = false) {
  const card = element("article", "panel service-card"),
    heading = element("div", "panel-heading");
  heading.append(
    element("h2", "", service.name),
    element("span", "badge", service.kind.toUpperCase()),
  );
  card.append(heading);
  for (const [label, value] of [
    ["Host", service.hostname],
    ["Address", service.ip_address],
    ["Ports", service.ports.join(", ")],
    ["OS", service.operating_system],
    ["Depends on", service.dependencies.join(", ")],
    ["Notes", service.notes],
  ]) {
    if (value) {
      const p = element("p");
      p.append(
        element("strong", "", `${label}: `),
        document.createTextNode(value),
      );
      card.append(p);
    }
  }
  for (const url of service.urls) {
    const p = element("p"),
      a = element("a", "", url);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    p.append(a);
    card.append(p);
  }
  const tags = element("div", "tags");
  service.tags.forEach((tag) => tags.append(element("span", "tag", tag)));
  card.append(tags);
  if (service.local_windows_service) {
    card.append(element("p", "", `Local Windows service: ${service.local_windows_service}`));
  }
  if (allowDelete && service.monitor) {
    const area = element("div", "service-health");
    const render = (health) => {
      area.replaceChildren();
      area.append(element("strong", `health-${health.state}`, `● ${health.state.toUpperCase()}`));
      const observation = health.latest;
      if (observation) {
        for (const [label, endpoint] of [["HTTP", observation.endpoint], ["Health endpoint", observation.health_endpoint]]) {
          if (endpoint) area.append(element("p", "", `${label}: ${endpoint.status_code ?? "No response"} · ${endpoint.latency_ms} ms${endpoint.error ? ` · ${endpoint.error.replaceAll("_", " ")}` : ""}`));
        }
        area.append(element("p", "", `Checked ${new Date(observation.checked_at).toLocaleString()} · saved observation`));
      } else area.append(element("p", "", "Not checked yet"));
    };
    render(service.health || { state: "unknown" });
    const button = element("button", "primary-button", "Probe ↗");
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const latest = await jsonApi(`/api/services/${service.id}/probe`, {});
        render({ state: latest.state, latest });
      } catch (error) { notice(error.message); }
      finally { button.disabled = false; }
    });
    card.append(area, button);
  }
  if (allowDelete && (service.monitor || service.hostname)) {
    const dnsArea = element("pre", "report", dnsText(service.dns));
    const dnsButton = element("button", "primary-button", "Inspect DNS");
    dnsButton.addEventListener("click", async () => {
      dnsButton.disabled = true;
      try {
        await jsonApi(`/api/services/${service.id}/dns`, {});
        const data = await jsonApi(`/api/services/${service.id}/dns`);
        dnsArea.textContent = dnsText(data);
      } catch (error) { notice(error.message); }
      finally { dnsButton.disabled = false; }
    });
    card.append(dnsArea, dnsButton);
    if (service.monitor?.url.startsWith("https://")) {
      const tlsArea = element("pre", "report", tlsText(service.tls));
      const tlsButton = element("button", "primary-button", "Inspect TLS");
      tlsButton.addEventListener("click", async () => {
        tlsButton.disabled = true;
        try {
          await jsonApi(`/api/services/${service.id}/tls`, {});
          tlsArea.textContent = tlsText(await jsonApi(`/api/services/${service.id}/tls`));
        } catch (e) { notice(e.message); }
        finally { tlsButton.disabled = false; }
      });
      card.append(tlsArea, tlsButton);
    }
  }
  if (allowDelete) {
    const remove = element("button", "delete-button", "Delete service");
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        await api(`/api/services/${service.id}`, { method: "DELETE" });
        await loadServices();
      } catch (error) {
        notice(error.message);
        remove.disabled = false;
      }
    });
    card.append(remove);
  }
  return card;
}
async function loadServices() {
  const services = await jsonApi("/api/services?health=true"),
    list = $("#services-list");
  list.replaceChildren();
  if (!services.length)
    list.append(
      element(
        "div",
        "empty",
        "Add your first service. Inventory entries are searchable; upload a runbook to use them in knowledge chat.",
      ),
    );
  for (const service of services) {
    service.health = { state: service.health?.state || "unknown", latest: service.health };
    list.append(serviceCard(service, true));
  }
}
$("#probe-services").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try { await jsonApi("/api/services/probe", {}); await loadServices(); }
  catch (error) { notice(error.message); }
  finally { button.disabled = false; }
});
$("#service-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  notice();
  const form = $("#service-form"),
    data = Object.fromEntries(new FormData(form));
  const split = (value) =>
    value
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean);
  data.ports = split(data.ports).map(Number);
  data.urls = split(data.urls);
  data.dependencies = split(data.dependencies);
  data.tags = split(data.tags);
  data.monitor_url ||= null;
  data.local_windows_service ||= null;
  data.health_url ||= null;
  data.expected_status = Number(data.expected_status);
  data.timeout_seconds = Number(data.timeout_seconds);
  if (data.monitor_url) data.monitor = {
    url: data.monitor_url, health_url: data.health_url,
    expected_status: data.expected_status, timeout_seconds: data.timeout_seconds,
    environment: data.environment,
  };
  for (const key of ["monitor_url", "health_url", "expected_status", "timeout_seconds", "environment"]) delete data[key];
  if (!data.ip_address) data.ip_address = null;
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    await jsonApi("/api/services", data);
    form.reset();
    form.closest("details").open = false;
    await loadServices();
  } catch (error) {
    notice(error.message);
  } finally {
    button.disabled = false;
  }
});
refreshStatus();


let eventPage = null;
let eventParams = null;
const selectedWindowsEvents = new Map();
async function loadWindowsEvents(older = false) {
  const params = older ? new URLSearchParams(eventParams) : eventFilters(new FormData($("#events-form")));
  if (older && eventPage?.next_before_record) params.set("before_record", eventPage.next_before_record);
  eventParams = params;
  if (!older) $("#events-report").textContent = "";
  const [page, channels, services] = await Promise.all([
    jsonApi(`/api/events?${params}`), jsonApi("/api/events/channels"), jsonApi("/api/services")
  ]);
  eventPage = page;
  $("#events-status").textContent = channels.channels.map(c => `${c.channel}: ${c.status}${c.reason ? ` (${c.reason})` : ""}`).join("\n") +
    `\n${page.records.length} records. ${(page.issues || []).join("; ")}`;
  $("#events-more").disabled = !page.next_before_record;
  const serviceSelect = $("#events-service");
  const previous = serviceSelect.value;
  serviceSelect.replaceChildren(new Option("No service selected", "unknown"));
  for (const service of services) serviceSelect.add(new Option(service.name, service.id));
  serviceSelect.value = previous;
  const list = $("#events-list"); list.replaceChildren();
  for (const record of page.records) {
    const card = element("article", "panel");
    const label = element("label");
    const checkbox = element("input"); checkbox.type = "checkbox";
    checkbox.checked = selectedWindowsEvents.has(record.evidence_id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked && selectedWindowsEvents.size >= 50) {
        checkbox.checked = false; notice("Select at most 50 events."); return;
      }
      if (checkbox.checked) selectedWindowsEvents.set(record.evidence_id, eventReference(record));
      else selectedWindowsEvents.delete(record.evidence_id);
    });
    label.append(checkbox, document.createTextNode(" Select event evidence"));
    const detail = element("details");
    detail.append(element("summary", "", "Exact source fields"), element("pre", "report", JSON.stringify(record, null, 2)));
    card.append(label, element("pre", "report", eventText(record)), detail); list.append(card);
  }
}
$("#events-form").addEventListener("submit", async event => {
  event.preventDefault(); selectedWindowsEvents.clear();
  try { await loadWindowsEvents(); } catch (error) { notice(error.message); }
});
$("#events-more").addEventListener("click", () => loadWindowsEvents(true).catch(e => notice(e.message)));
async function explainWindowsEvents(incident = false) {
  if (!selectedWindowsEvents.size) { notice("Select event records first."); return; }
  const body = {selected: [...selectedWindowsEvents.values()],
    question: $("#events-question").value, service: $("#events-service").value || "unknown",
    use_model: $("#events-model").checked, notes: $("#incident-notes").value};
  if (incident) body.incident_title = $("#incident-title").value || "Windows host incident";
  const result = await jsonApi("/api/events/analyse", body);
  const analysis = result.analysis || result;
  $("#events-report").textContent = incident ? result.markdown : analysis.answer + "\n\nRepeated provider/event groups:\n" +
    JSON.stringify(analysis.repeated_patterns, null, 2) + "\nSeparate time correlations:\n" + JSON.stringify(analysis.correlations, null, 2);
}
$("#events-analyse").addEventListener("click", () => explainWindowsEvents().catch(e => notice(e.message)));
$("#events-incident").addEventListener("click", () => explainWindowsEvents(true).catch(e => notice(e.message)));

$("#host-refresh").addEventListener("click", async event => {
  event.target.disabled = true;
  try {
    const page = await jsonApi("/api/host");
    $("#host-status").textContent = `${page.host} · ${page.status} · ${page.observed_at}`;
    const facts = page.records[0]?.facts || {};
    const grid = $("#host-overview"); grid.replaceChildren();
    for (const [title, value] of hostCards(facts)) {
      const card = element("div", "host-metric");
      card.append(element("small", "", title), element("strong", "", value)); grid.append(card);
    }
    for (const [selector, values, format] of [["#host-disks", facts.disks || [], diskText], ["#host-network", facts.interfaces || [], interfaceText]]) {
      const list = $(selector); list.replaceChildren();
      for (const value of values) list.append(element("p", "host-row", format(value)));
      if (!values.length) list.append(element("p", "", "No accessible entries in this sample."));
    }
    $("#host-evidence").textContent = [...page.issues, ...page.records.map(r => r.evidence_id)].join("\n");
  } catch (error) { notice(error.message); }
  finally { event.target.disabled = false; }
});
$("#host-services-form").addEventListener("submit", async event => {
  event.preventDefault();
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(event.target)) if (value) params.set(key, value);
  params.set("limit", "100");
  const button = event.target.querySelector("button"); button.disabled = true;
  try {
    const page = await jsonApi(`/api/host/services?${params}`);
    $("#host-services-status").textContent = `${page.status} · ${page.records.length} services${page.truncated ? " · Truncated; narrow filters" : ""}\n${page.issues.join("\n")}`;
    const list = $("#host-services"); list.replaceChildren();
    for (const record of page.records) list.append(element("pre", "host-row", serviceText(record)));
  } catch (error) { notice(error.message); }
  finally { button.disabled = false; }
});
$("#host-correlate").addEventListener("click", async event => {
  event.target.disabled = true;
  try {
    const name = $("#host-mapping").value.trim();
    const result = await jsonApi(`/api/host/correlations${name ? `?service=${encodeURIComponent(name)}` : ""}`);
    $("#host-correlations").textContent = hostContextText(result);
  } catch (error) { notice(error.message); }
  finally { event.target.disabled = false; }
});
