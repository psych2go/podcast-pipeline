import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from content_map import (  # noqa: E402
    coverage_report,
    enrich_content_map_evidence,
    segment_evidence_sha256,
    source_segment_accountability,
    validate_content_map,
    validate_summary_map,
)
from transcript_completeness import (  # noqa: E402
    CompletenessPolicy,
    analyze_audio_completeness,
    calculate_completeness,
    faster_whisper_vad,
    validate_completeness_result,
)
from transcript_correction import (  # noqa: E402
    CorrectionValidationError,
    build_manifest,
    correction_batches,
    correction_contract_required,
    render_corrected_transcript,
    validate_correction_manifest,
)
from quality_report import build_quality_report  # noqa: E402
import agent_pipeline  # noqa: E402
import fetcher  # noqa: E402
import process as pipeline_process  # noqa: E402


def _raw(count=3, *, contract=True, completeness_mode="report_only",
         accountability_contract=False):
    segments = []
    for index in range(count):
        text = f"Segment {index + 1} contains substantive source words."
        segments.append({
            "id": f"S{index + 1:04d}",
            "start": float(index * 2),
            "end": float(index * 2 + 2),
            "text": text,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "speaker": "SPEAKER_00",
        })
    text = "\n".join(f"[SPEAKER_00] {item['text']}" for item in segments)
    return {
        "source_kind": "local_asr",
        "segments": segments,
        "meta": {
            "timestamped": True,
            "evidence_mode": "timestamp",
            "completeness_mode": completeness_mode,
            **({"correction_contract_version": 1} if contract else {}),
            **(
                {"source_accountability_contract_version": 1}
                if accountability_contract else {}
            ),
        },
        "evidence": {
            "revision_id": "revision-1",
            "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
        },
    }


def _unit(raw, segment_ids, *, uid="U0001", status="included",
          exclusion_type=None, notes=""):
    by_id = {item["id"]: item for item in raw["segments"]}
    starts = [by_id[item]["start"] for item in segment_ids]
    ends = [by_id[item]["end"] for item in segment_ids]
    unit = {
        "id": uid,
        "topic": "Accounted source",
        "claims": [],
        "importance": "low",
        "status": status,
        "notes": notes,
        "timestamps": [[min(starts), max(ends)]],
        "evidence": {
            "mode": "timestamp",
            "segment_ids": segment_ids,
            "source_sha256": segment_evidence_sha256(
                raw["segments"], segment_ids),
        },
    }
    if exclusion_type is not None:
        unit["exclusion_type"] = exclusion_type
    return unit


