import test from "node:test";
import assert from "node:assert/strict";
import {eventText,eventFilters,eventReference} from "../src/events.js";
test("event facts retain source identity and hostile text literally", () => {
  const record={channel:"System",severity:"error",provider:"Example",event_id:7031,record_id:10,timestamp:"2026-10-29T19:42:14Z",message:"<script>inert</script>",evidence_id:"WE-source"};
  assert.match(eventText(record),/Event ID 7031/);
  assert.match(eventText(record),/<script>inert<\/script>/);
  assert.deepEqual(eventReference(record),{channel:"System",record_id:10,evidence_id:"WE-source"});
});
test("bounded time filters and correlation window", () => {
  const data=new FormData(); data.set("channel","System");data.set("level","error");data.set("hours","1");
  const filters=eventFilters(data,new Date("2026-10-29T20:00:00Z"));
  assert.equal(filters.get("limit"),"100"); assert.equal(filters.get("start"),"2026-10-29T19:00:00.000Z");
  data.set("around","2026-10-29T19:42");
  const around=eventFilters(data); assert.equal(around.get("minutes"),"5");assert.equal(around.has("start"),false);
});
