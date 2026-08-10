from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


EXPECTED_WHEEL_SHA256 = "ec25e08253c4cd263027805a5794ea8a379475e8ac755c51faa487aa69a8e38f"
ROOT = Path(__file__).parents[1]
WHEEL_ENV = "AGENT_ASCENDRY_WHEEL"


LEGACY_HOOK = {
    "hooks": [
        {
            "type": "command",
            "command": "python .codex/hooks/capture_agent_experience.py",
            "commandWindows": (
                "powershell.exe -NoProfile -ExecutionPolicy Bypass "
                "-File .codex\\hooks\\capture_agent_experience.ps1"
            ),
            "statusMessage": "Recording agent experience",
            "timeout": 3,
        }
    ]
}
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


class AgentAscendryWheelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get(WHEEL_ENV)
        if not configured:
            raise unittest.SkipTest(f"set {WHEEL_ENV} to the accepted candidate wheel")
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
                ".codex/hooks/capture_agent_experience.py": b"# legacy Python hook\n",
                ".codex/hooks/capture_agent_experience.ps1": b"# legacy PowerShell hook\n",
                ".codex/hooks/agent_experience_maintenance.py": b"# legacy audit\n",
                ".codex/hooks.json": (
                    json.dumps(
                        {
                            "description": "Existing Drydock registry",
                            "hooks": {"Stop": [LEGACY_HOOK]},
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode(),
            }
            for relative, content in existing.items():
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)

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
            self.assertEqual(first_result["created"], [
                ".agent-ascendry/installation.json",
                ".codex/hooks/agent_ascendry_capture.py",
            ])
            self.assertEqual(first_result["owned"], [".codex/hooks/agent_ascendry_capture.py"])
            self.assertIn(".codex/agents/agent_curator.toml", first_result["reused"])
            for relative, content in existing.items():
                if relative != ".codex/hooks.json":
                    self.assertEqual((root / relative).read_bytes(), content, relative)
            self.assertFalse((root / ".codex/agents/agent_ascendry_curator.toml").exists())
            self.assertFalse((root / ".agents/skills/curate-agent-evolution").exists())

            registry = json.loads((root / ".codex/hooks.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["hooks"]["Stop"], [LEGACY_HOOK, ASCENDRY_HOOK])
            exclude = subprocess.run(
                ["git", "check-ignore", "-v", ".agent-ascendry/installation.json"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("/.agent-ascendry/", exclude.stdout)

            after_first_init = file_tree(root)
            second, second_result = self.run_ascendry(root, "init", ".", "--platform", "codex")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second_result["created"], [])
            self.assertEqual(file_tree(root), after_first_init)

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
