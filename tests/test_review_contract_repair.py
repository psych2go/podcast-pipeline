"""Synthetic contract recovery regressions; never access episode content/network."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import ai_review, review_contract_repair as repair, review_repair
from scripts.atomic_io import atomic_write_json
from scripts.content_map import (
    body_sha256, deduplicate_summary_units, enrich_content_map_evidence,
    enrich_summary_map_evidence, validate_content_map, validate_summary_map,
)
from scripts.content_finalizer import finalize_content_artifacts


def check(kind="safety_cross_check"):
    item = {
        "claim": "嘉宾建议自动部署补丁", "parent_claim_id": "U0001-C01",
        "subclaim_id": "U0001-C01-F01", "claim_type": "not_applicable",
        "claim_origin": "speaker_reported", "speaker_role": "guest",
        "assertion_type": "recommendation", "verification_mode": "transcript_attribution",
        "risk_domain": "safety", "verdict": "qualified",
        "publication_status": "attributed_or_qualified", "evidence_segment_ids": ["S0001"],
        "source_urls": [], "checked_at": "2026-01-01", "notes": "",
    }
    if kind == "source_document":
        item.update(assertion_type="allegation", verification_mode="source_document_required")
    if kind == "firsthand_attribution":
        item.update(claim_origin="speaker_firsthand", assertion_type="fact", verdict="uncertain")
    return item


def diagnostic(kind="safety_cross_check"):
    return {"original_output": {"passed": True, "fact_checks": [check(kind)]},
            "audit": {"initial_errors": ["contract error"]}}


def package(folder):
    transcript = {"meta": {"evidence_mode": "text_anchor"}, "segments": [
        {"id": "S0001", "text": "Speaker said a thing."},
    ]}
    content = {"evidence_mode": "text_anchor", "units": [{
        "id": "U0001", "topic": "安全", "importance": "high", "status": "included",
        "claims": ["嘉宾建议自动部署补丁", "其他讨论"], "timestamps": [],
        "evidence": {"segment_ids": ["S0001"]},
        "claim_evidence": {"C01": ["S0001"], "C02": ["S0001"]},
        "claim_evidence_notes": {
            "C01": {"confidence": "high", "rationale": "Transcript explicitly supports claim one."},
            "C02": {"confidence": "high", "rationale": "Transcript explicitly supports claim two."},
        },
    }]}
    content, transcript = enrich_content_map_evidence(content, transcript)
    atomic_write_json(folder / "content_map.json", content)
    atomic_write_json(folder / "transcript.raw.json", transcript)
    (folder / "原始转录.txt").write_text("Speaker said a thing.", encoding="utf-8")
    notes = "嘉宾建议自动部署补丁。其他讨论。"
    briefing = "全局导览。\n\n## 安全讨论\n\n嘉宾建议自动部署补丁。其他讨论。\n"
    summary = {"chapters": [{"title": "安全讨论", "unit_ids": ["U0001"],
                             "claim_ids": ["U0001-C01", "U0001-C02"]}]}
    summary = enrich_summary_map_evidence(summary, notes, content, briefing)
    for name, text in (("中文完整笔记.md", notes), ("讲书稿.md", briefing)):
        (folder / name).write_text(text, encoding="utf-8")
    atomic_write_json(folder / "summary_map.json", summary)


def attribution_plan():
    return {"replacements": [{"file": name, "old": "嘉宾建议自动部署补丁。",
                              "new": "据节目中嘉宾所说，自动化补丁仍涉及验证问题。"}
                             for name in repair.PROSE_FILES],
            "remove_claim_ids": [], "remove_number_ids": [], "remove_example_ids": []}


class ContractRecoveryTests(unittest.TestCase):
    def test_only_targeted_contracts_are_selected_without_mutating_review(self):
        for kind in ("safety_cross_check", "source_document", "firsthand_attribution"):
            original = diagnostic(kind)
            frozen = copy.deepcopy(original)
            self.assertEqual(repair.contract_targets(original)[0]["repair_reason"], kind)
            self.assertEqual(original, frozen)
        ordinary = diagnostic()
        ordinary["original_output"]["fact_checks"][0].update(
            assertion_type="fact", claim_origin="external_source")
        self.assertEqual(repair.contract_targets(ordinary), [])

    def test_contract_exception_enters_repair_then_independent_review(self):
        with tempfile.TemporaryDirectory() as td:
            exc = ai_review.ReviewContractError("contract", diagnostic())
            reviewer = Mock(side_effect=[exc, {"passed": True}])
            with patch.object(repair, "repair_contract_failure", return_value={"action": "repair"}):
                result = review_repair.review_and_repair(td, reviewer=reviewer)
            self.assertTrue(result["passed"])
            self.assertEqual(reviewer.call_count, 2)
            history = json.loads((Path(td) / "review_repair.json").read_text())["history"]
            self.assertFalse(history[0]["passed"])
            self.assertEqual(history[0]["repair_actions"], [{"action": "repair"}])

    def test_contract_recovery_shares_round_limit_and_rejects_stale_pass(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            atomic_write_json(folder / "ai_review.json", {"passed": True})
            exc = ai_review.ReviewContractError("contract", diagnostic())
            reviewer = Mock(side_effect=exc)
            with patch.object(repair, "repair_contract_failure", return_value={}) as fix:
                with self.assertRaises(ai_review.ReviewContractError):
                    review_repair.review_and_repair(folder, reviewer=reviewer, max_rounds=2)
            self.assertEqual(reviewer.call_count, 3)
            self.assertEqual(fix.call_count, 2)
            result = json.loads((folder / "review_repair.json").read_text())
            self.assertFalse(result["passed"])
            self.assertEqual(len(result["history"]), 3)
            self.assertTrue(json.loads((folder / "ai_review.json").read_text())["passed"])

    def test_source_lookup_uses_search_and_rejects_unfetchable_or_fake_quotes(self):
        source = {"parent_claim_id": "U0001-C01", "purpose": "safety_cross_check",
                  "url": "https://example.org/guidance", "quote": "Always validate every patch.",
                  "explanation": "Validation guidance, not a proof of performance."}
        for fetched in (OSError("not public"), {"excerpt": "unrelated", "content_sha256": "x"}):
            with patch.object(repair, "run_json_task", return_value={
                "payload": {"sources": [source]}
            }) as runner, patch.object(repair, "_fetch_source", side_effect=[fetched]):
                accepted, rejected = repair.lookup_sources(Path("/tmp"), repair.contract_targets(
                    diagnostic()))
            self.assertEqual(accepted, [])
            self.assertEqual(len(rejected), 1)
            self.assertTrue(runner.call_args.kwargs["enable_search"])
        with patch.object(repair, "run_json_task", return_value={
            "payload": {"sources": [source]}
        }), \
                patch.object(repair, "_fetch_source", return_value={
                    "excerpt": source["quote"], "content_sha256": "hash"}):
            accepted, rejected = repair.lookup_sources(Path("/tmp"), repair.contract_targets(
                diagnostic()))
        self.assertEqual(accepted[0]["content_sha256"], "hash")
        self.assertFalse(rejected)

    def test_no_source_softens_both_prose_files_and_refreshes_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            raw = (folder / "原始转录.txt").read_bytes()
            transcript = (folder / "transcript.raw.json").read_bytes()
            with patch.object(repair, "lookup_sources", return_value=([], [])), \
                    patch.object(repair, "run_json_task", return_value={
                        "payload": attribution_plan()}), \
                    patch("scripts.content_finalizer.structure_report", return_value=[]):
                action = repair.repair_contract_failure(folder, diagnostic())
            self.assertFalse(action["sources"])
            summary = json.loads((folder / "summary_map.json").read_text())
            notes = (folder / "中文完整笔记.md").read_text()
            self.assertIn("据节目中嘉宾所说", notes)
            self.assertIn("据节目中嘉宾所说", (folder / "讲书稿.md").read_text())
            self.assertEqual(summary["notes_sha256"], body_sha256(notes))
            self.assertIn("writing_inputs", summary)
            self.assertEqual((folder / "原始转录.txt").read_bytes(), raw)
            self.assertEqual((folder / "transcript.raw.json").read_bytes(), transcript)
            self.assertFalse((folder / "ai_review.json").exists())

    def test_real_source_is_attached_as_reviewed_non_authoritative_correction(self):
        source = {"parent_claim_id": "U0001-C01", "purpose": "safety_cross_check",
                  "url": "https://example.org/guidance", "quote": "Always validate every patch.",
                  "explanation": "Validation guidance.", "content_sha256": "hash"}
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            with patch.object(repair, "lookup_sources", return_value=([source], [])), \
                    patch.object(repair, "run_json_task", return_value={
                        "payload": attribution_plan()}), \
                    patch("scripts.content_finalizer.structure_report", return_value=[]):
                repair.repair_contract_failure(folder, diagnostic())
            ledger = json.loads((folder / "editorial_corrections.json").read_text())
            self.assertEqual(ledger["corrections"][0]["source_urls"], [source["url"]])
            self.assertEqual(ledger["corrections"][0]["verdict"], "uncertain")
            self.assertIn("editorial_corrections.json", ai_review.reviewed_hashes(folder))

    def test_invalid_plan_and_finalization_failure_leave_originals_untouched(self):
        for failure in ("unknown_claim", "overlap", "finalization"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                folder = Path(td)
                package(folder)
                before = {p.name: p.read_bytes() for p in folder.iterdir()}
                plan = attribution_plan()
                if failure == "unknown_claim":
                    plan["remove_claim_ids"] = ["U9999-C01"]
                elif failure == "overlap":
                    plan["replacements"].append(copy.deepcopy(plan["replacements"][0]))
                with patch.object(repair, "lookup_sources", return_value=([], [])), \
                        patch.object(repair, "run_json_task", return_value={"payload": plan}), \
                        patch.object(review_repair, "_refresh_semantic_bindings",
                                     side_effect=RuntimeError("finalization failed")):
                    with self.assertRaises((ValueError, RuntimeError)):
                        repair.repair_contract_failure(folder, diagnostic())
                self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_lookup_failure_still_attempts_safe_prose_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            with patch.object(repair, "lookup_sources", side_effect=RuntimeError("offline")), \
                    patch.object(repair, "run_json_task", return_value={
                        "payload": attribution_plan()}), \
                    patch("scripts.content_finalizer.structure_report", return_value=[]):
                action = repair.repair_contract_failure(folder, diagnostic())
            self.assertEqual(action["rejected_sources"], [{"error": "offline"}])

    def test_claim_deletion_reindexes_claim_number_maps_ledger_and_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            content = json.loads((folder / "content_map.json").read_text())
            unit = content["units"][0]
            unit.update(numbers=["unsafe number", "remaining number"], examples=["example"],
                        number_items=[{"id": "N01", "text": "unsafe number"},
                                      {"id": "N02", "text": "remaining number"}],
                        claim_modalities=["recommendation", "general_claim"])
            atomic_write_json(folder / "content_map.json", content)
            summary = json.loads((folder / "summary_map.json").read_text())
            summary["notes_number_ids"] = ["U0001-N01", "U0001-N02"]
            summary["chapters"][0]["number_ids"] = ["U0001-N01", "U0001-N02"]
            atomic_write_json(folder / "summary_map.json", summary)
            atomic_write_json(folder / "editorial_fact_checks.json", {
                "claims": [{"parent_claim_id": f"U0001-C{i:02d}", "source_claim": text,
                            "checks": [{"subclaim_id": f"U0001-C{i:02d}-F01"}]}
                           for i, text in enumerate(unit["claims"], 1)],
                "issue_inventory": [{"parent_claim_id": "U0001-C01"}], "summary": {},
            })
            evidence = copy.deepcopy(unit["evidence"])
            repair.remove_claims(folder, {"U0001-C01"}, ["U0001-N01"])
            unit = json.loads((folder / "content_map.json").read_text())["units"][0]
            summary = json.loads((folder / "summary_map.json").read_text())
            ledger = json.loads((folder / "editorial_fact_checks.json").read_text())
            self.assertEqual(unit["claims"], ["其他讨论"])
            self.assertEqual(unit["claim_evidence"], {"C01": ["S0001"]})
            self.assertEqual(unit["evidence"], evidence)
            self.assertEqual(unit["numbers"], ["remaining number"])
            self.assertEqual(summary["notes_number_ids"], ["U0001-N01"])
            self.assertEqual(summary["chapters"][0]["claim_ids"], ["U0001-C01"])
            self.assertEqual(ledger["claims"][0]["checks"][0]["subclaim_id"], "U0001-C01-F01")
            self.assertEqual(ledger["issue_inventory"], [])
            self.assertEqual(ledger["summary"]["claim_count"], 1)

    def test_softening_can_drop_target_unit_numbers_without_removing_claims(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            content = json.loads((folder / "content_map.json").read_text())
            unit = content["units"][0]
            unit.update(numbers=["900 mg", "remaining number"],
                        number_items=[{"id": "N01", "text": "900 mg"},
                                      {"id": "N02", "text": "remaining number"}])
            atomic_write_json(folder / "content_map.json", content)
            summary = json.loads((folder / "summary_map.json").read_text())
            summary["notes_number_ids"] = ["U0001-N01", "U0001-N02"]
            summary["chapters"][0]["number_ids"] = ["U0001-N01", "U0001-N02"]
            atomic_write_json(folder / "summary_map.json", summary)
            removed = repair.remove_claims(folder, set(), ["U0001-N01"])
            unit = json.loads((folder / "content_map.json").read_text())["units"][0]
            summary = json.loads((folder / "summary_map.json").read_text())
            self.assertEqual(removed, [])
            self.assertEqual(unit["claims"], ["嘉宾建议自动部署补丁", "其他讨论"])
            self.assertEqual(unit["numbers"], ["remaining number"])
            self.assertEqual(summary["notes_number_ids"], ["U0001-N01"])
            self.assertEqual(summary["chapters"][0]["claim_ids"], ["U0001-C01", "U0001-C02"])

    def test_plan_may_not_drop_numbers_outside_target_units(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            plan = attribution_plan()
            plan["remove_number_ids"] = ["U0002-N01"]
            with patch.object(repair, "lookup_sources", return_value=([], [])), \
                    patch.object(repair, "run_json_task", return_value={"payload": plan}):
                with self.assertRaisesRegex(ValueError, "非目标数字"):
                    repair.repair_contract_failure(folder, diagnostic())

    def test_unsourced_claim_can_be_removed_transactionally_before_re_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            plan = attribution_plan()
            for replacement in plan["replacements"]:
                replacement["new"] = ""
            plan["remove_claim_ids"] = ["U0001-C01"]
            with patch.object(repair, "lookup_sources", return_value=([], [])), \
                    patch.object(repair, "run_json_task", return_value={"payload": plan}), \
                    patch("scripts.content_finalizer.structure_report", return_value=[]):
                action = repair.repair_contract_failure(folder, diagnostic())
            content = json.loads((folder / "content_map.json").read_text())
            summary = json.loads((folder / "summary_map.json").read_text())
            self.assertEqual(content["units"][0]["claims"], ["其他讨论"])
            self.assertEqual(summary["notes_claim_ids"], ["U0001-C01"])
            for name in repair.PROSE_FILES:
                self.assertNotIn("嘉宾建议自动部署补丁", (folder / name).read_text())
            self.assertEqual(action["removed_claims"][0]["claim_id"], "U0001-C01")

    def test_empty_unit_keeps_source_accountability_but_drops_public_claim_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            package(folder)
            repair.remove_claims(folder, {"U0001-C01", "U0001-C02"})
            content = json.loads((folder / "content_map.json").read_text())
            unit = content["units"][0]
            self.assertEqual(unit["status"], "excluded")
            self.assertEqual(unit["claim_evidence"], {})
            self.assertEqual(unit["evidence"]["segment_ids"], ["S0001"])
            errors, _ = validate_content_map(content, json.loads(
                (folder / "transcript.raw.json").read_text()))
            self.assertFalse(errors, errors)


class SummaryDedupTests(unittest.TestCase):
    def test_highest_overlap_wins_and_dependencies_are_moved_without_loss(self):
        summary = {"chapters": [
            {"title": "First", "unit_ids": ["U0009", "U0001"],
             "claim_ids": ["U0009-C01", "U0001-C01"], "number_ids": ["U0009-N01"]},
            {"title": "Primary", "unit_ids": ["U0009", "U0009"],
             "claim_ids": ["U0009-C02", "U0009-C03"]},
        ]}
        self.assertTrue(deduplicate_summary_units(summary))
        self.assertEqual(summary["chapters"][0]["unit_ids"], ["U0001"])
        primary = summary["chapters"][1]
        self.assertEqual(primary["unit_ids"], ["U0009"])
        self.assertEqual(primary["claim_ids"], ["U0009-C01", "U0009-C02", "U0009-C03"])
        self.assertEqual(primary["number_ids"], ["U0009-N01"])
        self.assertFalse(deduplicate_summary_units(summary))
        self.assertFalse(validate_summary_map(summary))

    def test_first_chapter_wins_tie_in_finalization_and_enrichment(self):
        summary = {"chapters": [
            {"title": "First", "unit_ids": ["U0009"], "claim_ids": ["U0009-C01"]},
            {"title": "Second", "unit_ids": ["U0009", "U0010"],
             "claim_ids": ["U0009-C02", "U0010-C01"]},
        ]}
        briefing = "## First\n\n" + "甲" * 550 + "\n\n## Second\n\n" + "乙" * 550
        _, aligned, changes = finalize_content_artifacts(briefing, summary)
        self.assertIn("deduplicate_summary_units", changes)
        self.assertEqual(aligned["chapters"][1]["unit_ids"], ["U0010"])
        enriched = enrich_summary_map_evidence(copy.deepcopy(summary), "notes", {"units": []})
        self.assertEqual(enriched["chapters"][0]["claim_ids"], ["U0009-C01", "U0009-C02"])
        self.assertFalse(validate_summary_map(enriched))


if __name__ == "__main__":
    unittest.main()
