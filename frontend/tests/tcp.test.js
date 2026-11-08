import test from "node:test";
import assert from "node:assert/strict";
import { tcpText } from "../src/tcp.js";

test("TCP empty, reachable, stale, refusal and retained history", () => {
  assert.match(tcpText(null), /UNKNOWN/);
  const item = { hostname: "localhost", address: "127.0.0.1", port: 8080,
    state: "reachable", connection_latency_ms: 1.2, observed_at: new Date().toISOString(), reason: "Connected" };
  const text = tcpText({ latest: [item], history: [item] });
  assert.match(text, /TCP :8080/);
  assert.match(text, /REACHABLE/);
  assert.match(text, /1.2 ms/);
  assert.match(text, /Recent samples/);
  assert.doesNotMatch(text, /uptime|%/);
  assert.match(tcpText({latest: [{...item, state: "refused", observed_at: "2000-01-01T00:00:00Z"}]}), /REFUSED.*Stale/);
});
