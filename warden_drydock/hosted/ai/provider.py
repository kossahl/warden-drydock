from __future__ import annotations

from collections.abc import Iterable
import json
import os
import urllib.error
import urllib.request

from .models import GenerationRequest


class ProviderUnavailable(RuntimeError):
    pass


class OpenAIResponsesAdapter:
    """Personal-pilot OpenAI adapter; provider-native details stop at this boundary."""

    adapter_id = "openai_responses"
    adapter_version = "1.0.0"
    model = "gpt-5.6-luna"

    def __init__(self, transport=None) -> None:
        self._transport = transport or self._http_transport

    def verify(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

    def stream(self, request: GenerationRequest) -> Iterable[tuple[str, str | None]]:
        payload = self.build_payload(request)
        try:
            yield from self._transport(payload)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable("provider request failed") from exc

    def build_payload(self, request: GenerationRequest) -> dict:
        sources = "\n\n".join(
            f"[source:{item.source_id} authority:{item.authority} revision:{request.revision_id}]\n{item.text}"
            for item in request.envelope.excerpts
        )
        return {
            "model": self.model,
            "store": False,
            "stream": True,
            "input": [{
                "role": "developer",
                "content": "Authority: Draft. Use only the bound sources. Cite source ids for factual claims; report missing or contradictory evidence.",
            }, {"role": "user", "content": f"Action: {request.action.value}\n{request.prompt}\n\nBound sources:\n{sources}"}],
            "metadata": {
                "generation_id": request.generation_id,
                "revision_id": request.revision_id,
                "source_set_digest": request.envelope.source_set_digest,
            },
        }

    @staticmethod
    def _http_transport(payload: dict) -> Iterable[tuple[str, str | None]]:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderUnavailable("provider credential is not configured")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[6:])
                    kind = event.get("type", "")
                    if kind == "response.output_text.delta":
                        yield "delta", event.get("delta", "")
                    elif kind == "response.completed":
                        yield "completion", None
                    elif kind == "response.failed":
                        yield "failure", None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderUnavailable("provider unavailable") from exc
