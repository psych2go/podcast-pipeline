"""Generic provider, failed-stage resume and deterministic mapping contracts."""
import copy
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import agent_pipeline, ai_review, review_contract_repair, review_repair, subagent
from scripts.atomic_io import atomic_write_json
from scripts.content_finalizer import repair_summary_structure
from scripts.content_map import body_sha256, enrich_summary_map_evidence, normalize_detail_items
from scripts.source_relevance import refresh_source_relevance_cache
import test_resume_finalization as resume_fixtures
from test_resume_finalization import package, read_json
from test_review_contract_repair import check, diagnostic


class ProviderOverrideTests(unittest.TestCase):
    def test_provider_applies_to_defaults_qualified_strings_and_transcription(self):
        for env_key in ("PI_PROVIDER", "SUBAGENT_PI_PROVIDER"):
            for task in ("content_writing", "transcript_correction_001", "ai_review"):
                with self.subTest(env_key=env_key, task=task), patch.dict(os.environ, {
                    env_key: "openai", "SUBAGENT_PI_MODEL": "openai-codex/test-model",
                }, clear=True):
                    self.assertEqual(subagent._pi_default_model(task), "openai/test-model")
                    self.assertEqual(subagent.resolve_stage_strategy(
                        task, model="openai-codex/test-model")["resolved_model"],
                        "openai/test-model")
                    self.assertEqual(subagent._pi_model("anthropic/test"), "anthropic/test")
        with patch.dict(os.environ, {"PI_PROVIDER": "ignored", "SUBAGENT_PI_PROVIDER": "openai",
                                     "SUBAGENT_TRANSCRIPTION_MODEL": "openai-codex/correction"},
                        clear=True):
            self.assertEqual(subagent._pi_default_model("transcript_correction"),
                             "openai/correction")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(subagent._pi_model("test"), "openai-codex/test")
            self.assertEqual(subagent._pi_default_model(), "openai-codex/gpt-6.1-sol")
            self.assertIsNone(subagent._pi_default_model("transcript_correction"))

    def test_probe_and_preconfigured_command_model_options_obey_provider(self):
        with patch.dict(os.environ, {"PI_PROVIDER": "openai"}, clear=True):
            for command in (["pi"], ["pi", "--model", "openai-codex/test"],
                            ["pi", "--model=openai-codex/test"]):
                cmd = subagent._pi_command(command, task="probe", schema_path=None,
                    write_files=False, enable_search=False, model="openai-codex/test")
                model = (cmd[cmd.index("--model") + 1] if "--model" in cmd else
                         next(v.split("=", 1)[1] for v in cmd if v.startswith("--model=")))
                self.assertEqual(model, "openai/test")
            for command in (["pi"], ["pi", "--provider", "openai-codex"],
                            ["pi", "--provider=openai-codex"]):
                cmd = subagent._pi_command(command, task="transcript_correction",
                    schema_path=None, write_files=False, enable_search=False, model=None)
                provider = (cmd[cmd.index("--provider") + 1] if "--provider" in cmd else
                            next(v.split("=", 1)[1] for v in cmd
                                 if v.startswith("--provider=")))
                self.assertEqual(provider, "openai")


