import test from "node:test";
import assert from "node:assert/strict";
import { api, readEvents, modelStatus } from "../src/api.js";

test("NDJSON decoder handles split UTF-8 and split lines", async () => {
  const bytes = new TextEncoder().encode(
    '{"type":"token","content":"◈"}\n{"type":"done"}\n',
  );
  const body = new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i++)
        controller.enqueue(bytes.slice(i, i + 1));
      controller.close();
    },
  });
  const events = [];
  for await (const event of readEvents(new Response(body))) events.push(event);
  assert.deepEqual(events, [{ type: "token", content: "◈" }, { type: "done" }]);
});

test("model status identifies either provider and reports embedding failures", () => {
  for (const provider of ["native", "ollama"]) {
    const display = modelStatus({
      ai: {provider, backend: "test", model: "chat.gguf", state: "ready"},
      embeddings: {state: "model_missing", reason: "Embedding GGUF missing"},
    });
    assert.match(display.model.toLowerCase(), new RegExp(provider));
    assert.match(display.model, /chat.gguf/);
    assert.equal(display.label, "Chat ready · embeddings unavailable");
    assert.match(display.detail, /Embedding GGUF missing/);
  }
});

test("model status shows lifecycle states and safe unconfigured identity", () => {
  for (const state of ["not_configured", "model_missing", "loading", "failed"]) {
    const display = modelStatus({
      ai: {provider: "native", backend: "llama.cpp", model: null, state, reason: "Check setup"},
      embeddings: {state: "not_configured"},
    });
    assert.equal(display.label, `AI: ${state.replaceAll("_", " ")}`);
    assert.match(display.model, /No model configured/);
    assert.match(display.detail, /Check setup/);
  }
});

test("API surfaces actionable provider error and request ID", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "Pull the embedding model" }), {
      status: 503,
      headers: { "x-request-id": "123456789abc" },
    });
  try {
    await assert.rejects(
      api("/api/documents"),
      /Pull the embedding model.*12345678/,
    );
  } finally {
    globalThis.fetch = original;
  }
});