class CompletenessMetricTests(unittest.TestCase):
    def test_complete_timeline_passes(self):
        segments = [{"start": 0.2, "end": 9.8, "text": "complete"}]
        report = calculate_completeness(
            10.0, [(0.0, 10.0)], segments,
            policy=CompletenessPolicy(boundary_tolerance_seconds=0.5),
        )
        self.assertTrue(report["passed"])
        self.assertEqual(report["speech_coverage"], 1.0)

    def test_missing_middle_speech_fails(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "start"},
            {"start": 8.0, "end": 10.0, "text": "end"},
        ]
        report = calculate_completeness(
            10.0, [(0.0, 10.0)], segments,
            policy=CompletenessPolicy(boundary_tolerance_seconds=0.0),
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["max_uncovered_speech_seconds"], 6.0)

    def test_missing_tail_and_invalid_timeline_fail(self):
        report = calculate_completeness(
            12.0, [(0.0, 12.0)], [
                {"start": 4.0, "end": 5.0, "text": "later"},
                {"start": 1.0, "end": 2.0, "text": "out of order"},
            ],
        )
        self.assertFalse(report["timeline_valid"])
        self.assertGreater(report["last_speech_gap_seconds"], 2.0)

    def test_empty_vad_never_certifies_completeness(self):
        mismatch = calculate_completeness(
            10.0, [], [{"start": 0.0, "end": 2.0, "text": "hallucinated"}])
        silent = calculate_completeness(10.0, [], [])
        self.assertFalse(mismatch["passed"])
        self.assertEqual(mismatch["status"], "detector_mismatch")
        self.assertEqual(mismatch["speech_coverage"], 0.0)
        self.assertFalse(silent["passed"])
        self.assertEqual(silent["status"], "no_speech")

    def test_analyze_empty_detector_preserves_rollout_mode(self):
        result = analyze_audio_completeness(
            "fake.wav",
            [{"start": 0.0, "end": 1.0, "text": "speech"}],
            detector=lambda _path: [],
            duration=10.0,
            mode="report_only",
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "detector_mismatch")
        self.assertEqual(result["enforcement_mode"], "report_only")

    def test_vad_chunks_audio_and_merges_overlap(self):
        calls = []

        def decode(_path, start, duration, sampling_rate):
            calls.append((start, duration, sampling_rate))
            return object()

        def vad(_audio, _options, sampling_rate):
            return [{"start": 0, "end": sampling_rate * 2}]

        intervals = faster_whisper_vad(
            "fake.wav", duration=20.0, chunk_seconds=10.0,
            overlap_seconds=2.0, decode_chunk=decode, vad_runner=vad)
        self.assertEqual(calls, [
            (0.0, 10.0, 16000),
            (8.0, 10.0, 16000),
            (16.0, 4.0, 16000),
        ])
        self.assertEqual(intervals, [(0.0, 2.0), (8.0, 10.0), (16.0, 18.0)])

    def test_agent_pipeline_blocks_only_enforce_mode(self):
        report_only = _raw(completeness_mode="report_only")
        report_only["meta"].update({
            "completeness_contract_version": 1,
            "completeness": {"passed": False},
        })
        enforce = json.loads(json.dumps(report_only))
        enforce["meta"]["completeness_mode"] = "enforce"
        self.assertFalse(agent_pipeline._completeness_blocks_content(
            "local_asr", report_only))
        self.assertTrue(agent_pipeline._completeness_blocks_content(
            "local_asr", enforce))

    def test_enforce_mode_validates_result_instead_of_trusting_passed(self):
        raw = _raw(completeness_mode="enforce")
        raw["meta"]["completeness_contract_version"] = 1
        raw["meta"]["completeness"] = {
            "passed": True,
            "status": "passed",
            "enforcement_mode": "enforce",
            "policy": {"enforcement_mode": "enforce"},
        }
        self.assertTrue(agent_pipeline._completeness_blocks_content(
            "local_asr", raw))

        measured = calculate_completeness(
            6.0, [(0.0, 6.0)], raw["segments"], mode="enforce")
        raw["meta"]["completeness"] = measured
        self.assertFalse(agent_pipeline._completeness_blocks_content(
            "local_asr", raw))

        forged = json.loads(json.dumps(measured))
        forged["audio_duration_seconds"] = 1.0
        forged["speech_seconds"] = 1.0
        forged["covered_speech_seconds"] = 1.0
        forged["first_speech_start"] = 0.0
        forged["last_speech_end"] = 1.0
        forged["first_speech_gap_seconds"] = 0.0
        forged["last_speech_gap_seconds"] = 0.0
        raw["meta"]["completeness"] = forged
        self.assertTrue(agent_pipeline._completeness_blocks_content(
            "local_asr", raw))

        forged = json.loads(json.dumps(measured))
        forged["audio_duration_seconds"] = 5.6
        forged["speech_seconds"] = 5.6
        forged["covered_speech_seconds"] = 5.6
        forged["first_speech_start"] = 0.0
        forged["last_speech_end"] = 5.6
        forged["first_speech_gap_seconds"] = 0.0
        forged["last_speech_gap_seconds"] = 0.0
        raw["meta"]["completeness"] = forged
        self.assertTrue(agent_pipeline._completeness_blocks_content(
            "local_asr", raw))

        forged = json.loads(json.dumps(measured))
        forged["speech_seconds"] = 60.0
        forged["covered_speech_seconds"] = 60.0
        raw["meta"]["completeness"] = forged
        self.assertTrue(agent_pipeline._completeness_blocks_content(
            "local_asr", raw))

        forged = json.loads(json.dumps(measured))
        forged["policy"]["min_speech_coverage"] = 0.0
        raw["meta"]["completeness"] = forged
        self.assertTrue(agent_pipeline._completeness_blocks_content(
            "local_asr", raw))

    def test_validator_accepts_short_rounded_measurement(self):
        raw = _raw(1, completeness_mode="enforce")
        raw["segments"][0]["start"] = 0.0
        raw["segments"][0]["end"] = 0.101
        result = calculate_completeness(
            1.0, [(0.0, 0.101)], raw["segments"], mode="enforce")
        self.assertEqual(validate_completeness_result(raw, result), [])


