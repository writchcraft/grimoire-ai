import test from "node:test";
import assert from "node:assert/strict";
import {hostCards, diskText, interfaceText, serviceText, hostContextText} from "../src/host.js";
test("host cards preserve measured values and unavailable fields", () => {
  assert.match(JSON.stringify(hostCards({cpu_percent: 0, uptime_seconds: 60})), /0.0%/);
  assert.match(JSON.stringify(hostCards({})), /Unavailable/);
});
test("disks, interfaces and services retain exact identifiers and evidence", () => {
  assert.match(diskText({identifier: "C:", used_percent: 95, space_state: "low"}), /95.0%/);
  assert.match(interfaceText({identifier: "loop", addresses: ["::1"], loopback: true}), /loopback/);
  assert.match(serviceText({facts: {name: "Example", state: "stopped"}, evidence_id: "WS-123"}), /WS-123/);
});
test("host incident context preserves uncertainty without dumping raw inventory", () => {
  const text = hostContextText({assessment: {confirmed: [], probable: [], unknown: ["Cause unknown"]}, correlations: [], records: [{facts: {secret: "not rendered"}}]});
  assert.match(text, /Cause unknown/);
  assert.match(text, /bounded sample/);
  assert.doesNotMatch(text, /not rendered/);
});
