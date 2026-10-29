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