class SourceAccountabilityTests(unittest.TestCase):
    def test_first_middle_and_last_omissions_are_reported(self):
        raw = _raw()
        for missing in range(3):
            ids = [item["id"] for index, item in enumerate(raw["segments"])
                   if index != missing]
            payload = {
                "source_accountability_version": 1,
                "evidence_mode": "timestamp",
                "units": [_unit(raw, ids)],
            }
            errors, _warnings = validate_content_map(payload, raw)
            self.assertTrue(any(raw["segments"][missing]["id"] in error
                                for error in errors))

    def test_valid_ad_exclusion_counts_as_covered(self):
        raw = _raw(1)
        payload = {
            "source_accountability_version": 1,
            "evidence_mode": "timestamp",
            "units": [_unit(
                raw, ["S0001"], status="excluded",
                exclusion_type="advertisement",
                notes="Sponsor promotion without editorial claims.",
            )],
        }
        errors, _warnings = validate_content_map(payload, raw)
        self.assertEqual(errors, [])
        self.assertTrue(source_segment_accountability(payload, raw)["passed"])

    def test_invalid_exclusion_type_fails(self):
        raw = _raw(1)
        payload = {
            "source_accountability_version": 1,
            "evidence_mode": "timestamp",
            "units": [_unit(
                raw, ["S0001"], status="excluded",
                exclusion_type="whatever", notes="Removed.",
            )],
        }
        errors, _warnings = validate_content_map(payload, raw)
        self.assertTrue(any("exclusion_type" in error for error in errors))

    def test_summary_cannot_reference_excluded_unit(self):
        raw = _raw(1)
        content_map = {
            "units": [_unit(
                raw, ["S0001"], status="excluded",
                exclusion_type="advertisement", notes="Sponsor message.",
            )],
        }
        errors = validate_summary_map({
            "chapters": [{
                "title": "Ad", "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
            }],
        }, content_map=content_map)
        self.assertTrue(any("excluded" in error for error in errors))

    def test_boundary_touch_does_not_account_adjacent_segment(self):
        raw = _raw(2)
        content_map = {
            "schema_version": 3,
            "source_accountability_version": 1,
            "evidence_mode": "timestamp",
            "units": [_unit(raw, ["S0001"])],
        }
        content_map["units"][0]["timestamps"] = [[0.0, 2.0]]
        enriched, _raw_result = enrich_content_map_evidence(content_map, raw)
        self.assertEqual(
            enriched["units"][0]["evidence"]["segment_ids"], ["S0001"])

    def test_open_ended_last_timestamp_is_normalized_to_point_anchor(self):
        raw = _raw(1)
        content_map = {
            "schema_version": 3,
            "source_accountability_version": 1,
            "evidence_mode": "timestamp",
            "units": [_unit(raw, ["S0001"])],
        }
        raw["segments"][0]["end"] = None
        content_map["units"][0]["timestamps"] = [[0.0, None]]
        enriched, _raw_result = enrich_content_map_evidence(content_map, raw)
        self.assertEqual(enriched["units"][0]["timestamps"], [[0.0, 0.0]])
        self.assertEqual(
            enriched["units"][0]["evidence"]["segment_ids"], ["S0001"])

    def test_arbitrary_open_ended_window_is_rejected_without_erasing_evidence(self):
        raw = _raw(2)
        content_map = {
            "schema_version": 3,
            "source_accountability_version": 1,
            "evidence_mode": "timestamp",
            "units": [_unit(raw, ["S0001"])],
        }
        content_map["units"][0]["timestamps"] = [[0.0, None]]
        previous_ids = list(
            content_map["units"][0]["evidence"]["segment_ids"])
        with self.assertRaisesRegex(ValueError, "timestamp 范围无效"):
            enrich_content_map_evidence(content_map, raw)
        self.assertEqual(
            content_map["units"][0]["evidence"]["segment_ids"],
            previous_ids,
        )

    def test_new_revision_requires_accountability_contract(self):
        raw = _raw(1, accountability_contract=True)
        payload = {
            "schema_version": 3,
            "evidence_mode": "timestamp",
            "units": [_unit(raw, ["S0001"])],
        }
        errors, _warnings = validate_content_map(payload, raw)
        self.assertTrue(any(
            "source_accountability_version=1" in error for error in errors))
        legacy = _raw(1, accountability_contract=False)
        legacy_errors, _warnings = validate_content_map(payload, legacy)
        self.assertFalse(any(
            "source_accountability_version=1" in error
            for error in legacy_errors))
        malformed = _raw(1, accountability_contract=False)
        malformed["meta"]["source_accountability_contract_version"] = "1"
        malformed_errors, _warnings = validate_content_map(payload, malformed)
        self.assertTrue(any("非负整数" in error for error in malformed_errors))

    def test_invalid_status_is_not_accounted(self):
        raw = _raw(1)
        payload = {
            "source_accountability_version": 1,
            "units": [_unit(raw, ["S0001"], status="unresolved")],
        }
        accountability = source_segment_accountability(payload, raw)
        self.assertFalse(accountability["passed"])
        self.assertEqual(accountability["missing_ids"], ["S0001"])
        self.assertEqual(accountability["invalid_unit_ids"], ["U0001"])


