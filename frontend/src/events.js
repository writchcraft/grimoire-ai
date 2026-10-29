export function eventText(record) {
  return `${record.channel} · ${record.severity}\n${record.provider || "Unknown provider"}\nEvent ID ${record.event_id ?? "unavailable"} · Record ${record.record_id ?? "unavailable"}\n${record.timestamp || "Timestamp unavailable"}\n${record.message ?? "Rendered message unavailable; inspect source data."}\nEvidence: ${record.evidence_id}`;
}
export function eventReference(record) {
  return {channel: record.channel, record_id: record.record_id, evidence_id: record.evidence_id};
}
export function eventFilters(form, now = new Date()) {
  const params = new URLSearchParams();
  for (const key of ["channel", "level", "provider", "event_id"]) {
    const value = form.get(key);
    if (value) params.set(key, value);
  }
  params.set("limit", "100");
  if (form.get("hours")) {
    params.set("end", now.toISOString());
    params.set("start", new Date(now.getTime() - Number(form.get("hours")) * 3600000).toISOString());
  }
  if (form.get("around")) {
    params.delete("start"); params.delete("end");
    params.set("around", new Date(form.get("around")).toISOString());
    params.set("minutes", "5");
  }
  return params;
}
