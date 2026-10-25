"""Synthetic resume regressions: real finalizers/gates, no episode data or APIs."""
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scripts import agent_pipeline as pipeline
from scripts.atomic_io import atomic_write_json
from scripts.content_map import (
    deduplicate_summary_units,
    enrich_content_map_evidence,
    enrich_summary_map_evidence,
    normalize_detail_items,
    validate_summary_map,
)
from scripts.process import _validate_finalized_briefing_files


def read_json(folder, name):
    return json.loads((folder / name).read_text(encoding="utf-8"))


def fingerprints(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.iterdir() if p.is_file()}


def package(folder, *, digits=False):
    source = "The speaker described a rate of twelve percent."
    transcript = {"source_kind": "web_transcript",
                  "meta": {"evidence_mode": "text_anchor"},
                  "segments": [{"id": "S0001", "text": source}]}
    content = normalize_detail_items({"evidence_mode": "text_anchor", "units": [{
        "id": "U0001", "topic": "讨论", "importance": "high", "status": "included",
        "claims": ["节目讨论比例"], "numbers": ["12%"], "examples": [],
        "timestamps": [], "evidence": {"segment_ids": ["S0001"]},
    }]})
    content, transcript = enrich_content_map_evidence(content, transcript)
    atomic_write_json(folder / "transcript.raw.json", transcript)
    atomic_write_json(folder / "content_map.json", content)
    atomic_write_json(folder / "canonical_entities.json", {"entities": []})
    (folder / "原始转录.txt").write_text(source, encoding="utf-8")
    notes = "节目讨论百分之十二的比例。"
    briefing = "全局导览介绍本期讨论的比例及其限定条件。\n\n## 比例讨论\n\n" + (
        "节目讨论12%的比例。" if digits else "节目讨论百分之十二的比例。"
    ) + "这是节目中的解释并非适用于所有情形。" * 40 + "\n"
    (folder / "中文完整笔记.md").write_text(notes, encoding="utf-8")
    (folder / "讲书稿.md").write_text(briefing, encoding="utf-8")
    summary = {"chapters": [{"title": "比例讨论", "unit_ids": ["U0001"],
                              "claim_ids": ["U0001-C01"]}],
               "notes_claim_ids": ["U0001-C01"], "notes_number_ids": ["U0001-N01"],
               "notes_example_ids": []}
    summary = enrich_summary_map_evidence(summary, notes, content, briefing)
    summary["transcript_basis"] = pipeline._transcript_basis(folder)
    summary["writing_inputs_version"] = 1
    summary["writing_inputs"] = pipeline._writing_input_hashes(folder)
    atomic_write_json(folder / "summary_map.json", summary)


