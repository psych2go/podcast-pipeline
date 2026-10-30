import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import agent_pipeline as ap


class ClaimProgressResumeTests(unittest.TestCase):
    def test_missing_progress_is_current(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text('{"segments": []}', encoding="utf-8")
            self.assertTrue(ap._claim_progress_is_current(folder, folder / "transcript.raw.json"))

    def test_failed_progress_forces_resume(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            raw = folder / "transcript.raw.json"
            raw.write_text('{"segments": []}', encoding="utf-8")
            (folder / "claim_evidence_progress.json").write_text(json.dumps({
                "schema_version": 1, "status": "invalid_result",
                "target_unit_ids": ["U0001", "U0002"],
                "completed_unit_ids": ["U0001"],
                "pending_unit_ids": ["U0002"], "failed_unit_ids": ["U0002"],
            }), encoding="utf-8")
            self.assertFalse(ap._claim_progress_is_current(folder, raw))


if __name__ == "__main__":
    unittest.main()
