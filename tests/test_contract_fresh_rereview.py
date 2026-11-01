import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import ai_review, review_contract_repair as repair, review_repair


class ContractFreshReReviewTests(unittest.TestCase):
    def test_targetless_contract_error_gets_one_fresh_review(self):
        with tempfile.TemporaryDirectory() as td:
            exc = ai_review.ReviewContractError("mode mismatch", {"original_output": {}})
            reviewer = Mock(side_effect=[exc, {"passed": True}])
            with patch.object(repair, "repair_contract_failure",
                              side_effect=ValueError("没有可自动修复的合同阻断项")):
                result = review_repair.review_and_repair(td, reviewer=reviewer)
            self.assertTrue(result["passed"])
            self.assertEqual(reviewer.call_count, 2)
            history = json.loads((Path(td) / "review_repair.json").read_text())["history"]
            self.assertEqual(history[0]["repair_actions"][0]["action"], "fresh_re_review")

    def test_targetless_contract_error_still_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            exc = ai_review.ReviewContractError("mode mismatch", {"original_output": {}})
            reviewer = Mock(side_effect=exc)
            with patch.object(repair, "repair_contract_failure",
                              side_effect=ValueError("没有可自动修复的合同阻断项")):
                with self.assertRaises(ai_review.ReviewContractError):
                    review_repair.review_and_repair(td, reviewer=reviewer, max_rounds=2)
            self.assertEqual(reviewer.call_count, 3)


if __name__ == "__main__":
    unittest.main()