class FailedStageResumeTests(unittest.TestCase):
    def test_current_valid_writing_is_not_regenerated_for_failed_review(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            folder = Path(td)
            package(folder)
            atomic_write_json(folder / "ai_review.json", {
                "passed": False, "reviewed_files": ai_review.reviewed_hashes(folder),
                "issues": [{"severity": "high", "category": "factuality"}],
            })
            resume_fixtures.ResumeFinalizationTests().upstream(stack)
            self.assertFalse(agent_pipeline.content_pipeline_needed(folder))
            writer = stack.enter_context(patch.object(agent_pipeline, "run_edit_task"))
            self.assertTrue(agent_pipeline.run_content_pipeline(folder, "Synthetic"))
            writer.assert_not_called()

    def test_valid_current_review_returns_without_review_or_revision(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            review = {"passed": True, "reviewed_files": ai_review.reviewed_hashes(folder)}
            atomic_write_json(folder / "ai_review.json", review)
            reviewer = Mock()
            with patch.object(review_repair, "_repair_content_revision") as revise:
                self.assertEqual(review_repair.review_and_repair(folder, reviewer=reviewer), review)
            reviewer.assert_not_called()
            revise.assert_not_called()

    def test_current_failed_review_resumes_at_repair_not_repeat_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            atomic_write_json(folder / "ai_review.json", {
                "passed": False, "reviewed_files": ai_review.reviewed_hashes(folder),
                "issues": [{"severity": "high", "category": "summary_map"}],
            })
            reviewer = Mock(return_value={"passed": True})
            with patch.object(review_repair, "_repair_summary",
                              return_value={"action": "repair"}) as fix:
                result = review_repair.review_and_repair(folder, reviewer=reviewer)
                self.assertTrue(result["passed"])
            fix.assert_called_once()
            reviewer.assert_called_once()

    def test_retry_after_repair_runner_failure_does_not_repeat_revision(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            scope = {"affected_units": ["U0001"], "previous_review": {"fact_checks": [check()]}}
            atomic_write_json(folder / "review_repair.json", {"pending_re_review": {
                "reviewed_files": ai_review.reviewed_hashes(folder), "repair_scope": scope}})
            reviewer = Mock(return_value={"passed": True})
            with patch.object(review_repair, "_repair_content_revision") as revision:
                review_repair.review_and_repair(folder, reviewer=reviewer)
            revision.assert_not_called()
            self.assertEqual(reviewer.call_args.kwargs["repair_scope"], scope)

    def test_completed_revision_checkpoint_survives_reviewer_failure(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            atomic_write_json(folder / "ai_review.json", {
                "passed": False, "reviewed_files": ai_review.reviewed_hashes(folder),
                "issues": [{"severity": "high", "category": "summary_map"}],
            })
            with patch.object(review_repair, "_repair_summary",
                              return_value={"action": "repair"}) as repair:
                with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                    review_repair.review_and_repair(folder, reviewer=Mock(
                        side_effect=RuntimeError("provider unavailable")))
                reviewer = Mock(return_value={"passed": True})
                review_repair.review_and_repair(folder, reviewer=reviewer)
                repair.assert_called_once()
                reviewer.assert_called_once()

    def test_current_contract_diagnostic_resumes_at_source_repair(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            data = {**diagnostic(), "reviewed_files": ai_review.reviewed_hashes(folder)}
            atomic_write_json(folder / "ai_review_contract_failures" / "failed.json", data)
            reviewer = Mock(return_value={"passed": True})
            with patch.object(review_contract_repair, "repair_contract_failure", return_value={
                    "action": "source_repair", "affected_units": ["U0001"]}) as repair:
                review_repair.review_and_repair(folder, reviewer=reviewer)
            repair.assert_called_once()
            reviewer.assert_called_once()
            self.assertEqual(reviewer.call_args.kwargs["repair_scope"]["affected_units"], ["U0001"])

    def test_empty_source_cache_refresh_does_not_invalidate_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            cache = folder / "source_relevance_cache.json"
            atomic_write_json(cache, {"schema_version": 1, "updated_at": "original", "entries": {}})
            before = cache.read_bytes()
            for _ in range(2):
                refresh_source_relevance_cache(folder)
            self.assertEqual(cache.read_bytes(), before)


class TargetedReviewTests(unittest.TestCase):
    def test_contract_attribution_and_public_fact_verdicts_are_repairable(self):
        for changes, reason in (
            ({"claim_origin": "speaker_firsthand", "assertion_type": "fact",
              "verdict": "faithfully_attributed", "verification_mode": "web_required"},
             "firsthand_attribution"),
            ({"claim_origin": "speaker_reported", "assertion_type": "fact",
              "verdict": "supported", "publication_status": "used_as_fact"},
             "firsthand_attribution"),
            ({"claim_origin": "editorial_added", "assertion_type": "fact",
              "publication_status": "used_as_fact", "verdict": "uncertain"}, "public_source"),
        ):
            data = diagnostic()
            data["original_output"]["fact_checks"][0].update(changes)
            frozen = copy.deepcopy(data)
            self.assertEqual(
                review_contract_repair.contract_targets(data)[0]["repair_reason"], reason)
            self.assertEqual(data, frozen)

    def test_failed_status_with_valid_contract_still_looks_up_sources(self):
        item = check()
        item.update(verification_mode="safety_cross_check", source_urls=["https://example.org/old"])
        review = {"passed": False, "fact_checks": [item], "issues": [
            {"severity": "high", "category": "factuality", "statement": "U0001-C01 wrong scope"}]}
        targets = review_contract_repair.failed_review_targets(review)
        self.assertEqual(targets[0]["repair_reason"], "safety_cross_check")
        with tempfile.TemporaryDirectory() as td, patch.object(
                review_contract_repair, "repair_contract_failure", return_value={
                    "action": "lookup_and_revision", "affected_units": ["U0001"]}) as repair:
            reviewer = Mock(side_effect=[review, {"passed": True}])
            result = review_repair.review_and_repair(td, reviewer=reviewer)
        self.assertTrue(result["passed"])
        self.assertEqual(repair.call_args.args[1]["repair_targets"], targets)
        self.assertEqual(reviewer.call_args.kwargs["repair_scope"]["affected_units"], ["U0001"])

    def test_rereview_preserves_only_unaffected_fact_checks_verbatim(self):
        previous = {"fact_checks": [check(), {**check(), "parent_claim_id": "U0002-C01",
                                              "subclaim_id": "U0002-C01-F01"}]}
        frozen = copy.deepcopy(previous)
        scope = {"affected_units": ["U0001"], "previous_review": previous}
        fresh = {**check(), "verdict": "qualified", "source_urls": ["https://example.org/new"]}
        result = ai_review._merge_targeted_checks({"passed": False, "fact_checks": [fresh]}, scope)
        self.assertFalse(result["passed"])
        self.assertEqual(result["fact_checks"], [previous["fact_checks"][1], fresh])
        self.assertEqual(previous, frozen)
        self.assertIn("完整发布判定", ai_review._targeted_review_prompt(scope))
        # A verbatim echo of a retained check is dropped (retained copy wins);
        # a changed out-of-scope verdict is still rejected.
        echoed = ai_review._merge_targeted_checks(
            {"fact_checks": [copy.deepcopy(previous["fact_checks"][1])]}, scope)
        self.assertEqual(echoed["fact_checks"], [previous["fact_checks"][1]])
        with self.assertRaises(RuntimeError):
            ai_review._merge_targeted_checks({"fact_checks": [
                {**previous["fact_checks"][1], "verdict": "contradicted"}]}, scope)
        issue = {"severity": "high", "category": "coverage", "statement": "U0002-C01 missing"}
        scope["previous_review"]["issues"] = [issue]
        result = ai_review._merge_targeted_checks({"passed": True, "fact_checks": [fresh]}, scope)
        self.assertFalse(result["passed"])
        self.assertEqual(result["issues"], [issue])


class DeterministicSummaryTests(unittest.TestCase):
    def test_duplicate_span_merges_all_prose_instead_of_stranding_chapters(self):
        text = "导览。\n\n## A\n\n甲方论证。\n\n## B\n\n中间推理。\n\n## C\n\n乙方限定。\n"
        summary = {"chapters": [
            {"title": "A", "unit_ids": ["U0001"], "claim_ids": ["U0001-C01"]},
            {"title": "B", "unit_ids": ["U0002"], "claim_ids": ["U0002-C01"]},
            {"title": "C", "unit_ids": ["U0001"], "claim_ids": ["U0001-C02"]},
        ]}
        merged, result = repair_summary_structure(text, summary)
        self.assertEqual(len(result["chapters"]), 1)
        self.assertEqual(result["chapters"][0]["unit_ids"], ["U0001", "U0002"])
        self.assertEqual(result["chapters"][0]["claim_ids"],
                         ["U0001-C01", "U0002-C01", "U0001-C02"])
        for body in ("甲方论证。", "中间推理。", "乙方限定。"):
            self.assertIn(body, merged)
        self.assertEqual(repair_summary_structure(merged, result), (merged, result))

    def test_empty_claims_recovered_from_declared_unit_and_empty_unit_from_claim(self):
        text = "## A\n\n正文。"
        content = {"units": [{"id": "U0001", "status": "included", "claims": ["原始主张"]}]}
        for chapter in ({"title": "A", "unit_ids": ["U0001"], "claim_ids": []},
                        {"title": "A", "unit_ids": [], "claim_ids": ["U0001-C01"]}):
            _, summary = repair_summary_structure(text, {"chapters": [chapter]}, content)
            self.assertEqual(summary["chapters"][0]["unit_ids"], ["U0001"])
            self.assertEqual(summary["chapters"][0]["claim_ids"], ["U0001-C01"])

    def test_unmapped_chapter_merges_into_neighbor_without_losing_text(self):
        _, summary = repair_summary_structure("## A\n\n论点。\n\n## B\n\n限定条件。", {
            "chapters": [{"title": "A", "unit_ids": ["U0001"], "claim_ids": ["U0001-C01"]},
                         {"title": "B", "unit_ids": [], "claim_ids": []}]})
        self.assertEqual(len(summary["chapters"]), 1)
        self.assertEqual(summary["chapters"][0]["claim_ids"], ["U0001-C01"])

    def test_number_mapping_requires_actual_detail_not_same_digit_elsewhere(self):
        content = normalize_detail_items({"units": [{
            "id": "U0001", "importance": "high", "status": "included", "claims": [],
            "numbers": ["12%", "30% revenue"], "examples": [],
        }]})
        notes = "比例为百分之十二，其他比例为百分之三十。"
        result = enrich_summary_map_evidence({"chapters": []}, notes, content)
        self.assertEqual(result["notes_number_ids"], ["U0001-N01"])
        self.assertEqual(result["notes_sha256"], body_sha256(notes))
        for different_number in ("百分之十二点五", "百分之一百一十二"):
            result = enrich_summary_map_evidence({"chapters": []}, different_number, content)
            self.assertEqual(result["notes_number_ids"], [])

    def test_duplicate_mapping_resume_merges_without_writing_and_rehashes(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            folder = Path(td)
            package(folder)
            briefing = folder / "讲书稿.md"
            briefing.write_text(briefing.read_text() + "\n## 限定条件\n\n这是同一比例的限定。\n")
            summary = read_json(folder, "summary_map.json")
            summary["chapters"].append({"title": "限定条件", "unit_ids": ["U0001"],
                                        "claim_ids": ["U0001-C01"]})
            # Hash the authored input without invoking the old deduplicator.
            from scripts.content_map import briefing_chapters
            bodies = briefing_chapters(briefing.read_text())
            for chapter in summary["chapters"]:
                chapter["body_sha256"] = body_sha256(bodies[chapter["title"]])
            atomic_write_json(folder / "summary_map.json", summary)
            resume_fixtures.ResumeFinalizationTests().upstream(stack)
            writer = stack.enter_context(patch.object(agent_pipeline, "run_edit_task"))
            self.assertTrue(agent_pipeline.run_content_pipeline(folder, "Synthetic"))
            writer.assert_not_called()
            self.assertEqual(len(read_json(folder, "summary_map.json")["chapters"]), 1)
            self.assertIn("这是同一比例的限定。", briefing.read_text())
            resume_fixtures.ResumeFinalizationTests().assert_finalized(folder)

    def test_mapping_only_resume_repairs_number_without_writer_and_rehashes(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            folder = Path(td)
            package(folder)
            summary = read_json(folder, "summary_map.json")
            summary["notes_number_ids"] = []
            atomic_write_json(folder / "summary_map.json", summary)
            resume_fixtures.ResumeFinalizationTests().upstream(stack)
            writer = stack.enter_context(patch.object(agent_pipeline, "run_edit_task"))
            self.assertTrue(agent_pipeline.run_content_pipeline(folder, "Synthetic"))
            writer.assert_not_called()
            self.assertEqual(read_json(folder, "summary_map.json")["notes_number_ids"],
                             ["U0001-N01"])
            self.assertTrue(agent_pipeline._writing_artifacts_are_current(folder))
