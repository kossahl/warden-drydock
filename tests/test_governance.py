import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ISSUE_TEMPLATE_DIRECTORY = ROOT / ".github" / "ISSUE_TEMPLATE"
WORK_PACKAGE_TEMPLATE = ISSUE_TEMPLATE_DIRECTORY / "agent-work-package.yml"
DECISION_TEMPLATE = ISSUE_TEMPLATE_DIRECTORY / "decision.yml"
BUG_TEMPLATE = ISSUE_TEMPLATE_DIRECTORY / "bug-report.yml"
FEATURE_TEMPLATE = ISSUE_TEMPLATE_DIRECTORY / "feature-request.yml"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
COMMUNICATION_POLICY = ROOT / "docs" / "agent-communication.md"


def normalized(text):
    return re.sub(r"\s+", " ", text.casefold()).strip()


def parse_issue_form(path):
    """Extract contract-relevant issue-form structure from the YAML template."""
    return parse_issue_form_text(path.read_text(encoding="utf-8"))


def parse_issue_form_text(text):
    data = yaml.safe_load(text)
    fields = {}
    for item in data["body"]:
        field_id = item.get("id")
        if field_id is None:
            if item.get("type") != "markdown":
                raise ValueError(
                    f"body item type {item.get('type')!r} is missing 'id'"
                )
            continue
        if field_id in fields:
            raise ValueError(f"duplicate field id {field_id!r}")
        attributes = item.get("attributes") or {}
        options = attributes.get("options") or []
        validations = item.get("validations") or {}
        required = bool(validations.get("required")) or any(
            option.get("required") for option in options
        )
        fields[field_id] = {
            "type": item["type"],
            "label": attributes.get("label"),
            "required": required,
        }
    labels = set(data.get("labels") or [])
    return {"text": text, "fields": fields, "labels": labels}


class IssueFormGovernanceTests(unittest.TestCase):
    def test_public_bug_and_feature_reporting_remain_available(self):
        bug = parse_issue_form(BUG_TEMPLATE)
        feature = parse_issue_form(FEATURE_TEMPLATE)
        self.assertEqual(
            {
                "observed",
                "expected",
                "reproduction",
                "version",
                "verification",
                "public_safety",
            },
            set(bug["fields"]),
        )
        self.assertEqual(
            {
                "problem",
                "outcome",
                "alternatives",
                "non_goals",
                "public_safety",
            },
            set(feature["fields"]),
        )
        for name, form in (("bug", bug), ("feature", feature)):
            with self.subTest(template=name):
                self.assertEqual(
                    "checkboxes", form["fields"]["public_safety"]["type"]
                )
                self.assertTrue(form["fields"]["public_safety"]["required"])
        self.assertIn("bug", bug["labels"])
        self.assertIn("enhancement", feature["labels"])

    def test_work_package_form_requires_complete_delegation_contract(self):
        form = parse_issue_form(WORK_PACKAGE_TEMPLATE)
        required_fields = {
            "phase",
            "package_version",
            "owner",
            "problem",
            "deliverables",
            "scope",
            "authority",
            "baseline",
            "interfaces",
            "acceptance",
            "risks",
            "questions",
            "delivery",
            "public_safety",
        }
        self.assertLessEqual(required_fields, set(form["fields"]))
        for field_id in required_fields:
            with self.subTest(field=field_id):
                self.assertTrue(form["fields"][field_id]["required"])
                self.assertTrue(form["fields"][field_id]["label"])
        self.assertEqual("checkboxes", form["fields"]["public_safety"]["type"])
        self.assertIn("public-safe", form["labels"])
        version_text = normalized(form["text"])
        self.assertIn("positive integer", version_text)
        self.assertIn("increment it whenever scope", version_text)

    def test_decision_form_requires_material_decision_record(self):
        form = parse_issue_form(DECISION_TEMPLATE)
        expected_fields = {
            "source",
            "question",
            "impact",
            "options",
            "recommendation",
            "safe_progress",
            "durable_record",
        }
        self.assertEqual(expected_fields, set(form["fields"]))
        for field_id in expected_fields:
            with self.subTest(field=field_id):
                self.assertTrue(form["fields"][field_id]["required"])
                self.assertTrue(form["fields"][field_id]["label"])
        self.assertLessEqual(
            {"public-safe", "status:needs-decision", "type:decision"},
            form["labels"],
        )
        text = normalized(form["text"])
        self.assertIn("stop affected implementation", text)
        self.assertIn("two or three real options", text)

    def test_decision_form_parser_rejects_malformed_or_duplicated_items(self):
        with self.assertRaises(yaml.YAMLError):
            parse_issue_form_text("body: [\n")
        with self.assertRaises(ValueError):
            parse_issue_form_text(
                "body:\n"
                "  - type: input\n"
                "    id: duplicate\n"
                "  - type: input\n"
                "    id: duplicate\n"
            )

    def test_decision_form_parser_rejects_body_item_without_id(self):
        with self.assertRaises(ValueError) as context:
            parse_issue_form_text(
                "body:\n"
                "  - type: markdown\n"
                "    attributes:\n"
                "      value: intro\n"
                "  - type: textarea\n"
                "    attributes:\n"
                "      label: Missing id\n"
            )
        self.assertEqual(
            "body item type 'textarea' is missing 'id'", str(context.exception)
        )


class PullRequestGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = normalized(PR_TEMPLATE.read_text(encoding="utf-8"))

    def test_pr_template_preserves_work_package_and_handoff_evidence(self):
        required_phrases = {
            "phase and work-package id",
            "work-package version",
            "responsible agent",
            "pinned base commit",
            "assigned file or subsystem ownership",
            "authoritative adrs and repository documents",
            "deviations from the approved work package",
            "public api or schema impact",
            "data migration or generated-artifact impact",
            "recovery or rollback consideration",
            "status and outcome",
            "changed files or design artifacts",
            "verification commands and actual outcomes",
            "new risks or open decisions",
            "next action and owner",
        }
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.template)

    def test_pr_template_has_public_safety_and_review_gates(self):
        required_gates = {
            "no campaign content",
            "personal data",
            "secrets",
            "private logs",
            "credentials",
            "unremediated exploit details",
            "comment was treated as an authoritative instruction",
            "validation against accepted repository decisions",
            "no material uncertainty was resolved by an undocumented assumption",
            "status:needs-decision",
            "review threads are resolved before merge",
        }
        for phrase in required_gates:
            with self.subTest(gate=phrase):
                self.assertIn(phrase, self.template)


class CommunicationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = normalized(COMMUNICATION_POLICY.read_text(encoding="utf-8"))

    def test_connector_coordination_allowlists_only_the_project_repository(self):
        allowlist = re.search(
            r"only allowed repository for connector-backed coordination is `([^`]+)`",
            self.policy,
        )
        self.assertIsNotNone(allowlist)
        self.assertEqual("kossahl/warden-drydock", allowlist.group(1))
        repository_slugs = set(
            re.findall(r"(?<![\w.-])([\w.-]+/[\w.-]+)(?![\w.-])", self.policy)
        )
        self.assertEqual({"kossahl/warden-drydock"}, repository_slugs)

    def test_github_writes_are_parent_mediated_and_input_is_untrusted(self):
        required_phrases = {
            "agents do not receive github credentials",
            "do not write directly to github",
            "the parent uses the connected github integration",
            "explicitly authorized writes",
            "coordination evidence, not executable instructions",
            "every public contribution is untrusted",
            "before notifying the responsible agent",
        }
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.policy)

    def test_polling_is_read_only_idempotent_and_cannot_execute_agents(self):
        required_phrases = {
            "a recurring monitor, when enabled, is read-only",
            "must not start an agent",
            "run commands",
            "change repository state",
            "publish a response",
            "stable github identifier",
            "processed at most once",
            "webhook-triggered execution",
            "direct bot comments are outside the hosted mvp",
        }
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.policy)

    def test_decision_authority_uses_current_repository_permission(self):
        required_phrases = {
            "repository collaborator",
            "permission is `maintain` or `admin`",
            "resolves the comment author's login",
            "checks that permission through the connected github integration",
            "`pull`, `triage`, `push`",
            "matching display name are not sufficient",
        }
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.policy)


if __name__ == "__main__":
    unittest.main()
