import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import prewrite_fact_checks as pfc


def _inv():
    return [
        {"parent_claim_id": "U0001-C01", "source_claim": "甲 说 A"},
        {"parent_claim_id": "U0001-C02", "source_claim": "乙说B"},
        {"parent_claim_id": "U0002-C01", "source_claim": "丙说C"},
    ]


def _rec(pid, text):
    return {"parent_claim_id": pid, "source_claim": text, "checks": [{}]}


class BatchAlignTests(unittest.TestCase):
    def test_align_reorders_and_normalizes_whitespace(self):
        payload = {"claims": [
            _rec("U0002-C01", "丙说C"),
            _rec("U0001-C01", "甲  说 A "),
            _rec("U9999-C01", "extra"),
        ]}
        matched, missing = pfc._align_batch_claims(payload, _inv())
        self.assertEqual(missing, [1])
        self.assertEqual(matched[0]["source_claim"], "甲 说 A")
        self.assertEqual(matched[2]["parent_claim_id"], "U0002-C01")

    def test_unique_parent_id_restores_drifted_source_claim(self):
        payload = {"claims": [_rec("U0001-C02", "乙说 B（改写）")]}
        matched, missing = pfc._align_batch_claims(payload, _inv())
        self.assertIn(1, matched)
        self.assertEqual(matched[1]["source_claim"], "乙说B")

    def test_batch_retries_only_missing_claims_and_merges(self):
        calls = []

        def fake_run(_folder, task, _schema, **kwargs):
            calls.append(task)
            if len(calls) == 1:
                claims = [_rec("U0001-C01", "甲 说 A"), _rec("U0002-C01", "丙说C")]
                issues = [{"parent_claim_id": "U0002-C01"}]
            else:
                claims = [_rec("U0001-C02", "乙说B")]
                issues = []
            return {"payload": {"claims": claims, "issue_inventory": issues,
                                "summary": {}}, "duration_ms": 1}

        with tempfile.TemporaryDirectory() as td, \
                patch.object(pfc, "_prompt", side_effect=lambda f, inv, *a: str(
                    [i["parent_claim_id"] for i in inv])), \
                patch.object(pfc, "run_json_task", side_effect=fake_run):
            result = pfc._run_fact_check_batch(
                Path(td), _inv(), "h", {}, None, "low", 4)
        self.assertEqual(len(calls), 2)
        self.assertIn("U0001-C02", calls[1])
        self.assertNotIn("U0002-C01", calls[1])
        payload = result["payload"]
        self.assertEqual(pfc._batch_pairs(payload),
                         [(i["parent_claim_id"], i["source_claim"]) for i in _inv()])
        self.assertEqual(payload["summary"]["claim_count"], 3)
        self.assertTrue(payload["summary"]["exhaustive_inventory_completed"])
        self.assertEqual(len(payload["issue_inventory"]), 1)

    def test_batch_fails_after_three_incomplete_attempts(self):
        def fake_run(*_a, **_k):
            return {"payload": {"claims": [], "issue_inventory": [],
                                "summary": {}}}

        with tempfile.TemporaryDirectory() as td, \
                patch.object(pfc, "_prompt", return_value="t"), \
                patch.object(pfc, "run_json_task", side_effect=fake_run) as run:
            with self.assertRaisesRegex(RuntimeError, "未完整覆盖"):
                pfc._run_fact_check_batch(Path(td), _inv(), "h", {}, None, "low", 4)
        self.assertEqual(run.call_count, 3)


if __name__ == "__main__":
    unittest.main()
