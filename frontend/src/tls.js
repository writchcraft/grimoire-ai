export function tlsText(data) {
  const item = data?.latest;
  if (!item) return "TLS · UNKNOWN · Not checked yet";
  if (!item.applicable) return `TLS · Skipped · ${item.reason}`;
  const stale = Date.now() - new Date(item.observed_at).getTime() > 300000 ? " · Stale" : "";
  const changed = data.changes ? Object.entries(data.changes).filter(([, v]) => v).map(([k]) => k).join(", ") : "";
  return `TLS · ${item.state.toUpperCase()}${stale}\n${item.certificate_presented ? "Certificate presented" : item.reason}\nExpires: ${item.expires_at ?? "unavailable"} (${item.remaining_days ?? "?"} days at check)\nHostname: ${item.hostname_match} · Trust: ${item.trust}\n${item.trust_reason ?? ""}\n${item.protocol ?? "Protocol unavailable"}\nChecked: ${item.observed_at}${changed ? `\nChanged: ${changed}` : ""}`;
}
