import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agent_pipeline import (
    CONTENT_MAP_GENERATION_SCHEMA,
    _env_positive_int,
    _transcript_basis_is_current,
    _validate_content_map_stage,
    content_pipeline_needed,
    run_content_pipeline,
)
from claim_evidence import _unit_payloads
from content_map import init_content_map, validate_content_map
from episode import load_episode, update_transcript_status
from quality_report import _transcript_status_accepted


class OrchestrationRecoveryTests(unittest.TestCase):
    def test_claim_evidence_tuning_requires_positive_integers(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_CLAIM_TUNING", None)
            self.assertEqual(
                _env_positive_int("TEST_CLAIM_TUNING", 3), 3)
        with mock.patch.dict(
                os.environ, {"TEST_CLAIM_TUNING": "1"}, clear=False):
            self.assertEqual(
                _env_positive_int("TEST_CLAIM_TUNING", 3), 1)
        with mock.patch.dict(
                os.environ, {"TEST_CLAIM_TUNING": "0"}, clear=False):
            with self.assertRaises(ValueError):
                _env_positive_int("TEST_CLAIM_TUNING", 3)

    def test_partial_content_artifacts_trigger_resume(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "原始转录.txt").write_text(
                "source " * 40, encoding="utf-8")
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "web_transcript",
                "meta": {"evidence_mode": "text_anchor"},
                "segments": [{
                    "id": "S0001",
                    "start": None,
                    "end": None,
                    "text": "source",
                }],
            }), encoding="utf-8")
            (folder / "讲书稿.md").write_text(
                "导览。\n\n## 章节\n正文。", encoding="utf-8")
            self.assertTrue(content_pipeline_needed(folder))

    def test_force_refetch_always_rebuilds_content(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(content_pipeline_needed(Path(td), force=True))

    def test_content_map_schema_restricts_completed_statuses(self):
        status = CONTENT_MAP_GENERATION_SCHEMA["properties"]["units"][
            "items"]["properties"]["status"]
        self.assertEqual(
            status["enum"], ["included", "condensed", "excluded"])

    def test_content_map_stage_rejects_unknown_status_and_segment(self):
        transcript = {
            "segments": [{"id": "S0001", "text": "source"}],
        }
        errors = _validate_content_map_stage({
            "units": [{
                "id": "U0001",
                "topic": "topic",
                "claims": ["claim"],
                "importance": "high",
                "status": "expanded",
                "notes": "",
                "evidence": {"segment_ids": ["S9999"]},
            }],
        }, transcript)
        self.assertTrue(any("status" in error for error in errors))
        self.assertTrue(any("未知片段" in error for error in errors))
        self.assertTrue(any("未记账" in error for error in errors))

    def test_content_map_stage_rejects_excluded_claims(self):
        errors = _validate_content_map_stage({
            "units": [{
                "id": "U0001",
                "topic": "advertisement",
                "claims": ["should not exist"],
                "importance": "low",
                "status": "excluded",
                "notes": "sponsor read",
                "evidence": {"segment_ids": ["S0001"]},
            }],
        }, {"segments": [{"id": "S0001", "text": "ad"}]})
        self.assertTrue(any("不得生成 claims" in error for error in errors))

    def test_invalid_structured_map_stops_before_claim_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            raw = {
                "source_kind": "web_transcript",
                "meta": {"evidence_mode": "text_anchor"},
                "segments": [{
                    "id": "S0001", "start": None, "end": None,
                    "text": "source claim",
                }],
            }
            (folder / "transcript.raw.json").write_text(
                json.dumps(raw), encoding="utf-8")
            (folder / "原始转录.txt").write_text(
                "source claim", encoding="utf-8")
            invalid = {
                "units": [{
                    "id": "U0001",
                    "topic": "topic",
                    "speaker": "speaker",
                    "claims": ["claim"],
                    "reasoning": [],
                    "examples": [],
                    "numbers": [],
                    "terms": [],
                    "timestamps": [],
                    "importance": "high",
                    "status": "expanded",
                    "notes": "",
                    "evidence": {"segment_ids": ["S0001"]},
                }],
            }
            with mock.patch(
                    "agent_pipeline.quality_metadata",
                    return_value={"transcript_status": "可接受（已抽查）"}), \
                    mock.patch(
                        "agent_pipeline.run_json_task",
                        return_value={"payload": invalid}) as structured, \
                    mock.patch(
                        "agent_pipeline.refine_claim_evidence") as claim_runner:
                self.assertFalse(run_content_pipeline(folder, "Episode"))
        structured.assert_called_once()
        claim_runner.assert_not_called()

    def test_claim_evidence_skips_excluded_units(self):
        payloads = _unit_payloads({
            "units": [{
                "id": "U0001",
                "status": "excluded",
                "claims": ["legacy excluded claim"],
                "evidence": {"segment_ids": ["S0001"]},
            }],
        }, {"segments": [{"id": "S0001", "text": "ad"}]})
        self.assertEqual(payloads, [])

    def test_corrected_transcript_change_invalidates_summary_basis(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "转录_纠错.txt").write_text(
                "corrected revision", encoding="utf-8")
            self.assertFalse(_transcript_basis_is_current(folder, {
                "transcript_basis": {
                    "file": "转录_纠错.txt",
                    "sha256": "stale",
                },
            }))

    def test_transcript_review_updates_episode_and_source(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "来源.md").write_text(
                "# 来源信息\n- 转录质量：未标注\n", encoding="utf-8")
            load_episode(folder, create=True)
            update_transcript_status(
                folder,
                "可接受（subagent 已抽查）",
                "sample_checked",
            )
            quality = load_episode(folder)["quality"]
            self.assertEqual(
                quality["transcript_status"], "可接受（subagent 已抽查）")
            self.assertEqual(quality["correction_status"], "sample_checked")
            source = (folder / "来源.md").read_text(encoding="utf-8")
            self.assertIn("- 转录质量：可接受（subagent 已抽查）", source)
            self.assertIn("- 纠错状态：sample_checked", source)

    def test_unacceptable_status_does_not_match_acceptable(self):
        self.assertFalse(_transcript_status_accepted("不可接受"))
        self.assertTrue(_transcript_status_accepted("可接受（已抽查）"))

    def test_last_timestamp_segment_uses_point_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            raw_path = folder / "transcript.raw.json"
            output = folder / "content_map.json"
            raw_path.write_text(json.dumps({
                "meta": {
                    "timestamped": True,
                    "evidence_mode": "timestamp",
                },
                "segments": [{
                    "id": "S0001",
                    "start": 12,
                    "end": None,
                    "text": "last segment",
                }],
            }), encoding="utf-8")
            payload = init_content_map(raw_path, output)
            self.assertEqual(payload["units"][0]["timestamps"], [[12, 12]])

    def test_invalid_text_evidence_mode_is_rejected(self):
        errors, _warnings = validate_content_map({
            "schema_version": 3,
            "evidence_mode": "text",
            "units": [{
                "id": "U0001",
                "topic": "topic",
                "claims": ["claim"],
                "importance": "high",
                "status": "included",
                "timestamps": [[0, 1]],
                "evidence": {
                    "segment_ids": ["S0001"],
                    "source_sha256": "",
                },
                "claim_evidence": {},
            }],
        })
        self.assertTrue(any("evidence_mode 无效" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
