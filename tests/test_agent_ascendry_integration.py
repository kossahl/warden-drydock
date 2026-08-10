from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RELEASE_WHEEL_URL = (
    "https://github.com/kossahl/agent-ascendry/releases/download/v0.1.0/"
    "agent_ascendry-0.1.0-py3-none-any.whl"
)
EXPECTED_WHEEL_SHA256 = "3b4efdc3416d48a7dc5892d35fe8d55dfd3d27afdc2da4aaef161ce121726a73"
RELEASE_SOURCE_COMMIT = "ed383bae871e15d28ab69bb60b0cfcc7e3a5296b"
ROOT = Path(__file__).parents[1]
WHEEL_ENV = "AGENT_ASCENDRY_WHEEL"


ASCENDRY_HOOK = {
    "hooks": [
        {
            "type": "command",
            "command": "python .codex/hooks/agent_ascendry_capture.py",
            "commandWindows": "python .codex\\hooks\\agent_ascendry_capture.py",
            "statusMessage": "Recording Agent Ascendry experience",
            "timeout": 5,
        }
    ]
}


def file_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


class AgentAscendryReleasePinTests(unittest.TestCase):
    def test_integration_document_matches_the_immutable_release_pin(self):
        documentation = (ROOT / "docs/agent-ascendry-integration.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(RELEASE_WHEEL_URL, documentation)
        self.assertIn(EXPECTED_WHEEL_SHA256, documentation)
        self.assertIn(RELEASE_SOURCE_COMMIT, documentation)
        self.assertIn("v0.1.0", documentation)


class AgentAscendryWheelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get(WHEEL_ENV)
        if not configured:
            raise unittest.SkipTest(f"set {WHEEL_ENV} to the published v0.1.0 wheel")
        cls.wheel = Path(configured).resolve()
        if not cls.wheel.is_file():
            raise AssertionError(f"{WHEEL_ENV} does not name a file")
        digest = hashlib.sha256(cls.wheel.read_bytes()).hexdigest()
        if digest != EXPECTED_WHEEL_SHA256:
            raise AssertionError(f"unexpected Agent Ascendry wheel SHA-256: {digest}")

        cls.installation = tempfile.TemporaryDirectory()
        cls.package_root = Path(cls.installation.name) / "site-packages"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--target",
                str(cls.package_root),
                str(cls.wheel),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "installation"):
            cls.installation.cleanup()

    def run_ascendry(self, root: Path, *arguments: str, payload=None):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.package_root)
        completed = subprocess.run(
            [sys.executable, "-m", "agent_ascendry", *arguments],
            cwd=root,
            input=None if payload is None else json.dumps(payload),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        parsed = json.loads(stream)
        return completed, parsed

    def test_zero_touch_drydock_style_lifecycle_uses_only_the_wheel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            subprocess.run(
                ["git", "init", "--initial-branch=master", str(root)],
                capture_output=True,
                check=True,
            )

            config = (ROOT / ".agent-ascendry.yaml").read_bytes()
            existing = {
                ".agent-ascendry.yaml": config,
                ".codex/agents/agent_curator.toml": b'name = "agent_curator"\n',
                ".codex/agents/reviewer.toml": b'name = "reviewer"\n',
                ".agents/skills/improve-drydock-agents/SKILL.md": b"# Existing Drydock skill\n",
                ".codex/hooks/agent_ascendry_capture.py": (
                    ROOT / ".codex/hooks/agent_ascendry_capture.py"
                ).read_bytes(),
                ".codex/hooks.json": (ROOT / ".codex/hooks.json").read_bytes(),
            }
            for relative, content in existing.items():
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)

            subprocess.run(
                ["git", "config", "user.name", "Agent Ascendry Test"],
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "agent-ascendry@example.invalid"],
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "add", "--", *sorted(existing)],
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Create tracked Drydock fixture"],
                cwd=root,
                capture_output=True,
                check=True,
            )
            tracked = subprocess.run(
                ["git", "ls-files"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(set(tracked.stdout.splitlines()), set(existing))
            before_init_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(before_init_status.stdout, "")

            import_probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import agent_ascendry; print(agent_ascendry.__file__)",
                ],
                cwd=root,
                env={**os.environ, "PYTHONPATH": str(self.package_root)},
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertTrue(
                Path(import_probe.stdout.strip()).resolve().is_relative_to(
                    self.package_root.resolve()
                )
            )

            first, first_result = self.run_ascendry(root, "init", ".", "--platform", "codex")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first_result["created"], [".agent-ascendry/installation.json"])
            self.assertEqual(first_result["owned"], [])
            self.assertIn(".codex/agents/agent_curator.toml", first_result["reused"])
            self.assertIn(".codex/hooks.json", first_result["reused"])
            self.assertIn(".codex/hooks/agent_ascendry_capture.py", first_result["reused"])
            for relative, content in existing.items():
                self.assertEqual((root / relative).read_bytes(), content, relative)
            ownership = json.loads(
                (root / ".agent-ascendry/installation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(ownership["assets"], {})
            self.assertIsNone(ownership["hook_block"])
            self.assertFalse((root / ".codex/agents/agent_ascendry_curator.toml").exists())
            self.assertFalse((root / ".agents/skills/curate-agent-evolution").exists())

            registry = json.loads((root / ".codex/hooks.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["hooks"]["Stop"], [ASCENDRY_HOOK])
            exclude = subprocess.run(
                ["git", "check-ignore", "-v", ".agent-ascendry/installation.json"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("/.agent-ascendry/", exclude.stdout)
            after_init_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(after_init_status.stdout, "")

            after_first_init = file_tree(root)
            second, second_result = self.run_ascendry(root, "init", ".", "--platform", "codex")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second_result["created"], [])
            self.assertEqual(file_tree(root), after_first_init)
            after_second_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(after_second_status.stdout, "")

            validate, validation = self.run_ascendry(root, "validate", ".")
            self.assertEqual(validate.returncode, 0, validate.stderr)
            self.assertTrue(all(check["status"] == "pass" for check in validation["checks"]))

            payload = {
                "session_id": "drydock-pilot",
                "turn_id": "capture-1",
                "hook_event_name": "Stop",
                "agent": "agent_curator",
                "skills": ["improve-drydock-agents"],
                "outcome": "completed",
                "test_status": "passed",
                "last_assistant_message": "must not be persisted",
            }
            capture, captured = self.run_ascendry(root, "capture", ".", payload=payload)
            self.assertEqual(capture.returncode, 0, capture.stderr)
            self.assertTrue(captured["created"])
            repeated, repeated_result = self.run_ascendry(root, "capture", ".", payload=payload)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertFalse(repeated_result["created"])
            self.assertEqual(repeated_result["event_id"], captured["event_id"])
            event = json.loads((root / captured["path"]).read_text(encoding="utf-8"))
            self.assertEqual(event["agent"], "agent_curator")
            self.assertEqual(event["skills"], ["improve-drydock-agents"])
            self.assertNotIn("last_assistant_message", event)
            self.assertNotIn("must not be persisted", json.dumps(event))

            wrapper_environment = {
                **os.environ,
                "PYTHONPATH": str(self.package_root),
            }
            wrapper = root / ".codex/hooks/agent_ascendry_capture.py"
            hook_capture = subprocess.run(
                [sys.executable, str(wrapper)],
                cwd=root,
                input=json.dumps({
                    "session_id": "drydock-pilot",
                    "turn_id": "capture-hook",
                    "hook_event_name": "Stop",
                    "agent": "agent_curator",
                }),
                capture_output=True,
                text=True,
                env=wrapper_environment,
                check=False,
            )
            self.assertEqual(hook_capture.returncode, 0)
            self.assertEqual(hook_capture.stdout, "")
            self.assertEqual(hook_capture.stderr, "")
            failed_hook = subprocess.run(
                [sys.executable, str(wrapper)],
                cwd=root,
                input="{",
                capture_output=True,
                text=True,
                env=wrapper_environment,
                check=False,
            )
            self.assertEqual(failed_hook.returncode, 0)
            self.assertEqual(failed_hook.stdout, "")
            self.assertEqual(
                failed_hook.stderr.strip(),
                "Agent Ascendry capture failed; run agent-ascendry validate .",
            )

            audit, audited = self.run_ascendry(root, "audit", ".")
            self.assertEqual(audit.returncode, 0, audit.stderr)
            self.assertEqual(audited["report"]["counts"]["valid_v2"], 2)
            self.assertIsNone(audited["report"]["queue_error"])
            self.assertIsNone(audited["report"]["processed_state_error"])


if __name__ == "__main__":
    unittest.main()