class ResumeFinalizationTests(unittest.TestCase):
    def upstream(self, stack):
        # Only skip unrelated acquisition/AI stages; finalization, evidence
        # enrichment, reuse checks and deterministic validators remain real.
        for name, value in (
            ("effective_source_kind", "web_transcript"),
            ("quality_metadata", {"transcript_status": "可接受"}),
            ("_content_map_is_valid", True),
            ("_canonical_entities_is_valid", True),
            ("ledger_is_current", True),
            ("sync_episode_state", None),
        ):
            stack.enter_context(patch.object(pipeline, name, return_value=value))

    def assert_finalized(self, folder):
        errors, _ = _validate_finalized_briefing_files(folder, folder / "讲书稿.md")
        self.assertEqual(errors, [])
        self.assertTrue(pipeline._writing_artifacts_are_current(folder))

    def reject_current_package(self, folder):
        review = {"passed": False, "reviewed_files": fingerprints(folder),
                  "issues": [{"category": "coverage", "description": "补充比例说明"}]}
        atomic_write_json(folder / "ai_review.json", review)
        return (folder / "ai_review.json").read_bytes()

    def test_reused_prose_is_rehashed_after_digit_repair_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            folder = Path(td)
            package(folder, digits=True)
            immutable = {name: (folder / name).read_bytes()
                         for name in ("原始转录.txt", "transcript.raw.json")}
            self.upstream(stack)
            writer = stack.enter_context(patch.object(pipeline, "run_edit_task"))
            self.assertTrue(pipeline.run_content_pipeline(folder, "Synthetic"))
            writer.assert_not_called()
            self.assertIn("百分之十二", (folder / "讲书稿.md").read_text())
            self.assert_finalized(folder)
            before = fingerprints(folder)
            self.assertTrue(pipeline.run_content_pipeline(folder, "Synthetic"))
            self.assertEqual(before, fingerprints(folder))
            for name, data in immutable.items():
                self.assertEqual((folder / name).read_bytes(), data)

    def test_digit_and_lexicon_repairs_finish_before_hash_binding(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            folder = Path(td)
            package(folder, digits=True)
            briefing = folder / "讲书稿.md"
            briefing.write_text(briefing.read_text() + "\nExampleAI 的讨论。\n", encoding="utf-8")
            summary = enrich_summary_map_evidence(
                read_json(folder, "summary_map.json"),
                (folder / "中文完整笔记.md").read_text(),
                read_json(folder, "content_map.json"), briefing.read_text())
            atomic_write_json(folder / "summary_map.json", summary)
            self.upstream(stack)

            def pronounce(_folder, _prompt, **kwargs):
                self.assertEqual(kwargs["task_name"], "tts_lexicon_pre_review")
                self.assertNotIn("12%", briefing.read_text())
                atomic_write_json(folder / "tts_lexicon.json", {"ExampleAI": "示例人工智能"})

            writer = stack.enter_context(patch.object(
                pipeline, "run_edit_task", side_effect=pronounce))
            self.assertTrue(pipeline.run_content_pipeline(folder, "Synthetic"))
            writer.assert_called_once()
            self.assert_finalized(folder)

    def test_entity_alias_repair_rehashes_actual_notes_and_briefing(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            folder = Path(td)
            package(folder)
            atomic_write_json(folder / "canonical_entities.json", {"entities": [{
                "canonical_name": "Example Company", "observed_names": ["Exampel"],
                "public_aliases": [], "entity_type": "company",
            }]})
            self.upstream(stack)

            def write(_folder, _prompt, **kwargs):
                self.assertEqual(kwargs["task_name"], "content_writing")
                for name in ("中文完整笔记.md", "讲书稿.md"):
                    path = folder / name
                    path.write_text(path.read_text() + "\nExampel 的解释。\n", encoding="utf-8")

            writer = stack.enter_context(patch.object(
                pipeline, "run_edit_task", side_effect=write))
            self.assertTrue(pipeline.run_content_pipeline(folder, "Synthetic"))
            writer.assert_called_once()
            for name in ("中文完整笔记.md", "讲书稿.md"):
                self.assertIn("Example Company", (folder / name).read_text())
                self.assertNotIn("Exampel", (folder / name).read_text())
            self.assert_finalized(folder)

    def test_notes_modified_during_reused_finalization_are_reloaded_and_bound(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            folder = Path(td)
            package(folder)
            self.upstream(stack)
            original = pipeline._finalize_with_depth_repair

            def expand(target):
                result = original(target)
                notes = folder / "中文完整笔记.md"
                notes.write_text(notes.read_text() + "补充原有解释的限定条件。", encoding="utf-8")
                return result

            stack.enter_context(patch.object(
                pipeline, "_finalize_with_depth_repair", side_effect=expand))
            writer = stack.enter_context(patch.object(pipeline, "run_edit_task"))
            self.assertTrue(pipeline.run_content_pipeline(folder, "Synthetic"))
            writer.assert_not_called()
            self.assert_finalized(folder)

    def test_rejected_review_never_bypasses_artifact_validation(self):
        for defect in ("missing_number", "notes_hash", "unchanged", "valid_change"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as td, \
                    ExitStack() as stack:
                folder = Path(td)
                package(folder)
                review_bytes = self.reject_current_package(folder)
                if defect == "missing_number":
                    summary = read_json(folder, "summary_map.json")
                    summary["notes_number_ids"] = []
                    atomic_write_json(folder / "summary_map.json", summary)
                elif defect == "notes_hash":
                    (folder / "中文完整笔记.md").write_text("修订后的比例说明。", encoding="utf-8")
                elif defect == "valid_change":
                    summary = read_json(folder, "summary_map.json")
                    summary["repair_note"] = "metadata only"
                    atomic_write_json(folder / "summary_map.json", summary)
                self.upstream(stack)

                def write(_folder, _prompt, *, folder=folder, **kwargs):
                    self.assertEqual(kwargs["task_name"], "content_writing")
                    # Model-authored output covers the numeric detail in prose
                    # and in the index; the pipeline must not invent the index.
                    (folder / "中文完整笔记.md").write_text(
                        "节目讨论百分之十二的比例。", encoding="utf-8")
                    summary = read_json(folder, "summary_map.json")
                    summary["notes_number_ids"] = ["U0001-N01"]
                    atomic_write_json(folder / "summary_map.json", summary)

                writer = stack.enter_context(patch.object(
                    pipeline, "run_edit_task", side_effect=write))
                self.assertTrue(pipeline.run_content_pipeline(folder, "Synthetic"))
                self.assertEqual(writer.call_count, int(defect != "valid_change"))
                self.assert_finalized(folder)
                # Content recovery never changes the verdict; independent
                # review remains mandatory with its original stale hashes.
                self.assertEqual((folder / "ai_review.json").read_bytes(), review_bytes)

    def duplicate_chapters(self, folder):
        summary = read_json(folder, "summary_map.json")
        duplicate = copy.deepcopy(summary["chapters"][0])
        duplicate["title"] = "另一个角度"
        summary["chapters"].append(duplicate)
        atomic_write_json(folder / "summary_map.json", summary)
        briefing = folder / "讲书稿.md"
        briefing.write_text(briefing.read_text() + "\n## 另一个角度\n\n"
                            + "这是同一来源主张的另一个解释角度。" * 40 + "\n", encoding="utf-8")

    def test_duplicate_dedup_never_empties_a_chapter_or_moves_its_claims(self):
        summary = {"chapters": [
            {"title": "First", "unit_ids": ["U0001"], "claim_ids": ["U0001-C01"]},
            {"title": "Second", "unit_ids": ["U0001"], "claim_ids": ["U0001-C02"]},
        ]}
        before = copy.deepcopy(summary)
        self.assertFalse(deduplicate_summary_units(summary))
        self.assertEqual(summary, before)
        self.assertTrue(validate_summary_map(summary))

    def test_invalid_generated_chapters_get_bounded_writer_feedback(self):
        for repaired in (True, False):
            with self.subTest(repaired=repaired), tempfile.TemporaryDirectory() as td, \
                    ExitStack() as stack:
                folder = Path(td)
                package(folder)
                good_summary = read_json(folder, "summary_map.json")
                good_briefing = (folder / "讲书稿.md").read_text()
                self.duplicate_chapters(folder)
                self.upstream(stack)

                def write(_folder, prompt, *, repaired=repaired, folder=folder,
                          good_summary=good_summary, good_briefing=good_briefing, **kwargs):
                    if kwargs["task_name"] == "content_writing_contract_repair":
                        self.assertIn("重复映射", prompt)
                        if repaired:
                            atomic_write_json(folder / "summary_map.json", good_summary)
                            (folder / "讲书稿.md").write_text(good_briefing, encoding="utf-8")

                writer = stack.enter_context(patch.object(
                    pipeline, "run_edit_task", side_effect=write))
                self.assertEqual(pipeline.run_content_pipeline(folder, "Synthetic"), repaired)
                self.assertEqual(writer.call_count, 2 if repaired else 3)
                for call in writer.call_args_list:
                    self.assertEqual({p.name for p in call.kwargs["allowed_files"]},
                                     {"中文完整笔记.md", "讲书稿.md", "summary_map.json"})
                    self.assertFalse(set(call.kwargs["allowed_files"])
                                     & set(call.kwargs["input_files"]))
                if repaired:
                    self.assert_finalized(folder)
                else:
                    self.assertTrue(validate_summary_map(read_json(folder, "summary_map.json")))

    def test_missing_detail_is_not_mechanically_filled_on_exhaustion(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            folder = Path(td)
            package(folder)
            summary = read_json(folder, "summary_map.json")
            summary["notes_number_ids"] = []
            atomic_write_json(folder / "summary_map.json", summary)
            self.upstream(stack)
            writer = stack.enter_context(patch.object(pipeline, "run_edit_task"))
            self.assertFalse(pipeline.run_content_pipeline(folder, "Synthetic"))
            self.assertEqual(writer.call_count, 3)
            self.assertEqual(read_json(folder, "summary_map.json")["notes_number_ids"], [])


if __name__ == "__main__":
    unittest.main()
