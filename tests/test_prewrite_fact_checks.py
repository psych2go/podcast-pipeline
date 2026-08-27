import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prewrite_fact_checks
import review_repair
from hashing import sha256_file
from quality_report import build_quality_report


class PrewriteFactCheckTests(unittest.TestCase):
    def _folder(self, root):
        folder = Path(root)
        (folder / "原始转录.txt").write_text("source transcript", encoding="utf-8")
        content_map = {
            "schema_version": 3,
            "units": [
                {
                    "id": "U0001",
                    "status": "included",
                    "importance": "high",
                    "claims": ["嘉宾称公司估值为十亿美元。", "这是他的观点。"],
                    "evidence": {"segment_ids": ["S0001"]},
                },
                {
                    "id": "U0002",
                    "status": "excluded",
                    "importance": "low",
                    "claims": [],
                    "evidence": {"segment_ids": ["S0002"]},
                },
            ],
        }
        (folder / "content_map.json").write_text(
            json.dumps(content_map, ensure_ascii=False), encoding="utf-8")
        return folder, content_map

    def _ledger(self, folder, content_map):
        claims = []
        for item in prewrite_fact_checks.claim_inventory(content_map):
            claims.append({
                **item,
                "risk_level": "high",
                "risk_domains": ["financial"],
                "requires_web": False,
                "checks": [{
                    "subclaim_id": item["parent_claim_id"] + "-F01",
                    "statement": item["source_claim"],
                    "claim_origin": "speaker_reported",
                    "assertion_type": "fact",
                    "verification_mode": "web_spot_check",
                    "risk_domain": "financial",
                    "verdict": "qualified",
                    "editorial_correction": "",
                    "source_urls": [],
                    "checked_at": "2026-08-26",
                    "notes": "attributed source claim",
                }],
            })
        return {
            "schema_version": 1,
            "generated_at": "2026-08-26T00:00:00+00:00",
            "content_map_sha256": sha256_file(folder / "content_map.json"),
            "transcript_basis": prewrite_fact_checks._transcript_basis(folder),
            "claims": claims,
            "issue_inventory": [],
            "summary": {
                "claim_count": len(claims),
                "checked_subclaim_count": len(claims),
                "issue_count": 0,
                "exhaustive_inventory_completed": True,
            },
        }

    def test_inventory_covers_every_nonexcluded_source_claim_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            _folder, content_map = self._folder(td)
            inventory = prewrite_fact_checks.claim_inventory(content_map)
        self.assertEqual(
            [item["parent_claim_id"] for item in inventory],
            ["U0001-C01", "U0001-C02"],
        )
        self.assertEqual(inventory[0]["source_claim"], "嘉宾称公司估值为十亿美元。")

    def test_ledger_freshness_binds_content_map_and_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            folder, content_map = self._folder(td)
            ledger = self._ledger(folder, content_map)
            (folder / prewrite_fact_checks.FILENAME).write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(prewrite_fact_checks.validate_ledger(folder), [])
            (folder / "原始转录.txt").write_text("changed", encoding="utf-8")
            errors = prewrite_fact_checks.validate_ledger(folder)
        self.assertTrue(any("转录基准已过期" in error for error in errors))

    def test_unsourced_editorial_correction_is_dropped(self):
        payload = {
            "claims": [{
                "parent_claim_id": "U0001-C01",
                "checks": [{
                    "claim_origin": "speaker_reported",
                    "verification_mode": "web_required",
                    "verdict": "qualified",
                    "editorial_correction": "unsupported rewrite",
                    "source_urls": [],
                    "notes": "model suggestion",
                }],
            }],
        }
        prewrite_fact_checks._sanitize_unsourced_corrections(payload)
        check = payload["claims"][0]["checks"][0]
        self.assertEqual(check["editorial_correction"], "")
        self.assertEqual(check["verdict"], "uncertain")
        self.assertEqual(check["verification_mode"], "transcript_attribution")
        self.assertIn("已由流水线丢弃", check["notes"])

    def test_subclaim_ids_are_pipeline_owned_and_normalized(self):
        payload = {
            "claims": [{
                "parent_claim_id": "U0001-C01",
                "checks": [
                    {"subclaim_id": "U0001-C01-S01"},
                    {"subclaim_id": "anything"},
                ],
            }],
        }
        prewrite_fact_checks._normalize_subclaim_ids(payload)
        self.assertEqual(
            [item["subclaim_id"] for item in payload["claims"][0]["checks"]],
            ["U0001-C01-F01", "U0001-C01-F02"],
        )

    def test_requires_web_claim_must_include_a_source_url(self):
        with tempfile.TemporaryDirectory() as td:
            folder, content_map = self._folder(td)
            ledger = self._ledger(folder, content_map)
            ledger["claims"][0]["requires_web"] = True
            errors = prewrite_fact_checks.validate_ledger(folder, ledger)
        self.assertTrue(any("requires_web" in error for error in errors))

    def test_required_ledger_missing_blocks_quality_report(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "原始转录.txt").write_text("source", encoding="utf-8")
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "web_transcript",
                "meta": {"evidence_mode": "timestamp"},
                "segments": [],
            }), encoding="utf-8")
            (folder / "content_map.json").write_text(json.dumps({
                "schema_version": 3,
                "evidence_mode": "timestamp",
                "prewrite_fact_checks_version": 1,
                "units": [],
            }), encoding="utf-8")
            report = build_quality_report(folder, strict=True)
        codes = [item["code"] for item in report["error_details"]]
        self.assertIn("prewrite_fact_checks_invalid", codes)

    def test_prompt_requires_source_only_map_and_exhaustive_inventory(self):
        with tempfile.TemporaryDirectory() as td:
            folder, content_map = self._folder(td)
            prompt = prewrite_fact_checks._prompt(
                folder,
                prewrite_fact_checks.claim_inventory(content_map),
                sha256_file(folder / "content_map.json"),
                prewrite_fact_checks._transcript_basis(folder),
            )
        self.assertIn("不得把外部纠正写回 content_map", prompt)
        self.assertIn("不得发现第一个高风险问题后停止", prompt)
        self.assertIn("每一条 source claim", prompt)


