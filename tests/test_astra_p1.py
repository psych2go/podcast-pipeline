from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.artifact_manifest import build_artifact_manifest, write_artifact_manifest
from scripts.snapshot import canonical_snapshot, snapshot_sha256
from scripts.preflight import review_recovery_classification


class AstraP1Tests(unittest.TestCase):
    def test_manifest_marks_downstream_stale_after_transcript_change(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "转录_纠错.txt").write_text("a")
            (p / "讲书稿.md").write_text("b")
            write_artifact_manifest(p)
            (p / "转录_纠错.txt").write_text("changed")
            m = build_artifact_manifest(p)
            self.assertEqual(m["artifacts"]["briefing"]["status"], "stale")
            self.assertIn("corrected_transcript", m["artifacts"]["briefing"]["stale_reason"])

    def test_snapshot_changes_when_review_or_quality_changes(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "ai_review.json").write_text("{}")
            a = snapshot_sha256(p)
            (p / "quality_report.json").write_text("{}")
            self.assertNotEqual(a, snapshot_sha256(p))
            self.assertIn("ai_review.json", canonical_snapshot(p))

    def test_repeated_static_failure_is_short_circuited(self):
        r = {"error_details": [{"code": "contract_failed"}], "review_input_sha256": "x"}
        kind, state = review_recovery_classification(r)
        self.assertEqual(kind, "checker_suspect")
        self.assertEqual(review_recovery_classification(r, state)[0], "content_change_required")

    def test_transient_runner_failure_remains_retryable(self):
        kind, _ = review_recovery_classification({"error_details": [{"code": "empty_output"}]})
        self.assertEqual(kind, "runner_transient")


if __name__ == "__main__":
    unittest.main()
