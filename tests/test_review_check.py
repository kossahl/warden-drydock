from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReviewCheckTests(unittest.TestCase):
    def test_review_check_is_bom_free_lf_and_has_tracked_lf_policy(self) -> None:
        script = (ROOT / "scripts" / "review-check.sh").read_bytes()
        self.assertFalse(script.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", script)
        self.assertEqual(
            0,
            subprocess.run(
                ["bash", "-n", str(ROOT / "scripts" / "review-check.sh")],
                check=False,
            ).returncode,
        )
        self.assertIn(
            "scripts/review-check.sh text eol=lf",
            (ROOT / ".gitattributes").read_text(encoding="utf-8"),
        )

    def test_source_guard_allows_only_the_deliberate_lf_attribute(self) -> None:
        source = (ROOT / "scripts" / "review-check.sh").read_text(encoding="utf-8")
        start = source.index("is_allowed_eol_attribute() {")
        end = source.index("\n}", start) + 2
        guard = source[start:end]

        def guard_result(path: str, name: str, value: str) -> int:
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    f"{guard}\nis_allowed_eol_attribute \"$1\" \"$2\" \"$3\"",
                    "guard-test",
                    path,
                    name,
                    value,
                ],
                check=False,
            ).returncode

        self.assertEqual(
            0,
            guard_result("scripts/review-check.sh", "eol", "lf"),
        )
        for malformed in (
            ("scripts/review-check.sh", "eol", "crlf"),
            ("scripts/review-check.sh", "text", "lf"),
            ("scripts/other.sh", "eol", "lf"),
            ("scripts/review-check.sh", "eol", "lf extra"),
        ):
            with self.subTest(attribute=malformed):
                self.assertNotEqual(0, guard_result(*malformed))


if __name__ == "__main__":
    unittest.main()