class ExactEntityRepairTests(unittest.TestCase):
    def test_exact_entity_repair_preserves_raw_evidence_and_source_excerpt(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            raw_text = "It's entitled Our Brain, Ourselves."
            (folder / "原始转录.txt").write_text(raw_text, encoding="utf-8")
            (folder / "transcript.raw.json").write_text(json.dumps({
                "segments": [{"id": "S0001", "text": raw_text}],
            }), encoding="utf-8")
            (folder / "转录_纠错.txt").write_text(raw_text, encoding="utf-8")
            content_map = {
                "units": [{
                    "id": "U0001",
                    "claims": ["书名是 Our Brain, Ourselves。"],
                    "terms": ["Our Brain, Ourselves"],
                    "source_excerpt": raw_text,
                    "evidence": {"segment_ids": ["S0001"], "source_sha256": "x"},
                    "claim_evidence": {"C01": ["S0001"]},
                    "claim_evidence_sha256": {"C01": "x"},
                    "timestamps": [[0, 1]],
                }],
            }
            (folder / "content_map.json").write_text(
                json.dumps(content_map, ensure_ascii=False), encoding="utf-8")
            (folder / "中文完整笔记.md").write_text(
                "Our Brain, Ourselves", encoding="utf-8")
            (folder / "讲书稿.md").write_text(
                "Our Brain, Ourselves", encoding="utf-8")
            issue = {
                "category": "entity_accuracy",
                "repair_kind": "exact_entity",
                "replacement_from": "Our Brain, Ourselves",
                "replacement_to": "Our Brains, Our Selves",
                "allowed_files": [
                    "转录_纠错.txt", "content_map.json", "中文完整笔记.md", "讲书稿.md",
                ],
                "source_urls": ["https://example.com/book"],
            }
            with patch("review_repair._refresh_semantic_bindings"):
                action = review_repair._repair_exact_entities(folder, [issue])

            updated = json.loads(
                (folder / "content_map.json").read_text(encoding="utf-8"))
            self.assertIn("Our Brains, Our Selves", updated["units"][0]["claims"][0])
            self.assertEqual(updated["units"][0]["source_excerpt"], raw_text)
            self.assertEqual((folder / "原始转录.txt").read_text(encoding="utf-8"), raw_text)
            self.assertIn("Our Brains, Our Selves", (
                folder / "转录_纠错.txt").read_text(encoding="utf-8"))
            self.assertEqual(action["action"], "evidence_backed_exact_entity_repair")

    def test_ledger_entity_refresh_does_not_rewrite_urls_or_ids(self):
        payload = {
            "content_map_sha256": "WrongCo-hash",
            "claims": [{
                "parent_claim_id": "U0001-C01",
                "source_claim": "WrongCo announced a product",
                "checks": [{
                    "subclaim_id": "U0001-C01-F01",
                    "statement": "WrongCo announced a product",
                    "source_urls": ["https://example.com/WrongCo"],
                }],
            }],
        }
        updated = review_repair._replace_ledger_entity(
            payload, "WrongCo", "CorrectCo")
        self.assertEqual(updated["content_map_sha256"], "WrongCo-hash")
        self.assertEqual(
            updated["claims"][0]["checks"][0]["source_urls"],
            ["https://example.com/WrongCo"],
        )
        self.assertIn("CorrectCo", updated["claims"][0]["source_claim"])

    def test_medical_sentence_rewrite_is_not_treated_as_exact_entity(self):
        issue = {
            "severity": "high",
            "category": "factuality",
            "repair_kind": "exact_entity",
            "replacement_from": "drug did nothing",
            "replacement_to": "drug partly helped",
            "allowed_files": ["讲书稿.md"],
            "source_urls": ["https://example.com/paper"],
        }
        self.assertFalse(review_repair._is_exact_entity_issue(issue))


if __name__ == "__main__":
    unittest.main()
