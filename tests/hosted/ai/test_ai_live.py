from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from warden_drydock.core.generator import DATA
from warden_drydock.hosted.ai.live import LiveSessionService, StaleController, StaleWorkflow
from warden_drydock.hosted.ai.models import Action, CaptureType
from warden_drydock.hosted.ai.provider import OpenAIResponsesAdapter, ProviderUnavailable
from warden_drydock.hosted.ai.repository import InMemoryAIRepository
from warden_drydock.hosted.ai.retrieval import DeterministicSourceSelector
from warden_drydock.hosted.ai.retrieval import EngineSourceLoader
from warden_drydock.hosted.ai.service import ConsentRequired, GroundedAIService
from warden_drydock.hosted.engine import (
    ChangeKind,
    DeterministicEngine,
    ExactTextChange,
    InitializeRequest,
    StageExactDiffRequest,
    Status,
    WorkspaceRegistry,
    exact_diff_digest,
)
from warden_drydock.hosted.operations.secrets import SecretStore


@dataclass(frozen=True)
class Record:
    subject_id: str
    status: str
    content: str


class FakeProvider:
    adapter_id = "fake_provider"
    adapter_version = "1.0.0"
    def __init__(self, events=None, *, available=True):
        self.events = events or [("delta", "Authority: Draft. [source:station]"), ("completion", None)]
        self.available = available
        self.calls = []
        self.fingerprint = "a" * 64

    def verify(self):
        return True

    def credential_revision_fingerprint(self):
        return self.fingerprint

    def stream(self, request):
        self.calls.append(request)
        if not self.available:
            raise ProviderUnavailable("offline")
        yield from self.events


class FakeSourceLoader:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def load(self, campaign_id, revision_id, prompt):
        self.calls.append((campaign_id, revision_id, prompt))
        return tuple(self.records)


class ObservingProvider(FakeProvider):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository

    def stream(self, request):
        self.calls.append(request)
        self.assert_persisted(request.generation_id, 1)
        yield "delta", "Draft"
        self.assert_persisted(request.generation_id, 2)
        yield "completion", None

    def assert_persisted(self, generation_id, count):
        record = self.repository.get_generation(generation_id)
        if record is None or len(record.events) != count:
            raise AssertionError("stream event was not persisted incrementally")


class SourcesObservingProvider(FakeProvider):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository

    def stream(self, request):
        self.calls.append(request)
        if self.repository.sources.get(request.generation_id) != request.envelope:
            raise AssertionError("source envelope was not persisted before provider dispatch")
        yield from self.events


class GroundedAIServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryAIRepository()
        self.provider = FakeProvider()
        self.records = [Record("station", "canon", "The airlock is sealed."), Record("plan", "preparation", "Open it later.")]
        self.loader = FakeSourceLoader(self.records)
        self.service = GroundedAIService(self.repository, DeterministicSourceSelector(), self.provider, self.loader)

    def test_verification_and_explicit_consent_gate_dispatch(self):
        with self.assertRaises(ConsentRequired):
            self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        with self.assertRaises(ConsentRequired):
            self.service.record_consent(explicit=False)
        self.service.record_consent(explicit=True)
        self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual(["generation_one"], self.repository.dispatch_log)

    def test_openai_consent_then_start_dispatches_one_responses_request(self):
        repository = InMemoryAIRepository()
        dispatched = []

        def transport(payload):
            dispatched.append(payload)
            return iter((("completion", None),))

        provider = OpenAIResponsesAdapter(transport, max_output_tokens=512)
        service = GroundedAIService(repository, DeterministicSourceSelector(), provider, self.loader)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key"}, clear=True):
            service.record_consent(explicit=True)
            record = service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual("complete", record.terminal_status)
        self.assertEqual(1, len(dispatched))
        self.assertEqual(512, dispatched[0]["max_output_tokens"])
        self.assertEqual(["generation_one"], repository.dispatch_log)

    def test_openai_http_transport_sends_exact_payload_bytes_to_local_endpoint(self):
        repository = InMemoryAIRepository()
        received = {}
        constructed = []

        class RecordingHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received["method"] = self.command
                received["path"] = self.path
                received["headers"] = {name: value for name, value in self.headers.items()}
                received["body"] = self.rfile.read(length)
                events = (
                    {"type": "response.output_text.delta", "delta": "Draft"},
                    {"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 1}}},
                )
                body = "".join("data: " + json.dumps(event) + "\n" for event in events).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/responses"

        def cleanup():
            server.shutdown()
            thread.join(5)
            server.server_close()

        self.addCleanup(cleanup)

        provider = OpenAIResponsesAdapter(max_output_tokens=512)
        service = GroundedAIService(repository, DeterministicSourceSelector(), provider, self.loader)

        class LocalEndpointRequest(urllib.request.Request):
            def __init__(self, url, **kwargs):
                constructed.append(url)
                super().__init__(endpoint, **kwargs)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key"}, clear=True):
            with patch("urllib.request.Request", LocalEndpointRequest):
                service.record_consent(explicit=True)
                record = service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual("complete", record.terminal_status)
        self.assertEqual("Draft", record.terminal_content)
        self.assertEqual(["generation_one"], repository.dispatch_log)
        self.assertEqual(["https://api.openai.com/v1/responses"], constructed)
        self.assertEqual("POST", received["method"])
        self.assertEqual("/v1/responses", received["path"])
        self.assertEqual("Bearer synthetic-key", received["headers"].get("Authorization"))
        self.assertEqual("application/json", received["headers"].get("Content-Type"))
        payload = provider.build_payload(record.request)
        self.assertEqual(json.dumps(payload).encode("utf-8"), received["body"])
        self.assertEqual(512, payload["max_output_tokens"])

    def test_openai_missing_credential_prevents_consent_and_dispatch(self):
        repository = InMemoryAIRepository()
        dispatched = []
        provider = OpenAIResponsesAdapter(lambda payload: dispatched.append(payload))
        service = GroundedAIService(repository, DeterministicSourceSelector(), provider, self.loader)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConsentRequired):
                service.record_consent(explicit=True)
            with self.assertRaises(ConsentRequired):
                service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual([], dispatched)
        self.assertEqual([], repository.dispatch_log)

    def test_openai_compose_file_secret_loads_and_rotation_invalidates_consent(self):
        repository = InMemoryAIRepository()
        dispatched = []
        provider = OpenAIResponsesAdapter(lambda payload: dispatched.append(payload))
        service = GroundedAIService(repository, DeterministicSourceSelector(), provider, self.loader)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SecretStore(root)
            secret = root / "openai_api_key"
            store.replace("openai_api_key", b"synthetic-first\n")
            environment = {
                "DRYDOCK_SECRETS": str(root),
                "OPENAI_API_KEY_FILE": str(secret),
            }
            with patch.dict(os.environ, environment, clear=True):
                with patch.object(Path, "read_text", side_effect=AssertionError("presence probe read credential")):
                    self.assertTrue(provider.verify())
                self.assertEqual(
                    hashlib.sha256(b"synthetic-first").hexdigest(),
                    provider.credential_revision_fingerprint(),
                )
                service.record_consent(explicit=True)
                store.replace("openai_api_key", b"synthetic-second")
                with self.assertRaises(ConsentRequired):
                    service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual([], dispatched)
        self.assertEqual([], repository.dispatch_log)

    def test_openai_file_secret_boundary_fails_closed(self):
        provider = OpenAIResponsesAdapter(lambda _: ())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            inside = root / "secrets"
            inside.mkdir()
            outside.write_text("synthetic", encoding="utf-8")
            with patch.dict(os.environ, {
                "DRYDOCK_SECRETS": str(inside),
                "OPENAI_API_KEY_FILE": str(outside),
            }, clear=True):
                self.assertFalse(provider.verify())
                with self.assertRaises(ProviderUnavailable):
                    provider.credential_revision_fingerprint()
            with patch.dict(os.environ, {
                "OPENAI_API_KEY": "synthetic-env",
                "DRYDOCK_SECRETS": str(inside),
                "OPENAI_API_KEY_FILE": str(outside),
            }, clear=True):
                self.assertFalse(provider.verify())

    def test_openai_responses_access_and_transport_errors_persist_sanitized_failure(self):
        failures = (
            urllib.error.HTTPError(
                "https://api.openai.com/v1/responses",
                code,
                "Rejected",
                {},
                io.BytesIO(b"synthetic-key campaign-secret provider-body"),
            )
            for code in (401, 403)
        )
        failures = (*failures, urllib.error.URLError("transport campaign-secret"))
        for index, failure in enumerate(failures):
            with self.subTest(failure=type(failure).__name__, code=getattr(failure, "code", None)):
                repository = InMemoryAIRepository()
                provider = OpenAIResponsesAdapter(max_output_tokens=512)
                service = GroundedAIService(repository, DeterministicSourceSelector(), provider, self.loader)
                with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key"}, clear=True):
                    with patch("warden_drydock.hosted.ai.provider.urllib.request.urlopen", side_effect=failure) as urlopen:
                        service.record_consent(explicit=True)
                        record = service.start(
                            f"generation_{index}", "campaign_one", "revision_one", Action.ASK, "campaign-secret"
                        )
                        self.assertEqual(1, urlopen.call_count)
                        sent_request = urlopen.call_args.args[0]
                        self.assertEqual("https://api.openai.com/v1/responses", sent_request.full_url)
                        self.assertEqual("POST", sent_request.get_method())
                        self.assertEqual(512, json.loads(sent_request.data)["max_output_tokens"])
                self.assertEqual("failed", record.terminal_status)
                self.assertEqual("", record.terminal_content)
                self.assertEqual(["start", "failure"], [event.event_type for event in record.events])
                persisted = repr(record.events)
                self.assertNotIn("synthetic-key", persisted)
                self.assertNotIn("campaign-secret", persisted)
                self.assertNotIn("provider-body", persisted)
                self.assertEqual([f"generation_{index}"], repository.dispatch_log)
                if isinstance(failure, urllib.error.HTTPError):
                    failure.close()

    def test_retrieval_is_identical_and_persisted_before_dispatch(self):
        provider = SourcesObservingProvider(self.repository)
        self.service.provider = provider
        self.service.record_consent(explicit=True)
        one = self.service.start("generation_one", "campaign_one", "revision_one", Action.CHECK, "Check")
        two = self.service.start("generation_two", "campaign_one", "revision_one", Action.CHECK, "Check")
        self.assertEqual(one.request.envelope.source_set_digest, two.request.envelope.source_set_digest)
        self.assertIn("generation_one", self.repository.sources)
        self.assertEqual(self.repository.sources["generation_one"], provider.calls[0].envelope)
        self.assertEqual(self.repository.sources["generation_one"], self.repository.sources["generation_two"])
        self.assertEqual(2, len(provider.calls))
        self.assertEqual(["generation_one", "generation_two"], self.repository.dispatch_log)

    def test_generation_exact_replay_does_not_dispatch_twice(self):
        self.service.record_consent(explicit=True)
        one = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        two = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertIs(one, two)
        self.assertEqual(one.request, two.request)
        self.assertEqual(one.request.envelope.source_set_digest, self.repository.sources["generation_one"].source_set_digest)
        self.assertEqual(["generation_one"], self.repository.dispatch_log)
        with self.assertRaisesRegex(ValueError, "idempotency_digest_conflict"):
            self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "Changed")

    def test_concurrent_exact_replay_has_one_provider_dispatch(self):
        self.service.record_consent(explicit=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(lambda _: self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?"), range(2)))
        self.assertEqual(1, len(self.provider.calls))
        self.assertEqual(["generation_one"], self.repository.dispatch_log)
        self.assertEqual(records[0].request, records[1].request)

    def test_incomplete_provider_stream_is_resumable_failure(self):
        self.service.provider = FakeProvider(events=[("delta", "partial")])
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual("failed", record.terminal_status)
        self.assertEqual("failure", record.events[-1].event_type)
        self.assertIs(record.events[-1].retryable, True)
        stored = self.repository.get_generation("generation_one")
        self.assertEqual(["start", "delta", "failure"], [event.event_type for event in stored.events])
        self.assertEqual([1, 2, 3], [event.sequence for event in stored.events])
        self.assertEqual("partial", stored.events[1].draft_fragment)
        self.assertEqual("partial", stored.terminal_content)
        replayed = self.service.resume(stored, 1)
        self.assertEqual(stored.events[1:], list(replayed))
        self.assertEqual(["delta", "failure"], [event.event_type for event in replayed])
        self.assertEqual([2, 3], [event.sequence for event in replayed])

    def test_consent_is_invalidated_by_credential_revision_change(self):
        self.service.record_consent(explicit=True)
        self.provider.fingerprint = "b" * 64
        with self.assertRaises(ConsentRequired):
            self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")

    def test_every_material_consent_identity_change_requires_renewal(self):
        changes = (
            ("adapter_version", "provider", "2.0.0"),
            ("endpoint_id", "service", "alternate_endpoint"),
            ("region", "service", "alternate_region"),
            ("storage_mode", "service", "provider_default"),
            ("retrieval_policy_version", "service", 2),
            ("notice", "service", "changed handling notice"),
        )
        for field, target_name, changed in changes:
            with self.subTest(field=field):
                self.setUp()
                self.service.record_consent(explicit=True)
                target = self.provider if target_name == "provider" else self.service
                setattr(target, field, changed)
                with self.assertRaises(ConsentRequired):
                    self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")

    def _live_session_with_question_and_fact(self):
        live = LiveSessionService(self.repository)
        session = live.start("session_one", "campaign_one", "revision_one", "controller_one")
        live.capture("session_one", "controller_one", 1, session.workflow_version, event_id="question_one", device_id="device_one", operation_id="operation_one", device_order=1, capture_type=CaptureType.UNRESOLVED_QUESTION, text="Secret question")
        live.capture("session_one", "controller_one", 1, session.workflow_version, event_id="fact_one", device_id="device_one", operation_id="operation_two", device_order=2, capture_type=CaptureType.CONFIRMED_FACT, text="Door opened")
        return live

    def test_live_generation_rejects_stale_base_revision_as_unsafe_binding(self):
        self._live_session_with_question_and_fact()
        self.service.record_consent(explicit=True)
        with self.assertRaisesRegex(ValueError, "unsafe_binding"):
            self.service.start("generation_stale", "campaign_one", "revision_new", Action.CHECK, "Check", session_id="session_one")
        self.assertEqual([], self.repository.dispatch_log)
        self.assertEqual([], self.provider.calls)
        self.assertIsNone(self.repository.get_generation("generation_stale"))

    def test_live_generation_grounds_only_in_confirmed_facts(self):
        self._live_session_with_question_and_fact()
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.CHECK, "Check", session_id="session_one")
        envelope = record.request.envelope
        self.assertEqual("revision_one", record.request.revision_id)
        self.assertEqual("revision_one", envelope.revision_id)
        self.assertEqual("session_one", envelope.session_id)
        self.assertEqual({"station", "plan", "fact_one"}, {item.source_id for item in envelope.excerpts})
        fact = next(item for item in envelope.excerpts if item.source_id == "fact_one")
        self.assertEqual("table_fact", fact.authority)
        self.assertEqual("Door opened", fact.text)
        self.assertNotIn("question_one", [item.source_id for item in envelope.excerpts])
        self.assertNotIn("Secret question", [item.text for item in envelope.excerpts])
        self.assertEqual(["generation_one"], self.repository.dispatch_log)

    def test_events_after_terminal_fail_closed(self):
        self.service.provider = FakeProvider(events=[("completion", None), ("delta", "late")])
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual("failed", record.terminal_status)
        self.assertEqual(["start", "failure"], [event.event_type for event in record.events])
        self.assertIs(record.events[-1].retryable, False)
        self.assertEqual("", record.terminal_content)
        self.assertNotIn("late", record.terminal_content)

        self.service.provider = FakeProvider(events=[("delta", "draft"), ("completion", None), ("delta", "late")])
        record = self.service.start("generation_two", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual("failed", record.terminal_status)
        self.assertEqual(["start", "delta", "failure"], [event.event_type for event in record.events])
        self.assertIs(record.events[-1].retryable, False)
        self.assertEqual("draft", record.terminal_content)
        self.assertNotIn("late", record.terminal_content)

    def test_stream_is_ordered_resumable_and_terminal_draft_persists(self):
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual([1, 2, 3], [event.sequence for event in record.events])
        self.assertEqual([2, 3], [event.sequence for event in self.service.resume(record, 1)])
        self.assertEqual("complete", record.terminal_status)
        self.assertTrue(record.terminal_content.startswith("Authority: Draft"))

    def test_stream_events_are_persisted_before_next_provider_event(self):
        self.service.provider = ObservingProvider(self.repository)
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual("complete", record.terminal_status)

    def test_provider_outage_creates_failure_without_mutation(self):
        self.service.provider = FakeProvider(available=False)
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.GENERATE, "Idea")
        self.assertEqual("failed", record.terminal_status)
        self.assertEqual(["start", "failure"], [event.event_type for event in record.events])
        self.assertEqual({}, self.repository.sessions)

    def test_openai_payload_is_luna_draft_only_and_store_false(self):
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        payload = OpenAIResponsesAdapter(lambda _: ()).build_payload(record.request)
        self.assertEqual("gpt-5.6-luna", payload["model"])
        self.assertEqual(2048, payload["max_output_tokens"])
        self.assertIs(payload["store"], False)
        self.assertNotIn("tools", payload)
        self.assertEqual("developer", payload["input"][0]["role"])
        self.assertIn("Authority: Draft", payload["input"][0]["content"])

    def test_openai_stream_sends_explicit_smoke_cap_to_transport(self):
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        dispatched = []

        def transport(payload):
            dispatched.append(payload)
            return iter(())

        default_adapter = OpenAIResponsesAdapter(lambda _: ())
        smoke_adapter = OpenAIResponsesAdapter(transport, max_output_tokens=512)
        other_adapter = OpenAIResponsesAdapter(lambda _: ())
        list(smoke_adapter.stream(record.request))
        self.assertEqual(1, len(dispatched))
        self.assertEqual(512, dispatched[0]["max_output_tokens"])
        self.assertEqual(2048, default_adapter.build_payload(record.request)["max_output_tokens"])
        self.assertEqual(2048, other_adapter.build_payload(record.request)["max_output_tokens"])
        self.assertEqual(2048, OpenAIResponsesAdapter.default_max_output_tokens)
        self.assertEqual("gpt-5.6-luna", dispatched[0]["model"])
        self.assertIs(dispatched[0]["store"], False)
        self.assertNotIn("tools", dispatched[0])
        self.assertEqual("developer", dispatched[0]["input"][0]["role"])
        self.assertIn("Authority: Draft", dispatched[0]["input"][0]["content"])

    def test_openai_output_cap_rejects_unbounded_or_invalid_values(self):
        for value in (None, True, 0, -1, 1.5, 128_001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OpenAIResponsesAdapter(lambda _: (), max_output_tokens=value)

    def test_openai_sse_is_normalized_and_malformed_input_fails(self):
        lines = [
            b'data: {"type":"response.output_text.delta","delta":"Draft"}\n',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":1}}}\n',
        ]
        self.assertEqual(["delta", "usage", "completion"], [item[0] for item in OpenAIResponsesAdapter._parse_sse(lines)])
        with self.assertRaises(ProviderUnavailable):
            list(OpenAIResponsesAdapter._parse_sse([b"data: {broken}\n"]))


class LiveSessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryAIRepository()
        self.live = LiveSessionService(self.repository)
        self.session = self.live.start("session_one", "campaign_one", "revision_one", "controller_one")

    def capture(self, operation="operation_one", kind=CaptureType.CONFIRMED_FACT, text="Door opened"):
        return self.live.capture("session_one", "controller_one", 1, self.session.workflow_version,
            event_id="event_" + operation.split("_")[-1], device_id="device_one", operation_id=operation,
            device_order=len(self.session.captures) + 1, capture_type=kind, text=text)

    def test_base_revision_stays_pinned_when_head_advances(self):
        self.live.observe("session_one", reported_head_revision="revision_two")
        self.assertEqual("revision_one", self.session.base_revision)
        self.assertEqual("revision_two", self.session.reported_head_revision)

    def test_question_is_never_grounding_fact(self):
        self.capture(kind=CaptureType.UNRESOLVED_QUESTION, text="Who opened it?")
        self.capture("operation_two", CaptureType.CONFIRMED_FACT, "Door opened")
        facts = self.live.grounding_facts("session_one")
        self.assertEqual(["Door opened"], [item.text for item in facts])

    def test_capture_is_idempotent_and_digest_conflict_fails(self):
        version = self.session.workflow_version
        outcome = self.capture()
        self.assertEqual("accepted", outcome)
        outcome = self.live.capture("session_one", "controller_one", 1, self.session.workflow_version,
            event_id="event_one", device_id="device_one", operation_id="operation_one", device_order=1,
            capture_type=CaptureType.CONFIRMED_FACT, text="Door opened")
        self.assertEqual("exact_replay", outcome)
        self.assertEqual(1, len(self.session.captures))
        with self.assertRaises(ValueError):
            self.live.capture("session_one", "controller_one", 1, self.session.workflow_version,
                event_id="event_one", device_id="device_one", operation_id="operation_one", device_order=1,
                capture_type=CaptureType.CONFIRMED_FACT, text="Different")
        with self.assertRaises(ValueError):
            self.live.capture("session_one", "controller_one", 1, self.session.workflow_version,
                event_id="changed_event", device_id="device_one", operation_id="operation_one", device_order=2,
                capture_type=CaptureType.CONFIRMED_FACT, text="Door opened")
        self.assertGreater(self.session.workflow_version, version)

    def test_stale_controller_and_workflow_fail_safely(self):
        with self.assertRaises(StaleController):
            self.live.capture("session_one", "other_controller", 1, 1, event_id="event_one", device_id="device_one", operation_id="operation_one", device_order=1, capture_type=CaptureType.CONFIRMED_FACT, text="Fact")
        with self.assertRaises(StaleWorkflow):
            self.live.capture("session_one", "controller_one", 1, 99, event_id="event_one", device_id="device_one", operation_id="operation_one", device_order=1, capture_type=CaptureType.CONFIRMED_FACT, text="Fact")
        self.assertEqual([], self.session.captures)

    def test_takeover_invalidates_old_controller(self):
        self.live.takeover("session_one", "controller_two", 1, self.session.workflow_version)
        with self.assertRaises(StaleController):
            self.live.capture("session_one", "controller_one", 1, self.session.workflow_version, event_id="event_one", device_id="device_one", operation_id="operation_one", device_order=1, capture_type=CaptureType.CONFIRMED_FACT, text="Fact")

    def test_provider_outage_does_not_block_typed_capture_or_end(self):
        self.assertEqual("accepted", self.capture())
        ended = self.live.end("session_one", "controller_one", 1, self.session.workflow_version, device_id="device_one", operation_id="end_one")
        self.assertEqual("ended_review_pending", ended.mode)
        self.assertEqual("revision_one", ended.base_revision)

    def test_recovery_disablement_preserves_existing_records(self):
        self.capture()
        self.live.disable()
        with self.assertRaises(RuntimeError):
            self.live.start("session_two", "campaign_one", "revision_one", "controller_one")
        self.assertEqual(1, len(self.session.captures))

    def test_only_one_active_session_and_no_capture_after_end(self):
        with self.assertRaises(ValueError):
            self.live.start("session_two", "campaign_one", "revision_one", "controller_two")
        self.live.end("session_one", "controller_one", 1, self.session.workflow_version, device_id="device_one", operation_id="end_one")
        with self.assertRaises(StaleWorkflow):
            self.live.capture("session_one", "controller_one", 1, self.session.workflow_version, event_id="event_one", device_id="device_one", operation_id="operation_one", device_order=1, capture_type=CaptureType.CONFIRMED_FACT, text="Fact")
        replay = self.live.end("session_one", "controller_one", 1, 1, device_id="device_one", operation_id="end_one")
        self.assertEqual("ended_review_pending", replay.mode)
        with self.assertRaises(StaleWorkflow):
            self.live.end("session_one", "controller_one", 1, self.session.workflow_version, device_id="device_one", operation_id="end_two")


def _entity_text(kind, entity_id, name):
    replacement = (DATA / "adapters" / "mothership" / "templates" / f"{kind}.md").read_text(encoding="utf-8")
    replacement = replacement.replace('id: ""', f"id: {entity_id}", 1)
    replacement = replacement.replace('name: ""', f'name: "{name}"', 1)
    return replacement.replace("# Name", f"# {name}", 1)


class EngineSourceLoaderTests(unittest.TestCase):
    def _stage_created_sources(self, engine, handle):
        changes = (
            ExactTextChange("change_ship", "ship-argo", None, _entity_text("ship", "ship-argo", "Argo"), ChangeKind.CREATE, "ship"),
            ExactTextChange("change_sam", "npc-sam", None, _entity_text("npc", "npc-sam", "Sam Rivers"), ChangeKind.CREATE, "npc"),
        )
        staged = engine.stage_exact_diff(
            StageExactDiffRequest("command_create_sources", handle, exact_diff_digest(changes), changes)
        )
        self.assertEqual(Status.STAGED, staged.status)
        return staged.staged_handle

    def test_natural_language_actions_retrieve_through_real_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = WorkspaceRegistry(Path(directory))
            engine = DeterministicEngine(registry)
            handle = registry.allocate()
            result = engine.initialize(InitializeRequest("command_initialize", handle, "Engine Test"))
            self.assertEqual(Status.STAGED, result.status)
            revisions = {
                "revision_one": handle,
                "revision_two": self._stage_created_sources(engine, handle),
            }
            calls = []

            def workspace_for_revision(campaign_id, revision_id):
                calls.append((campaign_id, revision_id))
                return revisions[revision_id]

            loader = EngineSourceLoader(engine, workspace_for_revision)
            requests = []
            retrieve = engine.retrieve

            def record_request(request):
                requests.append(request.subject_id)
                return retrieve(request)

            engine.retrieve = record_request
            cases = (
                ("What is the campaign called?", ["called", "campaign"], ["campaign-main"], ["campaign-main"]),
                ("Check the campaign name and the Argo", ["argo", "campaign", "check", "name"], ["campaign-main"], ["ship-argo", "campaign-main"]),
                ("Where is Sam?", ["sam"], [], ["npc-sam"]),
            )
            for prompt, tokens, base_subjects, staged_subjects in cases:
                for campaign_id, revision_id, expected in (
                    ("campaign_one", "revision_one", base_subjects),
                    ("campaign_two", "revision_two", staged_subjects),
                ):
                    with self.subTest(prompt=prompt, revision=revision_id):
                        requests.clear()
                        records = loader.load(campaign_id, revision_id, prompt)
                        self.assertEqual(tokens, requests)
                        self.assertEqual(expected, [item.subject_id for item in records])
                        self.assertEqual((campaign_id, revision_id), calls[-1])

    def test_relevance_keeps_named_source_in_noisy_campaign(self):
        noisy = tuple(Record(f"npc-person-{index:02d}", "canon", "The Person has status unknown") for index in range(25))
        ship = Record("ship-zeta", "canon", "Zeta is operational")

        class FakeEngine:
            def retrieve(self, request):
                records = noisy if request.subject_id == "status" else ((ship,) if request.subject_id == "zeta" else ())
                return SimpleNamespace(result=SimpleNamespace(status=Status.STAGED), records=tuple(records))

        loader = EngineSourceLoader(FakeEngine(), lambda campaign, revision: object())
        loaded = loader.load("campaign_one", "revision_one", "What is the status of the Zeta?")
        envelope = DeterministicSourceSelector(max_sources=20).select("campaign_one", "revision_one", loaded)
        self.assertIn("ship-zeta", [item.source_id for item in envelope.excerpts])
        self.assertLess(len([item for item in envelope.excerpts if item.source_id.startswith("npc-")]), 20)

    def test_source_envelope_enforces_excerpt_and_aggregate_bounds(self):
        records = [Record(f"source-{index}", "canon", str(index) * 20000) for index in range(6)]
        envelope = DeterministicSourceSelector().select("campaign_one", "revision_one", records)
        self.assertTrue(all(len(item.text) <= 8000 for item in envelope.excerpts))
        self.assertLessEqual(sum(len(item.text) for item in envelope.excerpts), 32000)
        repeated = DeterministicSourceSelector().select("campaign_one", "revision_one", reversed(records))
        self.assertEqual(envelope.source_set_digest, repeated.source_set_digest)


if __name__ == "__main__":
    unittest.main()
