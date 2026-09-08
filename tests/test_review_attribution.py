import json
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ai_review
import catalog_health
import catalog_triage
import review_repair
from review_attribution import (
    MAX_ATTRIBUTED_ISSUES,
    STATEMENT_HEAD_CHARS,
    attribute_rejection,
    normalize_category,
)


def _failed_review(**overrides):
    review = {
        "passed": False,
        "transcript_quality": {"passed": True, "score": 96, "issues": []},
        "coverage": {"passed": True, "score": 95, "missing_topics": []},
        "factuality": {"passed": True, "score": 93, "issues": []},
        "numbers": {"passed": True, "issues": []},
        "attribution": {"passed": True, "issues": []},
        "entity_accuracy": {
            "passed": True, "issues": [], "checked_entities": [],
        },
        "tts": {"passed": True, "issues": []},
        "publish": {"passed": True, "issues": []},
        "issues": [],
        "reviewed_files": {"讲书稿.md": "hash"},
    }
    review.update(overrides)
    return review


class NormalizeCategoryTests(unittest.TestCase):
    def test_exact_alias_wins_over_prefix(self):
        # "coverage_" prefix would map to coverage; the exact alias must win.
        self.assertEqual(
            normalize_category("coverage_summary_map_mismatch"), "summary_map")

    def test_known_prefixes(self):
        self.assertEqual(normalize_category("numbers_brand_new"), "numbers")
        self.assertEqual(normalize_category("number_typo_new"), "numbers")
        self.assertEqual(normalize_category("entity_mismatch_new"), "entity")
        self.assertEqual(normalize_category("tts_whatever"), "tts")
        self.assertEqual(normalize_category("briefing_style_x"), "audit_narration")

    def test_observed_snapshot_values(self):
        cases = {
            "briefing_style": "audit_narration",
            "entity_accuracy": "entity",
            "numbers_geography_mismatch": "numbers",
            "numbers_scope_misattribution": "numbers",
            "attribution_time_scope_drift": "attribution",
            "personal_relationship_attribution": "attribution",
            "transcript_traceability": "transcript",
            "content_map_evidence_warning": "evidence_integrity",
            "fact_check_ledger_schema": "fact_check_contract",
            "allegation_source_document_missing": "fact_check_contract",
            "dynamic_policy_number": "numbers",
            "dynamic_context": "factuality",
            "tts_substring_collision": "tts",
            "tts_lexicon_coverage": "tts",
            "coverage_mapping": "coverage",
            "claim_evidence": "evidence_integrity",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_category(raw), expected, raw)

    def test_unknown_and_empty_fall_back_to_other(self):
        self.assertEqual(normalize_category("brand_new_model_idea"), "other")
        self.assertEqual(normalize_category(""), "other")
        self.assertEqual(normalize_category(None), "other")


class AttributeRejectionTests(unittest.TestCase):
    def test_codes_cover_severe_section_and_score(self):
        review = _failed_review(
            factuality={"passed": False, "score": 88, "issues": ["x"]},
            coverage={"passed": True, "score": 85, "missing_topics": []},
            issues=[{
                "severity": "high",
                "category": "briefing_style",
                "file": "中文完整笔记.md",
                "statement": "后台叙述泄漏",
                "repair_kind": "manual",
            }],
        )
        result = attribute_rejection(review)
        self.assertEqual(
            result["codes"],
            ["ai_review_severe_issue", "ai_review_section_failed",
             "ai_review_score_below_threshold"],
        )
        self.assertEqual(result["sections_failed"], ["factuality"])
        self.assertEqual(result["scores"]["factuality"], 88)
        self.assertEqual(result["scores"]["coverage"], 85)
        self.assertEqual(
            result["issue_counts"], {"audit_narration": 1})
        self.assertEqual(result["issues"][0]["normalized"], "audit_narration")
        self.assertEqual(result["issues"][0]["severity"], "high")

    def test_score_only_rejection(self):
        review = _failed_review(
            factuality={"passed": True, "score": 87, "issues": []})
        result = attribute_rejection(review)
        self.assertEqual(result["codes"], ["ai_review_score_below_threshold"])
        self.assertEqual(result["issue_counts"], {})

    def test_issue_cap_and_statement_truncation(self):
        long_statement = "很" * 500
        issues = [{
            "severity": "high",
            "category": f"numbers_kind_{index}",
            "file": "讲书稿.md",
            "statement": long_statement,
            "repair_kind": "manual",
        } for index in range(MAX_ATTRIBUTED_ISSUES + 5)]
        result = attribute_rejection(_failed_review(issues=issues))
        self.assertEqual(len(result["issues"]), MAX_ATTRIBUTED_ISSUES)
        for item in result["issues"]:
            self.assertLessEqual(len(item["statement_head"]), STATEMENT_HEAD_CHARS)
        # Counts include issues beyond the detail cap.
        self.assertEqual(
            sum(result["issue_counts"].values()),
            MAX_ATTRIBUTED_ISSUES + 5)

    def test_only_blocking_severities_are_counted(self):
        review = _failed_review(issues=[
            {"severity": "medium", "category": "factuality",
             "file": "讲书稿.md", "statement": "m", "repair_kind": "manual"},
            {"severity": "low", "category": "tts",
             "file": "讲书稿.md", "statement": "l", "repair_kind": "safe_tts"},
            {"severity": "HIGH", "category": "numbers",
             "file": "讲书稿.md", "statement": "h", "repair_kind": "manual"},
        ])
        result = attribute_rejection(review)
        self.assertEqual(result["issue_counts"], {"numbers": 1})

    def test_unknown_category_lands_in_other_with_raw_kept(self):
        review = _failed_review(issues=[{
            "severity": "critical", "category": "totally_new_thing",
            "file": "讲书稿.md", "statement": "x", "repair_kind": "manual",
        }])
        result = attribute_rejection(review)
        self.assertEqual(result["issue_counts"], {"other": 1})
        self.assertEqual(
            result["other_raw_counts"], {"totally_new_thing": 1})
        self.assertEqual(result["issues"][0]["category"], "totally_new_thing")

    def test_malformed_review_is_safe(self):
        result = attribute_rejection({"passed": False})
        self.assertEqual(result["codes"], [])
        self.assertEqual(result["issues"], [])
        self.assertEqual(attribute_rejection("junk")["codes"], [])


