import test from "node:test";
import assert from "node:assert/strict";
import { tlsText } from "../src/tls.js";

test("TLS unknown and skipped", () => {
  assert.match(tlsText(null), /Not checked/);
  assert.match(tlsText({latest: {applicable: false, reason: "HTTP"}}), /Skipped/);
});
test("TLS trust, hostname, expiry and staleness remain separate", () => {
  const data = {latest: {applicable: true, state: "untrusted", certificate_presented: true,
    observed_at: "2020-01-01T00:00:00Z", expires_at: "2027-01-01", remaining_days: 12,
    hostname_match: "mismatch", trust: "failed", trust_reason: "untrusted issuer", protocol: "TLSv1.3"},
    changes: {issuer: true, sans: false}};
  const text = tlsText(data);
  for (const part of ["UNTRUSTED", "Certificate presented", "mismatch", "failed", "12", "Stale", "Changed: issuer"])
    assert.ok(text.includes(part));
});
