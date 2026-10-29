import unittest

from scripts.ai_review import _deterministic_mode_fixes


class EpisodeMetadataModeFixTest(unittest.TestCase):
    def test_episode_metadata_forced_to_transcript_only(self):
        review = {"fact_checks": [{
            "claim": "节目介绍嗅觉系统", "parent_claim_id": "U0001-C01",
            "subclaim_id": "U0001-C01-F01", "claim_origin": "episode_metadata",
            "speaker_role": "not_applicable", "assertion_type": "fact",
            "verification_mode": "transcript_attribution", "risk_domain": "general",
            "verdict": "supported", "publication_status": "attributed_or_qualified",
        }]}
        fixed = _deterministic_mode_fixes(review)
        self.assertEqual(review["fact_checks"][0]["verification_mode"], "transcript_only")
        self.assertTrue(any(f["field"] == "verification_mode" for f in fixed))
        self.assertEqual(review["fact_checks"][0]["verdict"], "supported")


if __name__ == "__main__":
    unittest.main()


class StaleContractSnapshotTest(unittest.TestCase):
    def test_snapshot_fixed_by_current_code_is_not_replayed(self):
        import json
        import tempfile
        from pathlib import Path
        from scripts.review_repair import _contract_failure_still_reproduces
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "content_map.json").write_text(json.dumps({"units": [
                {"id": "U0001", "claims": ["节目介绍嗅觉系统"]}]}), encoding="utf-8")
            item = {
                "claim": "节目介绍嗅觉系统", "parent_claim_id": "U0001-C01",
                "subclaim_id": "U0001-C01-F01", "claim_type": "not_applicable",
                "claim_origin": "episode_metadata", "speaker_role": "not_applicable",
                "assertion_type": "fact", "verification_mode": "transcript_attribution",
                "risk_domain": "general", "verdict": "supported",
                "publication_status": "attributed_or_qualified",
                "evidence_segment_ids": ["S0001"], "source_urls": [],
                "checked_at": "2026-10-30", "notes": "",
            }
            stale = {"original_output": {"fact_checks": [item]}}
            self.assertFalse(_contract_failure_still_reproduces(folder, stale))
            broken = {"original_output": {"fact_checks": [
                {**item, "parent_claim_id": "U0009-C01", "subclaim_id": "U0009-C01-F01"}]}}
            self.assertTrue(_contract_failure_still_reproduces(folder, broken))