class ReviewRepairAttributionTests(unittest.TestCase):
    def test_history_records_structured_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            failed = _failed_review(issues=[{
                "severity": "high", "category": "factuality",
                "file": "讲书稿.md", "statement": "unsupported",
                "repair_kind": "manual",
            }])
            passed = {"passed": True, "issues": [], "reviewed_files": {}}
            reviewer = unittest.mock.Mock(side_effect=[failed, passed])
            with unittest.mock.patch(
                    "review_repair._repair_summary",
                    return_value={"action": "finalize_content_package"}):
                result = review_repair.review_and_repair(
                    folder, reviewer=reviewer, max_rounds=1)
            self.assertFalse(result["passed"])
            log = json.loads(
                (folder / "review_repair.json").read_text(encoding="utf-8"))
            rejection = log["history"][0]["rejection"]
            self.assertIn("ai_review_severe_issue", rejection["codes"])
            self.assertEqual(rejection["issue_counts"], {"factuality": 1})


class HealthAttributionTests(unittest.TestCase):
    def _episode(self, content, name, run_payload, quality_payload):
        folder = content / name
        folder.mkdir()
        (folder / "episode.json").write_text(
            '{"quality":{"mode":"strict"}}', encoding="utf-8")
        (folder / "run_report.json").write_text(
            json.dumps(run_payload), encoding="utf-8")
        (folder / "quality_report.json").write_text(
            json.dumps(quality_payload), encoding="utf-8")
        return folder

    def test_health_aggregates_rejections_and_prefers_codes(self):
        now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            content = Path(td)
            self._episode(content, "Blocked Episode", {
                "schema_version": 1,
                "runs": [{
                    "command": "process",
                    "started_at": "2026-09-08T10:00:00+00:00",
                    "status": "failed",
                    "metadata": {},
                    "stages": [{
                        "name": "ai_review",
                        "status": "failed",
                        "duration_seconds": 900,
                        "error": "AI review did not pass",
                        "metrics": {
                            "rejection": {
                                "codes": [
                                    "ai_review_severe_issue",
                                    "ai_review_section_failed",
                                ],
                                "issue_counts": {
                                    "audit_narration": 2, "numbers": 1,
                                },
                                "other_raw_counts": {"weird_new_one": 1},
                            },
                        },
                    }],
                }],
            }, {
                "passed": False,
                "errors": ["中文错误文案"],
                "error_details": [{
                    "code": "briefing_audit_narration",
                    "message": "中文错误文案",
                }],
            })
            report = catalog_health.build_health_report(
                content, since="7d", now=now)
        self.assertIn("## AI 审查拒绝归因", report)
        self.assertIn("| 1 | ai_review_severe_issue |", report)
        self.assertIn("| 1 | ai_review_section_failed |", report)
        self.assertIn("| 2 | audit_narration |", report)
        self.assertIn("| 1 | numbers |", report)
        self.assertIn("- 1 × weird_new_one", report)
        self.assertIn("| 1 | Blocked Episode |", report)
        # Unpublished table prefers the stable code over localized text.
        self.assertIn("| briefing_audit_narration |", report)
        self.assertNotIn("| 中文错误文案 |", report)


