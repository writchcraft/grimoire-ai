export function dnsText(data) {
  const item = data?.latest;
  if (!item) return "DNS · UNKNOWN · Not checked yet";
  const changed = data.addresses_changed === true ? " · Address set changed" : "";
  const stale = Date.now() - new Date(item.observed_at).getTime() > 300000 ? " · Stale" : "";
  return `DNS · ${item.status.toUpperCase()}${stale}\n${item.hostname ?? "No configured hostname"}\n${item.addresses.join(", ") || item.reason}${item.truncated ? " · Results truncated" : ""}\n${item.canonical_name ? `Canonical: ${item.canonical_name}\n` : ""}Checked ${new Date(item.observed_at).toLocaleString()}${changed}`;
}
