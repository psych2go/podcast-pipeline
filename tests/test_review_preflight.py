"""Deterministic review readiness must be cheap, read-only and fail closed."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import ai_review, quality_report
from scripts.content_finalizer import (
    ContentFinalizationError, finalize_content_package,
)
from scripts.content_map import (
    enrich_content_map_evidence, enrich_summary_map_evidence,
)
from scripts.preflight import ReviewPreflightError, ensure_review_ready


def write_package(folder):
    """Synthetic one-claim episode; no real episode data or model calls."""
    source = "The guest describes a useful research method."
    raw = {
        "source_kind": "third_party_transcript",
        "segments": [{"id": "S0001", "start": 0, "end": 10, "text": source}],
        "meta": {"timestamped": True},
    }
    content_map = {"schema_version": 3, "units": [{
        "id": "U0001", "topic": "研究方法", "claims": ["嘉宾介绍研究方法。"],
        "importance": "high", "status": "included", "timestamps": [[0, 10]],
        "evidence": {"segment_ids": ["S0001"]},
    }]}
    content_map, raw = enrich_content_map_evidence(content_map, raw)
    briefing = (
        "这是一段介绍研究主题、背景、证据范围及全篇结构的全局导览。"
        "我们将先解释问题的由来，再讨论研究方法的适用条件，最后说明结论的边界。\n\n"
        "## 研究方法\n\n" + "嘉宾介绍研究方法并讨论适用条件。" * 30
    )
    notes = "嘉宾介绍研究方法。" * 80
    summary = enrich_summary_map_evidence({
        "schema_version": 2,
        "notes_claim_ids": ["U0001-C01"],
        "chapters": [{"title": "研究方法", "unit_ids": ["U0001"],
                      "claim_ids": ["U0001-C01"]}],
    }, notes, content_map, briefing)
    for name, value in {
        "transcript.raw.json": raw, "content_map.json": content_map,
        "summary_map.json": summary,
    }.items():
        (folder / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    for name, value in {
        "原始转录.txt": source, "讲书稿.md": briefing, "中文完整笔记.md": notes,
        "来源.md": "- 转录质量：第三方转录\n",
    }.items():
        (folder / name).write_text(value, encoding="utf-8")
    return content_map


class ReviewPreflightTests(unittest.TestCase):
    def test_ready_is_not_publish_approval_and_pending_review_is_deferred(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            write_package(folder)
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            report = quality_report.build_review_preflight_report(folder)
            self.assertTrue(report["ready_for_review"], report["errors"])
            self.assertNotIn("passed", report)
            self.assertEqual(report["report_type"], "review_preflight")
            full = quality_report.build_quality_report(folder)
            self.assertFalse(full["passed"])
            self.assertIn("ai_review_missing", [x["code"] for x in full["error_details"]])
            self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_old_review_is_not_a_preflight_dependency(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            write_package(folder)
            # A fresh review can replace a broken/rejected output; do not parse it
            # as a prerequisite, and do not delete or amend it in preflight.
            old = folder / "ai_review.json"
            old.write_text("{broken old output", encoding="utf-8")
            report = ensure_review_ready(folder, persist=False)
            self.assertTrue(report["ready_for_review"])
            self.assertEqual(old.read_text(), "{broken old output")

    def test_blockers_stop_before_source_fetch_or_model_and_preserve_review(self):
        mutations = {
            "summary_map_validation_failed": ("中文完整笔记.md", "改过但未重新绑定的笔记。"),
            "tts_readiness_failed": ("tts_lexicon.json", '{"3/2": "三比二"}'),
            "notes_audit_narration": ("中文完整笔记.md", "公开稿应写成一个故事。"),
            "quality_validation_failed": ("content_map.json", "{broken"),
        }
        for code, (filename, text) in mutations.items():
            with self.subTest(code=code), tempfile.TemporaryDirectory() as td:
                folder = Path(td)
                write_package(folder)
                (folder / filename).write_text(text, encoding="utf-8")
                (folder / "ai_review.json").write_text('{"passed": false}', encoding="utf-8")
                before = {p.name: p.read_bytes() for p in folder.iterdir()}
                with patch.object(ai_review, "run_json_task") as model, patch.object(
                        ai_review, "refresh_source_relevance_cache") as fetch:
                    with self.assertRaises(ReviewPreflightError) as caught:
                        ai_review.run_ai_review(folder, persist=False)
                self.assertIn(code, [x["code"] for x in caught.exception.report["error_details"]])
                model.assert_not_called()
                fetch.assert_not_called()
                self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_run_report_records_preflight_failure_without_replacing_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            old = folder / "ai_review.json"
            old.write_text('{"passed": false}', encoding="utf-8")
            with patch.object(ai_review, "run_json_task") as model:
                with self.assertRaises(ReviewPreflightError):
                    ai_review.review_episode(folder)
            model.assert_not_called()
            diagnostic = json.loads((folder / "review_preflight.json").read_text())
            self.assertFalse(diagnostic["ready_for_review"])
            self.assertNotIn("passed", diagnostic)
            run = json.loads((folder / "run_report.json").read_text())["runs"][-1]
            stage = next(s for s in run["stages"] if s["name"] == "ai_review")
            self.assertEqual(stage["status"], "failed")
            self.assertTrue(stage["metrics"]["blocked_before_model"])
            self.assertIn("transcript_missing", stage["metrics"]["preflight_error_codes"])
            self.assertEqual(old.read_text(), '{"passed": false}')

    def test_required_fact_ledger_is_checked_before_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            content_map = write_package(folder)
            content_map["prewrite_fact_checks_version"] = 1
            (folder / "content_map.json").write_text(json.dumps(content_map), encoding="utf-8")
            report = quality_report.build_review_preflight_report(folder)
            self.assertFalse(report["ready_for_review"])
            self.assertIn(
                "prewrite_fact_checks_invalid",
                [x["code"] for x in report["error_details"]],
            )


class FinalizationSemanticSafetyTests(unittest.TestCase):
    def test_finalizer_preserves_related_but_distinct_entities(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            write_package(folder)
            path = folder / "讲书稿.md"
            tokens = "Artemis I、Artemis II、Artemis III、Artemis IV、X-plane 与 X-59。"
            path.write_text(path.read_text() + "\n" + tokens, encoding="utf-8")
            result = finalize_content_package(folder)
            self.assertIn(tokens, result["briefing"])
            self.assertIn(tokens, path.read_text())

    def test_editorial_instructions_block_without_cosmetic_rewriting(self):
        for name in ("讲书稿.md", "中文完整笔记.md"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                folder = Path(td)
                write_package(folder)
                path = folder / name
                path.write_text(path.read_text() + "\n公开稿应写成一个故事。", encoding="utf-8")
                before = {p.name: p.read_bytes() for p in folder.iterdir()}
                with self.assertRaises(ContentFinalizationError):
                    finalize_content_package(folder)
                self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})


if __name__ == "__main__":
    unittest.main()