def _write_json(path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _snapshot_review(passed=False, reviewed_at="2026-09-07T07:00:00+00:00",
                     semantic_hash="same", category="briefing_style"):
    semantic = {
        name: semantic_hash
        for name in catalog_triage.SEMANTIC_INPUT_FILES
    }
    return {
        "passed": passed,
        "reviewed_at": reviewed_at,
        "issues": [{
            "severity": "high",
            "category": category,
            "file": "中文完整笔记.md",
            "statement": "后台叙述",
            "repair_kind": "manual",
        }],
        # episode.json / 来源.md differ on purpose: the review itself writes
        # status transitions into them after every attempt.
        "reviewed_files": {
            **semantic,
            "episode.json": f"episode-{reviewed_at}",
            "来源.md": f"source-{reviewed_at}",
        },
    }


class ReviewEpisodeMetricsTests(unittest.TestCase):
    def _episode(self, folder):
        for name in ai_review.REVIEW_FILES:
            if name == "episode.json":
                text = json.dumps({
                    "schema_version": 1,
                    "display_title": "Episode",
                    "source": {"url": "https://example.com"},
                    "quality": {"content_review_status": "pending"},
                })
            elif name == "来源.md":
                text = "# 来源信息\n"
            else:
                text = f"current {name}"
            (folder / name).write_text(text, encoding="utf-8")

    def test_failed_review_records_rejection_metrics_in_run_report(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._episode(folder)
            review = _failed_review(
                reviewed_at="2026-09-08T00:00:00+00:00",
                factuality={"passed": False, "score": 85, "issues": ["x"]},
                issues=[{
                    "severity": "high", "category": "briefing_style",
                    "file": "讲书稿.md", "statement": "后台叙述",
                    "repair_kind": "manual",
                }],
                fact_checks=[], reviewer={"model": "test"},
                reviewed_files=ai_review.reviewed_hashes(folder),
                review_context=ai_review.review_context_hashes(folder),
            )
            report = ai_review.RunReport(folder, "ai_review", {})
            with unittest.mock.patch.object(
                    ai_review, "run_ai_review",
                    return_value=review), \
                    unittest.mock.patch.object(
                        ai_review, "update_cache_from_review",
                        return_value=0):
                ai_review.review_episode(folder, run_report=report)
            report.finish(False, "AI review did not pass")
            saved = json.loads(
                (folder / "run_report.json").read_text(encoding="utf-8"))
            stage = saved["runs"][0]["stages"][0]
            self.assertEqual(stage["status"], "failed")
            rejection = stage["metrics"]["rejection"]
            self.assertIn("ai_review_severe_issue", rejection["codes"])
            self.assertIn("ai_review_section_failed", rejection["codes"])
            self.assertIn("ai_review_score_below_threshold", rejection["codes"])
            self.assertEqual(rejection["sections_failed"], ["factuality"])
            self.assertEqual(
                rejection["issue_counts"], {"audit_narration": 1})
            self.assertIn("failure_snapshot", stage["metrics"])


class TriageTests(unittest.TestCase):
    def _folder(self, td, name="Episode"):
        folder = Path(td) / name
        folder.mkdir()
        return folder

    def test_healthy_episode(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._folder(td)
            _write_json(folder / "quality_report.json", {"passed": True})
            _write_json(folder / "ai_review.json", {"passed": True})
            report = catalog_triage.triage_episode(folder)
        self.assertFalse(report["blocked"])
        self.assertEqual(report["recommendation"]["action"], "healthy")

    def test_missing_review_recommends_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._folder(td)
            report = catalog_triage.triage_episode(folder)
        self.assertTrue(report["blocked"])
        self.assertEqual(report["recommendation"]["action"], "rerun")
        self.assertIn("缺少 AI 审查记录", report["recommendation"]["detail"])

    def test_stale_review_code_recommends_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._folder(td)
            _write_json(folder / "quality_report.json", {
                "passed": False,
                "error_details": [{"code": "ai_review_stale"}],
            })
            _write_json(folder / "ai_review.json", {"passed": True})
            report = catalog_triage.triage_episode(folder)
        self.assertEqual(report["recommendation"]["action"], "rerun")

    def test_transient_runner_failure_recommends_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._folder(td)
            _write_json(
                folder / "ai_review.json",
                _snapshot_review(semantic_hash="earlier-rejection"))
            _write_json(folder / "run_report.json", {
                "runs": [{
                    "stages": [{
                        "name": "ai_review",
                        "status": "failed",
                        "error": "ERROR: unexpected status 502",
                    }],
                }],
            })
            report = catalog_triage.triage_episode(folder)
        self.assertEqual(report["recommendation"]["action"], "rerun")
        self.assertIn("runner/服务故障", report["recommendation"]["detail"])

    def test_single_rejection_recommends_content_regen(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._folder(td)
            _write_json(
                folder / "ai_review.json",
                _snapshot_review(semantic_hash="only-one"))
            report = catalog_triage.triage_episode(folder)
        self.assertEqual(
            report["recommendation"]["action"], "content_regen")
        self.assertIn(
            "audit_narration", report["recommendation"]["blocking_categories"])
        # Only one failure: static_inputs is unknown, never guessed.
        self.assertIsNone(report["loops"]["static_inputs"])

    def test_repeated_static_rejection_flags_checker_suspect(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._folder(td)
            snapshots = folder / "ai_review_failures"
            snapshots.mkdir()
            _write_json(
                snapshots / "first.json",
                _snapshot_review(reviewed_at="2026-09-07T07:00:00+00:00"))
            _write_json(
                snapshots / "second.json",
                _snapshot_review(reviewed_at="2026-09-07T08:00:00+00:00"))
            _write_json(
                folder / "ai_review.json",
                _snapshot_review(reviewed_at="2026-09-07T08:00:00+00:00"))
            report = catalog_triage.triage_episode(folder)
        self.assertEqual(
            report["recommendation"]["action"], "checker_suspect")
        loops = report["loops"]
        self.assertTrue(loops["static_inputs"])
        self.assertEqual(loops["repeated_categories"], ["audit_narration"])
        self.assertEqual(loops["compared"], ["first.json", "second.json"])

    def test_changed_semantic_inputs_do_not_flag_checker(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._folder(td)
            snapshots = folder / "ai_review_failures"
            snapshots.mkdir()
            _write_json(
                snapshots / "first.json",
                _snapshot_review(
                    reviewed_at="2026-09-07T07:00:00+00:00",
                    semantic_hash="before"))
            _write_json(
                snapshots / "second.json",
                _snapshot_review(
                    reviewed_at="2026-09-07T08:00:00+00:00",
                    semantic_hash="after-rewrite"))
            _write_json(
                folder / "ai_review.json",
                _snapshot_review(
                    reviewed_at="2026-09-07T08:00:00+00:00",
                    semantic_hash="after-rewrite"))
            report = catalog_triage.triage_episode(folder)
        self.assertFalse(report["loops"]["static_inputs"])
        self.assertEqual(
            report["recommendation"]["action"], "content_regen")

    def test_review_passed_but_gate_blocks_is_gate_only(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._folder(td)
            _write_json(folder / "ai_review.json", {"passed": True})
            _write_json(folder / "quality_report.json", {
                "passed": False,
                "error_details": [{"code": "tts_readiness_failed"}],
            })
            report = catalog_triage.triage_episode(folder)
        self.assertEqual(report["recommendation"]["action"], "gate_only")
        self.assertIn(
            "tts_readiness_failed", report["quality"]["codes"])

    def test_triage_all_skips_legacy_and_filters_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            content = Path(td)
            blocked = content / "Blocked"
            blocked.mkdir()
            _write_json(blocked / "episode.json", {
                "quality": {"mode": "strict"},
            })
            _write_json(blocked / "quality_report.json", {"passed": False})
            legacy = content / "Legacy"
            legacy.mkdir()
            _write_json(legacy / "episode.json", {
                "quality": {"mode": "legacy"},
            })
            reports = catalog_triage.triage_all(content)
            self.assertEqual(
                [report["episode"] for report in reports], ["Blocked"])
            blocked_only = catalog_triage.triage_all(
                content, blocked_only=True)
            self.assertEqual(len(blocked_only), 1)

    def test_markdown_render_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            folder = self._folder(td)
            _write_json(
                folder / "ai_review.json",
                _snapshot_review(semantic_hash="x"))
            report = catalog_triage.triage_episode(folder)
        text = catalog_triage.render_markdown([report])
        self.assertIn("## Episode", text)
        self.assertIn("建议【content_regen】", text)
        self.assertIn(
            "`.venv/bin/python scripts/process.py --name \"Episode\"`", text)


if __name__ == "__main__":
    unittest.main()