class CondensedCoverageTests(unittest.TestCase):
    def _claim_unit(self, raw, segment_id, uid, status, importance):
        unit = _unit(
            raw, [segment_id], uid=uid, status=status,
            notes="Condensed context.")
        unit["importance"] = importance
        unit["claims"] = ["A substantive claim"]
        return unit

    def test_condensed_claim_is_notes_required_but_briefing_optional(self):
        raw = _raw(2)
        content_map = {"units": [
            self._claim_unit(raw, "S0001", "U0001", "included", "high"),
            self._claim_unit(raw, "S0002", "U0002", "condensed", "medium"),
        ]}
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "Main", "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
            }],
            "notes_claim_ids": ["U0001-C01", "U0002-C01"],
        }
        result = coverage_report(content_map, summary)
        self.assertTrue(result["passed"])
        self.assertEqual(result["claim_total"], 1)
        self.assertEqual(result["notes_claim_total"], 2)

    def test_low_condensed_claim_is_still_required_in_notes(self):
        raw = _raw(1)
        content_map = {"units": [
            self._claim_unit(raw, "S0001", "U0001", "condensed", "low"),
        ]}
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "Main", "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
            }],
            "notes_claim_ids": [],
        }
        result = coverage_report(content_map, summary)
        self.assertFalse(result["passed"])
        self.assertEqual(result["notes_missing_claim_ids"], ["U0001-C01"])


