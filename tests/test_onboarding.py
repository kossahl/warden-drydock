from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OnboardingContractTest(unittest.TestCase):
    def test_bootstrap_contract_is_self_contained(self):
        contract = (ROOT / "BOOTSTRAP.md").read_text(encoding="utf-8")
        required = [
            "https://github.com/kossahl/warden-drydock",
            "v0.1.0",
            "Python 3.11",
            "temporary virtual environment",
            "git+https://github.com/kossahl/warden-drydock.git@v0.1.0",
            "python -m warden_drydock --version",
            "Warden Drydock 0.1.0",
            "python -m warden_drydock bootstrap",
            "Confirm that it is empty",
            "do not initialize or commit Git",
            "Do not invent",
            "Remove the temporary virtual environment",
        ]
        for expectation in required:
            with self.subTest(expectation=expectation):
                self.assertIn(expectation, contract)

    def test_user_documentation_points_to_canonical_contract(self):
        for relative in ("README.md", "docs/user-guide.md", "docs/ai-assisted-setup.md"):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("BOOTSTRAP.md", text)


if __name__ == "__main__":
    unittest.main()
