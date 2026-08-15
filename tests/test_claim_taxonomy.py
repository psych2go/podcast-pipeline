import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from claim_taxonomy import (
    AI_REVIEW_SCHEMA_VERSION,
    atomic_subclaim_parent,
    derive_legacy_claim_type,
    is_cacheable_fact_check,
)
from quality_report import _ai_fact_check_consistency


def fact_check(**overrides):
    payload = {
        "claim": "嘉宾称内部项目转化率提高百分之二十",
        "parent_claim_id": "U0001-C01",
        "subclaim_id": "U0001-C01-F01",
        "claim_type": "guest_firsthand",
        "claim_origin": "speaker_firsthand",
        "speaker_role": "guest",
        "assertion_type": "fact",
        "verification_mode": "transcript_attribution",
        "risk_domain": "general",
        "verdict": "faithfully_attributed",
        "publication_status": "attributed_or_qualified",
        "evidence_segment_ids": ["S0001"],
        "source_urls": [],
        "checked_at": "2026-08-15T00:00:00+00:00",
        "notes": "内部数据只做忠实归因。",
    }
    payload.update(overrides)
    return payload


class ClaimTaxonomyTests(unittest.TestCase):
    def test_v3_legacy_type_is_derived_not_guessed(self):
        self.assertEqual(AI_REVIEW_SCHEMA_VERSION, 3)
        self.assertEqual(
            derive_legacy_claim_type(fact_check()),
            "guest_firsthand",
        )
        self.assertEqual(derive_legacy_claim_type(fact_check(
            claim_origin="speaker_reported",
            speaker_role="host",
            assertion_type="opinion",
        )), "not_applicable")
        self.assertEqual(derive_legacy_claim_type(fact_check(
            claim_origin="external_source",
            speaker_role="not_applicable",
            assertion_type="fact",
        )), "public_fact")

    def test_atomic_subclaim_id_binds_parent(self):
        self.assertEqual(
            atomic_subclaim_parent("U0012-C03-F02"),
            "U0012-C03",
        )
        self.assertIsNone(atomic_subclaim_parent("U0012-C03"))

    def test_firsthand_claim_needs_no_public_url(self):
        errors, warnings = _ai_fact_check_consistency(
            {"schema_version": 3, "fact_checks": [fact_check()]},
            valid_claim_ids={"U0001-C01"},
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_compound_parent_can_be_split_into_fact_and_host_opinion(self):
        public_fact = fact_check(
            claim="PayPal 在二零零二年被 eBay 收购",
            subclaim_id="U0001-C01-F01",
            claim_type="public_fact",
            claim_origin="external_source",
            speaker_role="not_applicable",
            assertion_type="fact",
            verification_mode="web_required",
            verdict="supported",
            publication_status="used_as_fact",
            source_urls=["https://example.com/official"],
        )
        host_opinion = fact_check(
            claim="主持人认为创始文化流失促成外溢创业",
            subclaim_id="U0001-C01-F02",
            claim_type="not_applicable",
            claim_origin="speaker_reported",
            speaker_role="host",
            assertion_type="opinion",
            verification_mode="transcript_attribution",
            verdict="faithfully_attributed",
        )
        errors, _warnings = _ai_fact_check_consistency(
            {
                "schema_version": 3,
                "fact_checks": [public_fact, host_opinion],
            },
            valid_claim_ids={"U0001-C01"},
        )
        self.assertEqual(errors, [])

    def test_allegation_checks_source_document_not_truth(self):
        allegation = fact_check(
            claim="诉状指控候选人被要求携带零件面试",
            claim_type="not_applicable",
            claim_origin="external_source",
            speaker_role="not_applicable",
            assertion_type="allegation",
            verification_mode="source_document_required",
            risk_domain="legal",
            verdict="accurately_reported",
            source_urls=["https://example.com/complaint.pdf"],
        )
        errors, _warnings = _ai_fact_check_consistency(
            {"schema_version": 3, "fact_checks": [allegation]},
            valid_claim_ids={"U0001-C01"},
        )
        self.assertEqual(errors, [])

    def test_high_risk_recommendation_requires_safety_cross_check(self):
        recommendation = fact_check(
            claim="嘉宾建议调整药物剂量",
            claim_type="guest_opinion",
            claim_origin="speaker_reported",
            speaker_role="guest",
            assertion_type="recommendation",
            verification_mode="transcript_attribution",
            risk_domain="medical",
            verdict="qualified",
        )
        errors, _warnings = _ai_fact_check_consistency(
            {"schema_version": 3, "fact_checks": [recommendation]},
            valid_claim_ids={"U0001-C01"},
        )
        self.assertTrue(any("safety_cross_check" in error for error in errors))
        self.assertTrue(any("安全核查来源" in error for error in errors))

    def test_invalid_parent_duplicate_and_non_contiguous_ids_are_blocked(self):
        first = fact_check(subclaim_id="U0001-C01-F02")
        duplicate = fact_check(subclaim_id="U0001-C01-F02")
        errors, _warnings = _ai_fact_check_consistency(
            {"schema_version": 3, "fact_checks": [first, duplicate]},
            valid_claim_ids={"U9999-C01"},
        )
        self.assertTrue(any("重复" in error for error in errors))
        self.assertTrue(any("不存在于 content_map" in error for error in errors))
        self.assertTrue(any("连续递增" in error for error in errors))

    def test_cache_only_accepts_external_or_editorial_objective_facts(self):
        self.assertFalse(is_cacheable_fact_check(fact_check()))
        self.assertTrue(is_cacheable_fact_check(fact_check(
            claim_origin="external_source",
            speaker_role="not_applicable",
            assertion_type="fact",
        )))
        self.assertFalse(is_cacheable_fact_check(fact_check(
            claim_origin="external_source",
            speaker_role="not_applicable",
            assertion_type="allegation",
        )))


if __name__ == "__main__":
    unittest.main()
