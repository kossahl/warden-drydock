from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


class OnboardingContractTest(unittest.TestCase):
    def _bootstrap_sample(self, source):
        match = re.search(
            r"^```text\n(?P<sample>.*?)\n```$",
            source,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match)
        return match.group("sample")

    def test_bootstrap_contract_is_self_contained(self):
        text = (ROOT / "BOOTSTRAP.md").read_text(encoding="utf-8")
        sample = self._bootstrap_sample(text)
        normalized = re.sub(r"\s+", " ", text)

        required = [
            "Python 3.11",
            "temporary virtual environment",
            "python -m warden_drydock --version",
            "python -m warden_drydock bootstrap",
            "Confirm that it is empty",
            "do not initialize or commit Git",
            "Do not invent",
            "Remove the temporary virtual environment",
        ]
        for expectation in required:
            with self.subTest(expectation=expectation):
                self.assertIn(expectation, sample)

        repo_url = "https://github.com/kossahl/warden-drydock"
        self.assertIn(repo_url, sample)
        for url in re.findall(r"https://github\.com/[\w./@-]+", sample):
            with self.subTest(url=url):
                self.assertTrue(url.startswith(repo_url))

        install = re.search(
            r"pip install \"warden-drydock @ git\+https://"
            r"github\.com/kossahl/warden-drydock\.git@(?P<tag>v[0-9]+\.[0-9]+\.[0-9]+)\"",
            sample,
        )
        self.assertIsNotNone(install)
        tag = install.group("tag")
        version = tag[1:]
        self.assertIn(
            f"git+https://github.com/kossahl/warden-drydock.git@{tag}",
            sample,
        )
        self.assertIn(f"Warden Drydock {version}", sample)

        availability = text[text.find("## Availability gate"):]
        self.assertIn(tag, availability)

        expected_tokens = {tag, f"Warden Drydock {version}"}
        tokens = re.findall(
            r"v[0-9]+\.[0-9]+\.[0-9]+|Warden Drydock [0-9]+\.[0-9]+\.[0-9]+",
            text,
        )
        self.assertEqual(set(tokens), expected_tokens)

        start = normalized.find("Do not use the default branch")
        end = normalized.find("or invent campaign canon.", start)
        self.assertNotEqual(start, -1)
        self.assertNotEqual(end, -1)
        prohibition = normalized[start : end + len("or invent campaign canon.")]
        for phrase in (
            "use the default branch",
            "reconstruct the campaign layout manually",
            "invent campaign canon",
        ):
            with self.subTest(prohibited=phrase):
                self.assertIn(phrase, prohibition)
                self.assertEqual(normalized.count(phrase), prohibition.count(phrase))

    def _actionable_bootstrap_pointer(self, document, text, canonical):
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if target.startswith(("http:", "https:", "mailto:")):
                continue
            if (document.parent / target).resolve() == canonical:
                return True
        return bool(
            re.search(
                r"\bread[^.\n]*?BOOTSTRAP\.md[^.\n]*?\bbefore\b",
                text,
                re.IGNORECASE,
            )
        )

    def test_user_documentation_points_to_canonical_contract(self):
        canonical = (ROOT / "BOOTSTRAP.md").resolve()
        for relative in ("README.md", "docs/user-guide.md", "docs/ai-assisted-setup.md"):
            with self.subTest(path=relative):
                document = ROOT / relative
                text = document.read_text(encoding="utf-8")
                self.assertTrue(self._actionable_bootstrap_pointer(document, text, canonical))

    def test_ci_verifies_built_distribution_version_without_release_literal(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("importlib.metadata", workflow)
        self.assertIn("$PWD/dist", workflow)
        self.assertIn('cd "$ONBOARDING_ROOT"', workflow)
        self.assertIn("GITHUB_REF_NAME#v", workflow)
        self.assertIsNone(re.search(r"Warden Drydock \d+\.\d+\.\d+", workflow))

    def test_ci_workflow_policy_is_phase_aware(self):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

        self.assertEqual(set(workflow["on"]), {"pull_request", "push"})
        self.assertIsNone(workflow["on"]["pull_request"])
        self.assertEqual(
            workflow["on"]["push"],
            {"branches": ["master"], "tags": ["v*"]},
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})

        concurrency = workflow["concurrency"]
        self.assertEqual(
            " ".join(concurrency["group"].split()),
            "${{ github.event_name == 'pull_request' "
            "&& format('{0}-pr-{1}', github.workflow, "
            "github.event.pull_request.number) "
            "|| format('{0}-run-{1}', github.workflow, github.run_id) }}",
        )
        self.assertEqual(
            concurrency["cancel-in-progress"],
            "${{ github.event_name == 'pull_request' }}",
        )

        baseline = workflow["jobs"]["test"]
        self.assertNotIn("if", baseline)
        self.assertEqual(baseline["name"], "test (3.11)")
        self.assertGreater(baseline["timeout-minutes"], 0)
        baseline_setup = next(
            step
            for step in baseline["steps"]
            if step.get("uses") == "actions/setup-python@v5"
        )
        self.assertEqual(baseline_setup["with"]["python-version"], "3.11")
        baseline_checkout = next(
            step
            for step in baseline["steps"]
            if step.get("uses") == "actions/checkout@v4"
        )
        self.assertEqual(baseline_checkout["with"]["fetch-depth"], 0)
        baseline_runs = [step.get("run", "") for step in baseline["steps"]]
        self.assertEqual(
            sum("python -m unittest discover -s tests -v" in run for run in baseline_runs),
            1,
        )
        self.assertEqual(
            sum("python -m warden_drydock --help" in run for run in baseline_runs),
            1,
        )
        self.assertEqual(sum("python -m build" in run for run in baseline_runs), 1)
        self.assertEqual(
            sum(
                step.get("name") == "Smoke-test clean AI onboarding boundary"
                for step in baseline["steps"]
            ),
            1,
        )

        whitespace = next(
            step
            for step in baseline["steps"]
            if step.get("name") == "Check committed whitespace"
        )
        self.assertEqual(
            whitespace["env"],
            {
                "EVENT_NAME": "${{ github.event_name }}",
                "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
                "BEFORE_SHA": "${{ github.event.before }}",
            },
        )
        for command in (
            'git diff --check "$BASE_SHA...$HEAD_SHA"',
            'git diff --check "$BEFORE_SHA" "$GITHUB_SHA"',
            'git diff --check "$GITHUB_SHA^" "$GITHUB_SHA"',
            'git diff-tree --check --root "$GITHUB_SHA"',
        ):
            with self.subTest(command=command):
                self.assertIn(command, whitespace["run"])

        compatibility = workflow["jobs"]["compatibility"]
        self.assertEqual(compatibility["if"], "${{ github.event_name == 'push' }}")
        self.assertEqual(compatibility["name"], "compatibility (3.13)")
        self.assertGreater(compatibility["timeout-minutes"], 0)
        compatibility_setup = next(
            step
            for step in compatibility["steps"]
            if step.get("uses") == "actions/setup-python@v5"
        )
        self.assertEqual(compatibility_setup["with"]["python-version"], "3.13")
        configured_versions = {
            baseline_setup["with"]["python-version"],
            compatibility_setup["with"]["python-version"],
        }
        self.assertEqual(configured_versions, {"3.11", "3.13"})


if __name__ == "__main__":
    unittest.main()
