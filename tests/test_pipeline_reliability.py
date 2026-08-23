import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ai_review
from editorial_corrections import validate_editorial_corrections
from content_map import (
    apply_claim_evidence_mapping,
    enrich_content_map_evidence,
    validate_content_map,
)
from episode import _source_heading
import claim_evidence
from rebuild_plan import build_rebuild_plan
from tts import build_tts_plan


def review_payload():
    return {
        "schema_version": 3,
        "passed": True,
        "summary": "review summary",
        "transcript_quality": {"passed": True, "score": 95},
        "coverage": {"passed": True, "score": 95},
        "factuality": {"passed": True, "score": 95},
        "numbers": {"passed": True},
        "attribution": {"passed": True},
        "entity_accuracy": {"passed": True, "checked_entities": [], "issues": []},
        "tts": {"passed": True},
        "publish": {"passed": True},
        "issues": [],
        "fact_checks": [{
            "claim": "嘉宾提出用产品责任促进 AI 安全",
            "parent_claim_id": "U0001-C01",
            "subclaim_id": "U0001-C01-F01",
            "claim_type": "guest_opinion",
            "claim_origin": "speaker_firsthand",
            "speaker_role": "guest",
            "assertion_type": "recommendation",
            "verification_mode": "transcript_attribution",
            "risk_domain": "legal",
            "verdict": "qualified",
            "publication_status": "attributed_or_qualified",
            "evidence_segment_ids": ["S0001"],
            "source_urls": [],
            "checked_at": "2026-08-23T00:00:00Z",
            "notes": "节目中的治理建议。",
        }],
    }


class EvidenceEnrichmentSafetyTests(unittest.TestCase):
    def test_open_ended_unit_window_is_rejected_without_erasing_evidence(self):
        transcript = {
            "meta": {"timestamped": True, "evidence_mode": "timestamp"},
            "segments": [{
                "id": "S0001", "start": 12, "end": None,
                "text": "final segment",
            }],
        }
        content_map = {
            "schema_version": 3,
            "evidence_mode": "timestamp",
            "units": [{
                "id": "U0001",
                "timestamps": [[12, None]],
                "evidence": {
                    "mode": "timestamp",
                    "segment_ids": ["S0001"],
                    "source_sha256": "existing",
                },
            }],
        }
        with self.assertRaisesRegex(ValueError, "timestamp 范围无效"):
            enrich_content_map_evidence(content_map, transcript)
        self.assertEqual(
            content_map["units"][0]["evidence"]["segment_ids"],
            ["S0001"],
        )

    def test_unknown_text_anchor_cannot_enrich_to_empty_evidence(self):
        transcript = {
            "meta": {"timestamped": False, "evidence_mode": "text_anchor"},
            "segments": [{"id": "S0001", "text": "source"}],
        }
        content_map = {
            "schema_version": 3,
            "evidence_mode": "text_anchor",
            "units": [{
                "id": "U0001",
                "evidence": {"segment_ids": ["S9999"]},
            }],
        }
        with self.assertRaisesRegex(ValueError, "enrichment 结果为空"):
            enrich_content_map_evidence(content_map, transcript)

    def test_claim_payload_without_segments_fails_before_runner(self):
        with self.assertRaisesRegex(RuntimeError, "不重试"):
            claim_evidence._validate_payload_sources([{
                "unit_id": "U0001",
                "claims": [{"claim_id": "U0001-C01", "text": "claim"}],
                "segments": [],
            }])


