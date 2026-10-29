const pct = value => value == null ? "Unavailable" : `${value.toFixed(1)}%`;
const gib = value => value == null ? "Unavailable" : `${(value / 2 ** 30).toFixed(1)} GiB`;
export function hostCards(facts) {
  const uptime = facts.uptime_seconds;
  return [
    ["System", facts.os ? `Windows ${facts.os.release} ${facts.os.edition || ""} · Build ${facts.os.build}` : "Unavailable"],
    ["Uptime", uptime == null ? "Unavailable" : `${Math.floor(uptime / 86400)}d ${Math.floor(uptime % 86400 / 3600)}h ${Math.floor(uptime % 3600 / 60)}m`],
    ["CPU", `${pct(facts.cpu_percent)} · ${facts.logical_cpu_count ?? "?"} logical processors`],
    ["Memory", facts.memory ? `${pct(facts.memory.used_percent)} · ${gib(facts.memory.available_bytes)} available / ${gib(facts.memory.total_bytes)}` : "Unavailable"],
  ];
}
export const diskText = disk => `${disk.identifier} · ${pct(disk.used_percent)} used · ${gib(disk.free_bytes)} free / ${gib(disk.total_bytes)} · ${disk.space_state}`;
export const interfaceText = nic => `${nic.identifier} · ${nic.state ?? "Unavailable"}${nic.loopback ? " · loopback" : ""}\n${nic.addresses.join(" · ") || "No assigned IP addresses"}`;
export const serviceText = record => `${record.facts.display_name ?? record.facts.name} (${record.facts.name})\n${record.facts.state ?? "Unavailable"} · ${record.facts.start_type ?? "Unavailable"} · PID ${record.facts.process_id ?? "Unavailable"}\n${record.observed_at}\n${record.evidence_id}`;
export function hostContextText(context) {
  const a = context.assessment;
  return ["CONFIRMED", ...a.confirmed, a.confirmed.length ? "" : "No threshold findings in this sample.",
    "PROBABLE", ...(a.probable.length ? a.probable : ["No cause established."]),
    "UNKNOWN", ...a.unknown, "SUPPORTING EVIDENCE",
    ...(context.correlations.length ? context.correlations.map(c => `${c.relationship}\n${c.caveat}\n${c.evidence_ids.join(" · ")}`) : ["No matching relationships in the bounded sample."]),
    ...(context.issues || []),
    context.event_scope ? `Event sample: ${context.event_scope.status}` : "",
  ].filter(Boolean).join("\n");
}
