import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.ai_review import _merge_targeted_checks


def fc(pid, sid, verdict="qualified"):
    return {"parent_claim_id": pid, "subclaim_id": sid, "verdict": verdict}


class TargetedMergeEchoTests(unittest.TestCase):
    def scope(self):
        return {"affected_units": ["U0002"], "previous_review": {
            "fact_checks": [fc("U0001-C01", "U0001-C01-F01"), fc("U0002-C01", "U0002-C01-F01")],
            "issues": []}}

    def test_echoed_retained_check_is_dropped(self):
        review = {"fact_checks": [fc("U0001-C01", "U0001-C01-F01", "supported"),
                                  fc("U0002-C01", "U0002-C01-F01", "supported")]}
        merged = _merge_targeted_checks(review, self.scope())
        self.assertEqual(len(merged["fact_checks"]), 2)
        self.assertEqual(merged["fact_checks"][0]["verdict"], "qualified")

    def test_new_out_of_scope_check_still_rejected(self):
        review = {"fact_checks": [fc("U0003-C01", "U0003-C01-F01")]}
        with self.assertRaisesRegex(RuntimeError, "范围外"):
            _merge_targeted_checks(review, self.scope())


if __name__ == "__main__":
    unittest.main()