class ClaimEvidenceProgressTests(unittest.TestCase):
    def test_failed_unit_writes_partial_progress_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            transcript = {
                "evidence": {"revision_sha256": "revision"},
                "meta": {"timestamped": True, "evidence_mode": "timestamp"},
                "segments": [{
                    "id": "S0001", "start": 0, "end": 5, "text": "source",
                }],
            }
            content_map = {
                "schema_version": 3,
                "evidence_mode": "timestamp",
                "units": [{
                    "id": "U0001", "topic": "topic",
                    "claims": ["claim one", "claim two"],
                    "importance": "high", "status": "included",
                    "timestamps": [[0, 5]],
                }],
            }
            (folder / "transcript.raw.json").write_text(
                json.dumps(transcript), encoding="utf-8")
            (folder / "content_map.json").write_text(
                json.dumps(content_map), encoding="utf-8")
            with mock.patch.object(
                    claim_evidence, "_run_batch",
                    side_effect=RuntimeError("runner down")):
                with self.assertRaises(RuntimeError):
                    claim_evidence.refine_claim_evidence(
                        folder, concurrency=1, max_batch_chars=100000)
            progress = json.loads(
                (folder / claim_evidence.PROGRESS_FILENAME).read_text(
                    encoding="utf-8"))
        self.assertEqual(progress["status"], "partial")
        self.assertEqual(progress["evidence_revision"], "revision")
        self.assertEqual(progress["failed_unit_ids"], ["U0001"])
        self.assertEqual(progress["pending_unit_ids"], ["U0001"])
        self.assertTrue(claim_evidence.validate_progress(progress, transcript))

    def test_completed_progress_matches_revision(self):
        transcript = {"evidence": {"revision_sha256": "revision"}}
        payload = {
            "schema_version": 1,
            "status": "completed",
            "evidence_revision": "revision",
            "target_unit_ids": ["U0001"],
            "completed_unit_ids": ["U0001"],
            "pending_unit_ids": [],
            "failed_unit_ids": [],
        }
        self.assertEqual(
            claim_evidence.validate_progress(payload, transcript), [])
        payload["evidence_revision"] = "stale"
        self.assertTrue(any(
            "revision" in error
            for error in claim_evidence.validate_progress(payload, transcript)
        ))


class SourceLabelTests(unittest.TestCase):
    def test_source_heading_matches_source_kind(self):
        self.assertEqual(_source_heading("web_transcript"), "转录页面")
        self.assertEqual(_source_heading("third_party_transcript"), "转录页面")
        self.assertEqual(_source_heading("local_asr"), "原始音频")
        self.assertEqual(_source_heading("local_transcript"), "本地转录来源")


class ClaimEvidenceRoleTests(unittest.TestCase):
    def test_primary_and_context_evidence_are_bound_separately(self):
        transcript = {
            "meta": {"timestamped": True, "evidence_mode": "timestamp"},
            "segments": [
                {"id": "S0001", "start": 0, "end": 5, "text": "claim"},
                {"id": "S0002", "start": 5, "end": 10, "text": "context"},
            ],
        }
        content_map = {
            "schema_version": 3,
            "evidence_mode": "timestamp",
            "units": [{
                "id": "U0001",
                "topic": "topic",
                "claims": ["claim"],
                "importance": "high",
                "status": "included",
                "timestamps": [[0, 10]],
            }],
        }
        content_map, transcript = enrich_content_map_evidence(
            content_map, transcript)
        content_map, transcript = apply_claim_evidence_mapping(
            content_map,
            transcript,
            [{
                "claim_id": "U0001-C01",
                "segment_ids": ["S0001", "S0002"],
                "primary_segment_ids": ["S0001"],
                "context_segment_ids": ["S0002"],
                "confidence": "high",
                "rationale": "S0001 directly supports the claim; S0002 adds context.",
            }],
        )
        roles = content_map["units"][0]["claim_evidence_roles"]["C01"]
        self.assertEqual(roles["primary_segment_ids"], ["S0001"])
        self.assertEqual(roles["context_segment_ids"], ["S0002"])
        self.assertEqual(validate_content_map(content_map, transcript)[0], [])


