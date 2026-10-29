export async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      detail =
        typeof body.detail === "string"
          ? body.detail
          : Array.isArray(body.detail)
            ? body.detail
                .map((e) => `${e.loc.slice(1).join(".")}: ${e.msg}`)
                .join("; ")
            : detail;
    } catch {
      /* Proxy errors may not be JSON. */
    }
    const id = response.headers.get("x-request-id");
    throw new Error(detail + (id ? ` [request ${id.slice(0, 8)}]` : ""));
  }
  if (response.status === 204) return null;
  return response;
}

export async function jsonApi(path, body) {
  const response = await api(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  return response.json();
}

export async function* readEvents(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) if (line.trim()) yield JSON.parse(line);
      if (done) break;
    }
    if (buffer.trim()) yield JSON.parse(buffer);
  } finally {
    reader.releaseLock();
  }
}

export function modelStatus(status) {
  const ai = status.ai;
  const embeddings = status.embeddings;
  const provider = ai.provider.charAt(0).toUpperCase() + ai.provider.slice(1);
  return {
    model: `${provider} · ${ai.model || "No model configured"}`,
    label: ai.state === "ready"
      ? (embeddings.state === "ready" ? "Local models ready" : "Chat ready · embeddings unavailable")
      : `AI: ${ai.state.replaceAll("_", " ")}`,
    detail: `${ai.backend} · ${(ai.reason || ai.state).replace(/\.+$/, "")}. Embeddings: ${embeddings.reason || embeddings.state}`,
  };
}
