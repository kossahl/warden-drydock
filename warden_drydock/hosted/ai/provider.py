from __future__ import annotations

from collections.abc import Iterable
import json
import os
import pathlib
import urllib.error
import urllib.request
import hashlib

from .models import GenerationRequest


class ProviderUnavailable(RuntimeError):
    pass


class OpenAIResponsesAdapter:
    """Personal-pilot OpenAI adapter; provider-native details stop at this boundary."""

    adapter_id = "openai_responses"
    adapter_version = "1.0.0"
    model = "gpt-5.6-luna"
    default_max_output_tokens = 2048
    maximum_output_tokens = 128_000

    def __init__(self, transport=None, *, max_output_tokens: int = default_max_output_tokens) -> None:
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not 1 <= max_output_tokens <= self.maximum_output_tokens
        ):
            raise ValueError(
                f"max_output_tokens must be an integer from 1 to {self.maximum_output_tokens}"
            )
        self._transport = transport or self._http_transport
        self.max_output_tokens = max_output_tokens

    def verify(self) -> bool:
        # Capability is established by the first authorized Responses request;
        # Models API read permission is neither required nor probed here.
        environment_key = os.environ.get("OPENAI_API_KEY")
        file_name = os.environ.get("OPENAI_API_KEY_FILE")
        if environment_key and file_name:
            return False
        if environment_key:
            return bool(environment_key.strip())
        if not file_name:
            return False
        try:
            path = self._credential_path(file_name)
            return path.is_file() and path.stat().st_size > 0
        except (OSError, ProviderUnavailable):
            return False

    def credential_revision_fingerprint(self) -> str:
        key = self._credential()
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _credential_path(file_name: str) -> pathlib.Path:
        root_name = os.environ.get("DRYDOCK_SECRETS")
        if not root_name:
            raise ProviderUnavailable("provider secret root is not configured")
        try:
            root = pathlib.Path(root_name).resolve(strict=True)
            path = pathlib.Path(file_name).resolve(strict=True)
            path.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ProviderUnavailable("provider credential file is unavailable") from exc
        return path

    def _credential(self) -> str:
        environment_key = os.environ.get("OPENAI_API_KEY")
        file_name = os.environ.get("OPENAI_API_KEY_FILE")
        if environment_key and file_name:
            raise ProviderUnavailable("provider credential configuration is ambiguous")
        if environment_key:
            key = environment_key.strip()
        elif file_name:
            try:
                key = self._credential_path(file_name).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise ProviderUnavailable("provider credential file is unavailable") from exc
        else:
            key = ""
        if not key:
            raise ProviderUnavailable("provider credential is not configured")
        return key

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
            "max_output_tokens": self.max_output_tokens,
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
    def _parse_sse(lines: Iterable[bytes]) -> Iterable[tuple[str, str | None]]:
        for raw in lines:
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise ProviderUnavailable("malformed provider stream") from exc
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError as exc:
                raise ProviderUnavailable("malformed provider stream") from exc
            kind = event.get("type", "")
            if kind == "response.output_text.delta":
                yield "delta", event.get("delta", "")
            elif kind == "response.completed":
                usage = event.get("response", {}).get("usage")
                if usage is not None:
                    yield "usage", json.dumps(usage, separators=(",", ":"), sort_keys=True)
                yield "completion", None
            elif kind == "response.failed":
                yield "failure", None

    def _http_transport(self, payload: dict) -> Iterable[tuple[str, str | None]]:
        key = self._credential()
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                yield from OpenAIResponsesAdapter._parse_sse(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderUnavailable("provider unavailable") from exc
