import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import review_contract_repair as repair


class SourcePurposeRebindTests(unittest.TestCase):
    def _run(self, source, targets):
        with patch.object(repair, "run_json_task", return_value={
                "payload": {"sources": [source]}}), \
                patch.object(repair, "_fetch_source", return_value={
                    "excerpt": source["quote"], "content_sha256": "h"}):
            return repair.lookup_sources(Path("/tmp"), targets)

    def test_mislabelled_purpose_for_targeted_claim_is_rebound(self):
        source = {"parent_claim_id": "U0002-C01", "purpose": "public_source",
                  "url": "https://example.org/study",
                  "quote": "Olfactory receptor neurons are bipolar neurons.",
                  "explanation": "study"}
        targets = [{"parent_claim_id": "U0002-C01",
                    "repair_reason": "firsthand_attribution"}]
        accepted, rejected = self._run(source, targets)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["purpose"], "firsthand_attribution")
        self.assertEqual(accepted[0]["original_purpose"], "public_source")
        self.assertFalse(rejected)

    def test_untargeted_claim_still_rejected(self):
        source = {"parent_claim_id": "U0009-C01", "purpose": "public_source",
                  "url": "https://example.org/x", "quote": "a sufficiently long quote here",
                  "explanation": ""}
        targets = [{"parent_claim_id": "U0002-C01", "repair_reason": "public_source"}]
        accepted, rejected = self._run(source, targets)
        self.assertEqual(accepted, [])
        self.assertIn("来源不属于本轮目标", rejected[0]["error"])


if __name__ == "__main__":
    unittest.main()