class RebuildPlanTests(unittest.TestCase):
    def test_transcript_basis_change_is_planned_in_shadow_mode(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            for name in (
                    "transcript.raw.json", "content_map.json",
                    "中文完整笔记.md", "讲书稿.md"):
                (folder / name).write_text("{}", encoding="utf-8")
            (folder / "原始转录.txt").write_text("source", encoding="utf-8")
            (folder / "转录_纠错.txt").write_text("corrected", encoding="utf-8")
            (folder / "summary_map.json").write_text(json.dumps({
                "transcript_basis": {
                    "file": "转录_纠错.txt",
                    "sha256": "stale",
                },
                "chapters": [],
            }), encoding="utf-8")
            plan = build_rebuild_plan(folder)
        self.assertTrue(plan["needs_content"])
        self.assertIn("stale:transcript_basis", plan["reasons"])
        self.assertEqual(plan["mode"], "shadow")
        self.assertIn("ai_review", plan["stages"])


class EditorialCorrectionTests(unittest.TestCase):
    def test_correction_requires_real_claim_and_source(self):
        payload = {
            "schema_version": 1,
            "corrections": [{
                "correction_id": "EC0001",
                "claim_id": "U0001-C01",
                "episode_statement": "节目称股份进入信托。",
                "public_treatment": "公开资料只支持治理股票。",
                "verdict": "corrected",
                "source_urls": ["https://example.com/official"],
                "checked_at": "2026-08-23T00:00:00Z",
                "risk_domain": "financial",
            }],
        }
        self.assertEqual(
            validate_editorial_corrections(
                payload, valid_claim_ids={"U0001-C01"}), [])
        payload["corrections"][0]["source_urls"] = []
        errors = validate_editorial_corrections(
            payload, valid_claim_ids={"U0001-C01"})
        self.assertTrue(any("公开来源" in error for error in errors))


class ReviewMechanicalRetryTests(unittest.TestCase):
    def _workspace(self, folder):
        (folder / "content_map.json").write_text(json.dumps({
            "units": [{"id": "U0001", "claims": ["claim"]}],
        }), encoding="utf-8")

    def test_mechanical_retry_fixes_contract_without_changing_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._workspace(folder)
            first = review_payload()
            corrected = copy.deepcopy(first)
            check = corrected["fact_checks"][0]
            check["verification_mode"] = "safety_cross_check"
            check["source_urls"] = ["https://example.com/legal-safety"]
            with mock.patch.object(ai_review, "run_json_task", return_value={
                "payload": corrected,
                "duration_ms": 1,
                "retry_count": 0,
            }) as runner:
                result, audit, wrapper = ai_review._validate_or_retry_review(
                    folder, first, model="", effort="max")
        self.assertEqual(audit["retry_count"], 1)
        self.assertEqual(audit["final_errors"], [])
        self.assertEqual(
            result["fact_checks"][0]["verification_mode"],
            "safety_cross_check",
        )
        self.assertIsNotNone(wrapper)
        runner.assert_called_once()

    def test_mechanical_retry_cannot_flip_review_verdict(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._workspace(folder)
            first = review_payload()
            corrected = copy.deepcopy(first)
            corrected["passed"] = False
            corrected["fact_checks"][0]["verification_mode"] = "safety_cross_check"
            corrected["fact_checks"][0]["source_urls"] = [
                "https://example.com/legal-safety"]
            with mock.patch.object(ai_review, "run_json_task", return_value={
                "payload": corrected,
            }):
                with self.assertRaisesRegex(RuntimeError, "语义结论"):
                    ai_review._validate_or_retry_review(
                        folder, first, model="", effort="max")


class TtsDisplaySpokenTests(unittest.TestCase):
    def test_plan_binds_display_and_spoken_text_separately(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text(
                "这是足够长的导览文本。\n\n## a16z\n\na16z 投资了 AI。",
                encoding="utf-8",
            )
            (folder / "tts_lexicon.json").write_text(json.dumps({
                "a16z": "A 十六 Z",
                "AI": "A I",
            }), encoding="utf-8")
            plan = build_tts_plan(folder, "讲书稿.md")
        self.assertTrue(plan)
        self.assertTrue(all(item.get("display_sha256") for item in plan))
        self.assertTrue(all(item.get("spoken_sha256") for item in plan))
        self.assertTrue(any(
            item["display_sha256"] != item["spoken_sha256"]
            for item in plan
        ))


if __name__ == "__main__":
    unittest.main()
