import test from "node:test";
import assert from "node:assert/strict";
import { dnsText } from "../src/dns.js";

test("DNS cards display deterministic states, addresses, changes and age", () => {
  assert.match(dnsText(), /UNKNOWN/);
  const latest = { status: "resolved", hostname: "grafana.home.arpa", addresses: ["192.168.1.20", "::1"], observed_at: "2000-01-01T00:00:00Z", canonical_name: "target.home.arpa" };
  const text = dnsText({ latest, addresses_changed: true });
  for (const expected of ["RESOLVED", "192.168.1.20, ::1", "Canonical: target.home.arpa", "Address set changed", "Stale"]) assert.ok(text.includes(expected));
  assert.match(dnsText({ latest: { ...latest, status: "unresolved", addresses: [], reason: "Name could not be resolved." } }), /Name could not be resolved/);
});
