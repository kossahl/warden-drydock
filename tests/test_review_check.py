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
        self.assertIn('[ "$attribute_path" = "scripts/review-check.sh" ]', source)
        self.assertIn('[ "$attribute_name" = "eol" ]', source)
        self.assertIn('[ "$attribute_value" = "lf" ]', source)


if __name__ == "__main__":
    unittest.main()
