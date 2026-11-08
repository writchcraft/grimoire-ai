export function tcpText(data) {
  if (!data?.latest?.length) return "TCP · UNKNOWN · Not checked yet";
  return data.latest.map(item => {
    const seconds = Math.max(0, Math.floor((Date.now() - new Date(item.observed_at)) / 1000));
    const samples = (data.history ?? []).filter(o => o.hostname === item.hostname && o.port === item.port)
      .slice(0, 5).map(o => `${o.observed_at} ${o.state}`).join("\n");
    return `TCP :${item.port} · ${item.state.toUpperCase()}${seconds > 300 ? " · Stale" : ""}\n${item.hostname} - ${item.address ?? "unavailable"} · ${item.connection_latency_ms ?? "?"} ms\nChecked ${seconds} seconds ago (${item.observed_at})\n${item.reason}${samples ? `\nRecent samples:\n${samples}` : ""}`;
  }).join("\n\n");
}
