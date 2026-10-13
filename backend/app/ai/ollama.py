import json
from collections.abc import AsyncIterator

import httpx

from app.ai.base import Messages, ModelStatus
from app.core.config import Settings
from app.core.errors import ProviderError


class _OllamaConnection:
    async def start(self) -> None:
        """The optional Ollama daemon owns its own model lifecycle."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.ollama_url + "/",
            timeout=settings.request_timeout,
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def health(self) -> ModelStatus:
        try:
            response = await self.client.get("api/tags", timeout=3)
            response.raise_for_status()
            names = {model["name"] for model in response.json()["models"]}

            def installed(name: str) -> bool:
                return name in names or (":" not in name and f"{name}:latest" in names)

            return {
                "reachable": True,
                "provider": "ollama",
                "backend": "Ollama",
                "model": self.settings.model,
                "state": "ready" if installed(self.settings.model) else "model_missing",
                "reason": None if installed(self.settings.model) else "Pull the configured model.",
                "context_size": self.settings.native_context_size,
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {
                "provider": "ollama",
                "backend": "Ollama",
                "model": self.settings.model,
                "state": "failed",
                "reason": "Ollama endpoint unavailable.",
                "reachable": False,
            }


class OllamaProvider(_OllamaConnection):
    def payload(self, messages: Messages, stream: bool) -> dict:
        return {
            "model": self.settings.model,
            "messages": messages,
            "stream": stream,
            "options": {
                "temperature": 0.15,
                "num_ctx": self.settings.native_context_size,
                "num_predict": self.settings.max_output_tokens,
            },
        }

    async def chat(self, messages: Messages) -> str:
        try:
            response = await self.client.post("api/chat", json=self.payload(messages, False))
            response.raise_for_status()
            data = response.json()
            content = data["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Empty response")
            return content
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ProviderError(
                "Ollama chat unavailable. Check the endpoint and pulled chat model."
            ) from exc

    async def stream(self, messages: Messages) -> AsyncIterator[str]:
        try:
            async with self.client.stream(
                "POST", "api/chat", json=self.payload(messages, True)
            ) as response:
                response.raise_for_status()
                received_content = False
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise ValueError("Provider reported an error")
                    token = data.get("message", {}).get("content", "")
                    if not isinstance(token, str):
                        raise ValueError("Invalid token")
                    if token:
                        received_content = received_content or bool(token.strip())
                        yield token
                    if data.get("done"):
                        if not received_content:
                            raise ValueError("Empty stream")
                        return
                raise ValueError("Incomplete stream")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderError(
                "Ollama stream interrupted. Check the endpoint and chat model."
            ) from exc