class CorrectionManifestTests(unittest.TestCase):
    def _manifest(self, raw=None):
        raw = raw or _raw()
        items = [{
            "segment_id": segment["id"],
            "corrected_text": segment["text"],
            "status": "unchanged",
            "change_types": [],
            "verification": "not_required",
            "unresolved": [],
        } for segment in raw["segments"]]
        return raw, build_manifest(raw, items)

    def test_valid_manifest_renders_deterministically(self):
        raw, manifest = self._manifest()
        self.assertEqual(validate_correction_manifest(raw, manifest), [])
        rendered = render_corrected_transcript(raw, manifest)
        self.assertEqual(rendered.count("[SPEAKER_00]"), 3)
        self.assertIn("Segment 1", rendered)
        self.assertEqual(
            validate_correction_manifest(
                raw, manifest, rendered_text=rendered), [])
        self.assertTrue(validate_correction_manifest(
            raw, manifest, rendered_text=rendered + " tampered"))

    def test_missing_duplicate_and_out_of_order_ids_fail(self):
        raw, manifest = self._manifest()
        variants = []
        missing = json.loads(json.dumps(manifest))
        missing["segments"].pop()
        variants.append(missing)
        duplicate = json.loads(json.dumps(manifest))
        duplicate["segments"][1]["segment_id"] = "S0001"
        variants.append(duplicate)
        reordered = json.loads(json.dumps(manifest))
        reordered["segments"][0], reordered["segments"][1] = (
            reordered["segments"][1], reordered["segments"][0])
        variants.append(reordered)
        for candidate in variants:
            self.assertTrue(validate_correction_manifest(raw, candidate))

    def test_hash_deletion_and_unresolved_high_risk_fail(self):
        raw, manifest = self._manifest()
        manifest["segments"][0]["source_sha256"] = "bad"
        manifest["segments"][1]["corrected_text"] = ""
        raw["segments"][2]["text"] = "Revenue was $15 million at Example Corp."
        raw["segments"][2]["content_sha256"] = hashlib.sha256(
            raw["segments"][2]["text"].encode()).hexdigest()
        manifest["segments"][2].update({
            "source_sha256": raw["segments"][2]["content_sha256"],
            "corrected_text": raw["segments"][2]["text"],
            "status": "unresolved",
            "verification": "unresolved",
            "unresolved": ["$15 million"],
        })
        errors = validate_correction_manifest(raw, manifest)
        self.assertTrue(any("source_sha256" in error for error in errors))
        self.assertTrue(any("不能为空" in error for error in errors))
        self.assertTrue(any("高风险" in error for error in errors))

    def test_status_text_summary_and_speaker_injection_are_bound(self):
        raw, manifest = self._manifest()
        changed_unchanged = json.loads(json.dumps(manifest))
        changed_unchanged["segments"][0]["corrected_text"] = "Different words"
        multiline = json.loads(json.dumps(manifest))
        multiline["segments"][0]["corrected_text"] = (
            raw["segments"][0]["text"] + "\n[SPEAKER_99] injected")
        tampered_summary = json.loads(json.dumps(manifest))
        tampered_summary["summary"]["unresolved"] = 99
        malformed = json.loads(json.dumps(manifest))
        malformed["segments"][0] = "not-an-object"
        cases = (
            (changed_unchanged, "unchanged 状态不得改变文本"),
            (multiline, "必须是单行文本"),
            (tampered_summary, "summary 与重新计算结果不一致"),
            (malformed, "必须全部是对象"),
        )
        for candidate, expected in cases:
            errors = validate_correction_manifest(raw, candidate)
            self.assertTrue(
                any(expected in error for error in errors),
                (expected, errors),
            )

    def test_flagged_segment_cannot_be_certified_not_required(self):
        for flag, value in (
                ("needs_redecode", True),
                ("needs_review", True),
                ("speaker_alignment", "unresolved")):
            raw = _raw(1)
            raw["segments"][0][flag] = value
            item = {
                "segment_id": "S0001",
                "corrected_text": raw["segments"][0]["text"],
                "status": "unchanged",
                "change_types": [],
                "verification": "not_required",
                "unresolved": [],
            }
            with self.assertRaisesRegex(
                    CorrectionValidationError, "已标记待复核"):
                build_manifest(raw, [item])

    def test_equal_length_unrelated_rewrite_requires_audio_verification(self):
        raw = _raw(1)
        item = {
            "segment_id": "S0001",
            "corrected_text": "Weather tomorrow appears pleasant near the coast.",
            "status": "corrected",
            "change_types": ["word"],
            "verification": "context_only",
            "unresolved": [],
        }
        with self.assertRaisesRegex(
                CorrectionValidationError, "改写幅度过大"):
            build_manifest(raw, [item])

    def test_corrected_high_risk_text_requires_independent_verification(self):
        raw = _raw(1)
        raw["segments"][0]["text"] = "Revenue was $15 million at Example Corp."
        raw["segments"][0]["content_sha256"] = hashlib.sha256(
            raw["segments"][0]["text"].encode()).hexdigest()
        item = {
            "segment_id": "S0001",
            "corrected_text": "Revenue was $50 million at Example Corp.",
            "status": "corrected",
            "change_types": ["number"],
            "verification": "context_only",
            "unresolved": [],
        }
        with self.assertRaises(CorrectionValidationError):
            build_manifest(raw, [item])

    def test_unrelated_unresolved_word_is_not_promoted_to_high_risk(self):
        raw = _raw(1)
        raw["segments"][0]["text"] = "In 2024 the speaker used an unclear adjective."
        raw["segments"][0]["content_sha256"] = hashlib.sha256(
            raw["segments"][0]["text"].encode()).hexdigest()
        item = {
            "segment_id": "S0001",
            "corrected_text": raw["segments"][0]["text"],
            "status": "unresolved",
            "change_types": [],
            "verification": "unresolved",
            "unresolved": ["unclear adjective"],
        }
        manifest = build_manifest(raw, [item])
        self.assertEqual(validate_correction_manifest(raw, manifest), [])

    def test_id_only_runner_payload_is_rejected_before_writes(self):
        raw = _raw(1)
        with tempfile.TemporaryDirectory() as td, patch(
                "agent_pipeline.run_json_task",
                return_value={"payload": {"segments": [
                    {"segment_id": "S0001"}
                ]}},
        ):
            with self.assertRaisesRegex(RuntimeError, "输出无效"):
                agent_pipeline._run_structured_correction(Path(td), raw)
            self.assertFalse((Path(td) / "correction_manifest.json").exists())
            self.assertFalse((Path(td) / "转录_纠错.txt").exists())

    def test_long_input_batches_preserve_consecutive_order(self):
        segments = [
            {"id": f"S{index:04d}", "text": "x" * 20}
            for index in range(1, 8)
        ]
        batches = correction_batches(segments, max_chars=45)
        self.assertEqual([len(batch) for batch in batches], [2, 2, 2, 1])
        self.assertEqual(
            [item["id"] for batch in batches for item in batch],
            [item["id"] for item in segments],
        )

    def test_contract_is_explicit_and_legacy_remains_compatible(self):
        self.assertTrue(correction_contract_required(_raw(contract=True)))
        self.assertFalse(correction_contract_required(_raw(contract=False)))
        malformed = _raw(contract=False)
        malformed["meta"]["correction_contract_version"] = "1"
        with self.assertRaises(ValueError):
            correction_contract_required(malformed)

    def test_new_enforced_contract_missing_completeness_and_manifest_is_blocked(self):
        raw = _raw(contract=True, completeness_mode="enforce")
        raw["meta"]["quality"] = "balanced"
        raw["meta"]["completeness_contract_version"] = 1
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(
                json.dumps(raw), encoding="utf-8")
            (folder / "原始转录.txt").write_text(
                "\n".join(f"[SPEAKER_00] {item['text']}"
                          for item in raw["segments"]), encoding="utf-8")
            report = build_quality_report(folder)
        codes = {item["code"] for item in report["error_details"]}
        self.assertIn("asr_completeness_missing", codes)
        self.assertIn("correction_manifest_missing", codes)

    def test_report_only_contract_warns_but_does_not_add_completeness_error(self):
        raw = _raw(contract=False, completeness_mode="report_only")
        raw["meta"].update({
            "quality": "balanced",
            "completeness_contract_version": 1,
            "completeness": {
                "passed": False,
                "status": "detector_mismatch",
                "timeline_valid": True,
                "enforcement_mode": "report_only",
                "policy": {"enforcement_mode": "report_only"},
            },
        })
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(
                json.dumps(raw), encoding="utf-8")
            (folder / "原始转录.txt").write_text(
                "\n".join(f"[SPEAKER_00] {item['text']}"
                          for item in raw["segments"]), encoding="utf-8")
            report = build_quality_report(folder)
        codes = {item["code"] for item in report["error_details"]}
        self.assertNotIn("asr_speech_coverage_failed", codes)
        self.assertTrue(any("report_only" in warning for warning in report["warnings"]))


