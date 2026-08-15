import hashlib
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from hashing import sha256_bytes, sha256_file, sha256_text
from html_gen import parse_sections as html_parse_sections
from preflight import _review_recovery_decision
from process import (
    EpisodeOptions, _process_impl, _quality_metrics as process_quality_metrics,
)
from tts import split_sections as tts_split_sections
from pipeline_metrics import quality_metrics
from quality_errors import (
    AI_REVIEW_FAILED, AI_REVIEW_MISSING, AI_REVIEW_STALE,
    SOURCE_REVIEW_STATUS, add_error, coded_errors,
)
from sections import parse_markdown_sections
from text_distance import edit_details, levenshtein_distance


class SharedSectionParserTests(unittest.TestCase):
    def test_html_and_tts_adapters_share_one_section_model(self):
        text = "导览段。\n\n## 第一章\n正文一。\n\n## 第二章\n正文二。"
        parsed = parse_markdown_sections(text)
        self.assertEqual(
            [(item.index, item.title, item.body) for item in parsed],
            [
                (-1, None, "导览段。"),
                (0, "第一章", "正文一。"),
                (1, "第二章", "正文二。"),
            ],
        )
        self.assertEqual(
            html_parse_sections(text),
            [(item.index, item.title, item.body) for item in parsed],
        )
        self.assertEqual(
            tts_split_sections(text),
            [(item.title, item.body) for item in parsed],
        )

    def test_orphan_text_after_first_heading_is_not_silently_dropped(self):
        text = "## 第一章\n正文。\n游离段仍属于第一章。"
        parsed = parse_markdown_sections(text)
        self.assertEqual(len(parsed), 1)
        self.assertIn("游离段仍属于第一章", parsed[0].body)


class SharedMetricsTests(unittest.TestCase):
    def test_process_and_shared_quality_metrics_match(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "quality_report.json").write_text(json.dumps({
                "passed": False,
                "errors": ["one", "two"],
                "warnings": ["warning"],
                "error_details": [
                    {"code": "one", "message": "one"},
                    {"code": "two", "message": "two"},
                ],
                "coverage": {
                    "claim_coverage": 0.9,
                    "notes_claim_coverage": 0.8,
                },
            }), encoding="utf-8")
            expected = quality_metrics(folder)
            self.assertEqual(process_quality_metrics(folder), expected)
            self.assertEqual(expected["error_codes"], ["one", "two"])


class SharedUtilityTests(unittest.TestCase):
    def test_hashing_helpers_have_one_canonical_result(self):
        payload = b"podcast pipeline"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "payload.bin"
            path.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            self.assertEqual(sha256_bytes(payload), expected)
            self.assertEqual(sha256_text(payload.decode()), expected)
            self.assertEqual(sha256_file(path), expected)

    def test_distance_and_edit_details_share_error_count(self):
        reference = ["one", "two", "three"]
        hypothesis = ["one", "too", "three", "four"]
        details = edit_details(reference, hypothesis)
        self.assertEqual(levenshtein_distance(reference, hypothesis), 2)
        self.assertEqual(details["errors"], 2)
        self.assertEqual(
            details["insertions"] + details["deletions"]
            + details["substitutions"],
            details["errors"],
        )


class EpisodeOptionsTests(unittest.TestCase):
    def test_internal_process_interface_uses_parameter_object(self):
        parameters = list(inspect.signature(_process_impl).parameters)
        self.assertEqual(
            parameters, ["source", "name", "folder", "run_report", "options"])

    def test_mode_is_derived_once(self):
        self.assertEqual(EpisodeOptions(html_only=True).mode, "html-only")
        self.assertEqual(EpisodeOptions(tts_only=True).mode, "tts-only")
        self.assertEqual(EpisodeOptions(fetch_only=True).mode, "fetch-only")
        self.assertEqual(EpisodeOptions().mode, "full")


class StructuredQualityErrorTests(unittest.TestCase):
    def test_checks_emit_stable_codes_without_text_classification(self):
        report = {"errors": [], "error_details": []}
        coded_errors(report)
        add_error(report, AI_REVIEW_MISSING,
                  "缺少 ai_review.json，不能自动发布")
        add_error(report, AI_REVIEW_STALE,
                  "AI 审查已过期，文件变更: ['讲书稿.md']")
        self.assertEqual(
            [item["code"] for item in report["error_details"]],
            [AI_REVIEW_MISSING, AI_REVIEW_STALE])
        missing, can_auto = _review_recovery_decision(report)
        self.assertTrue(missing)
        self.assertTrue(can_auto)

    def test_unrelated_coded_error_is_not_auto_reviewable(self):
        report = {"errors": [], "error_details": []}
        coded_errors(report)
        add_error(report, AI_REVIEW_MISSING,
                  "缺少 ai_review.json，不能自动发布")
        add_error(report, AI_REVIEW_FAILED, "AI 最终审查未通过")
        missing, can_auto = _review_recovery_decision(report)
        self.assertTrue(missing)
        self.assertFalse(can_auto)


if __name__ == "__main__":
    unittest.main()
