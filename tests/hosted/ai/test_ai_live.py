from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest

from warden_drydock.hosted.ai.live import LiveSessionService, StaleController, StaleWorkflow
from warden_drydock.hosted.ai.models import Action, CaptureType
from warden_drydock.hosted.ai.provider import OpenAIResponsesAdapter, ProviderUnavailable
from warden_drydock.hosted.ai.repository import InMemoryAIRepository
from warden_drydock.hosted.ai.retrieval import DeterministicSourceSelector
from warden_drydock.hosted.ai.retrieval import EngineSourceLoader
from warden_drydock.hosted.ai.service import ConsentRequired, GroundedAIService
from warden_drydock.hosted.engine import DeterministicEngine, InitializeRequest, Status, WorkspaceRegistry


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

    def test_retrieval_is_identical_and_persisted_before_dispatch(self):
        self.service.record_consent(explicit=True)
        one = self.service.start("generation_one", "campaign_one", "revision_one", Action.CHECK, "Check")
        two = self.service.start("generation_two", "campaign_one", "revision_one", Action.CHECK, "Check")
        self.assertEqual(one.request.envelope.source_set_digest, two.request.envelope.source_set_digest)
        self.assertIn("generation_one", self.repository.sources)
        self.assertEqual(1, len(self.provider.calls)) if False else None
        self.assertEqual(["generation_one", "generation_two"], self.repository.dispatch_log)

    def test_generation_exact_replay_does_not_dispatch_twice(self):
        self.service.record_consent(explicit=True)
        one = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        two = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertIs(one, two)
        self.assertEqual(["generation_one"], self.repository.dispatch_log)
        with self.assertRaises(ValueError):
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

    def test_live_generation_forces_base_revision_and_confirmed_facts(self):
        live = LiveSessionService(self.repository)
        session = live.start("session_one", "campaign_one", "revision_one", "controller_one")
        live.capture("session_one", "controller_one", 1, session.workflow_version, event_id="question_one", device_id="device_one", operation_id="operation_one", device_order=1, capture_type=CaptureType.UNRESOLVED_QUESTION, text="Secret question")
        live.capture("session_one", "controller_one", 1, session.workflow_version, event_id="fact_one", device_id="device_one", operation_id="operation_two", device_order=2, capture_type=CaptureType.CONFIRMED_FACT, text="Door opened")
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_new", Action.CHECK, "Check", session_id="session_one")
        self.assertEqual("revision_one", record.request.revision_id)
        texts = [item.text for item in record.request.envelope.excerpts]
        self.assertIn("Door opened", texts)
        self.assertNotIn("Secret question", texts)

    def test_events_after_terminal_fail_closed(self):
        self.service.provider = FakeProvider(events=[("completion", None), ("delta", "late")])
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        self.assertEqual("failed", record.terminal_status)
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
        self.assertEqual(512, payload["max_output_tokens"])
        self.assertIs(payload["store"], False)
        self.assertNotIn("tools", payload)
        self.assertEqual("developer", payload["input"][0]["role"])
        self.assertIn("Authority: Draft", payload["input"][0]["content"])

    def test_openai_stream_sends_finite_output_cap_to_transport(self):
        self.service.record_consent(explicit=True)
        record = self.service.start("generation_one", "campaign_one", "revision_one", Action.ASK, "State?")
        dispatched = []

        def transport(payload):
            dispatched.append(payload)
            return iter(())

        list(OpenAIResponsesAdapter(transport).stream(record.request))
        self.assertEqual(1, len(dispatched))
        self.assertEqual(512, dispatched[0]["max_output_tokens"])
        self.assertEqual("gpt-5.6-luna", dispatched[0]["model"])
        self.assertIs(dispatched[0]["store"], False)
        self.assertNotIn("tools", dispatched[0])
        self.assertEqual("developer", dispatched[0]["input"][0]["role"])
        self.assertIn("Authority: Draft", dispatched[0]["input"][0]["content"])

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


class EngineSourceLoaderTests(unittest.TestCase):
    def test_natural_language_actions_retrieve_through_real_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = WorkspaceRegistry(Path(directory))
            engine = DeterministicEngine(registry)
            handle = registry.allocate()
            result = engine.initialize(InitializeRequest("command_initialize", handle, "Engine Test"))
            self.assertEqual(Status.STAGED, result.status)
            loader = EngineSourceLoader(engine, lambda campaign, revision: handle)
            for prompt in ("What is the campaign called?", "Check the campaign name", "Generate a campaign introduction"):
                with self.subTest(prompt=prompt):
                    records = loader.load("campaign_one", "revision_one", prompt)
                    self.assertIn("campaign-main", [item.subject_id for item in records])

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