class FaithfulEvidenceTests(unittest.TestCase):
    def test_cleaner_preserves_ambiguous_real_speech(self):
        text = "Uh-huh.\n[foreign language]\nrepeat repeat repeat\nSponsored by Acme."
        self.assertEqual(fetcher.clean_whisper_hallucinations(text), text)

    def test_nonfaithful_policy_does_not_mutate_immutable_evidence(self):
        source_text = (
            "This episode is sponsored by Acme and includes a substantive "
            "discussion that must remain in immutable source evidence. " * 3
        ).strip()
        with tempfile.TemporaryDirectory() as td, patch(
                "process.fetch_transcript_from_url",
                return_value={
                    "text": source_text,
                    "segments": [{
                        "start": None, "end": None, "text": source_text,
                        "synthetic_boundary": True,
                    }],
                    "meta": {"timestamped": False},
                }):
            folder = Path(td) / "Episode"
            folder.mkdir()
            ok = pipeline_process.fetch_transcript(
                "https://example.com/transcript", folder, "Episode", None,
                content_policy="no-ads", display_title="Episode",
            )
            self.assertTrue(ok)
            self.assertEqual(
                (folder / "原始转录.txt").read_text(encoding="utf-8"),
                source_text,
            )
            raw = json.loads(
                (folder / "transcript.raw.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["segments"][0]["text"], source_text)
            self.assertEqual(raw["meta"]["content_policy"], "faithful")
            self.assertEqual(raw["meta"]["requested_content_policy"], "no-ads")

    def test_transcribe_preserves_decoder_text(self):
        word = SimpleNamespace(
            word=" uh-huh", start=0.0, end=0.5, probability=0.9)
        segment = SimpleNamespace(
            start=0.0, end=0.5, text=" uh-huh", words=[word],
            avg_logprob=-0.1, compression_ratio=1.0,
            no_speech_prob=0.0, temperature=0.0,
        )
        model = SimpleNamespace(transcribe=lambda *_args, **_kwargs: (
            iter([segment]), SimpleNamespace(language="en", language_probability=1.0)))
        with patch("fetcher._load_whisper_model", return_value=model), \
                patch("asr_runtime.resolve_runtime", return_value=SimpleNamespace(
                    device="cpu", compute_type="int8")):
            result = fetcher.transcribe_mp3_timestamped(
                "fake.mp3", quality="fast")
        self.assertEqual(result["segments"][0]["decoder_text"], " uh-huh")
        self.assertEqual(result["segments"][0]["text"], "uh-huh")
        self.assertTrue(result["segments"][0]["normalization"]["changed"])
        self.assertEqual(
            result["segments"][0]["normalization"]["operations"],
            [{"type": "whitespace_normalization"}],
        )


if __name__ == "__main__":
    unittest.main()
