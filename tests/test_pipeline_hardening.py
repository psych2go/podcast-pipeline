import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import catalog
import claim_evidence
import subagent
from content_map import (
    body_sha256,
    enrich_content_map_evidence,
    enrich_summary_map_evidence,
    validate_content_map,
    validate_summary_map,
)
from validator import integer_to_chinese, normalize_briefing_artifacts


class SubagentRecoveryTests(unittest.TestCase):
    def test_fallback_runner_is_used_after_transient_primary_failure(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {
                    "SUBAGENT_COMMAND": "primary-agent",
                    "SUBAGENT_FALLBACK_COMMAND": "backup-agent",
                    "SUBAGENT_MAX_RETRIES": "0",
                }, clear=False), \
                patch("subagent.shutil.which", side_effect=lambda name: f"/bin/{name}"), \
                patch("subagent.subprocess.run") as run:
            run.side_effect = [
                type("Result", (), {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "502 Bad Gateway",
                })(),
                type("Result", (), {
                    "returncode": 0,
                    "stdout": '{"ok": true}',
                    "stderr": "",
                })(),
            ]
            result = subagent._run(
                Path(td),
                "return JSON",
                task_name="fallback",
            )
        self.assertEqual(result["runner_index"], 1)
        self.assertIn("backup-agent", result["command"])

    def test_claim_evidence_fallback_produces_valid_distinct_mappings(self):
        payload = [{
            "unit_id": "U0001",
            "claims": [
                {"claim_id": "U0001-C01", "text": "first"},
                {"claim_id": "U0001-C02", "text": "second"},
            ],
            "segments": [
                {"id": "S0001", "text": "one"},
                {"id": "S0002", "text": "two"},
            ],
        }]
        mappings = claim_evidence.deterministic_fallback_mappings(payload)
        self.assertEqual(
            [item["segment_ids"] for item in mappings],
            [["S0001"], ["S0002"]],
        )
        self.assertTrue(all(
            item["confidence"] == "medium" for item in mappings))

    def test_claim_evidence_pipeline_recovers_when_runner_is_down(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            transcript = {
                "meta": {
                    "timestamped": True,
                    "evidence_mode": "timestamp",
                },
                "segments": [
                    {
                        "id": "S0001",
                        "start": 0,
                        "end": 10,
                        "text": "first source",
                    },
                    {
                        "id": "S0002",
                        "start": 10,
                        "end": 20,
                        "text": "second source",
                    },
                ],
            }
            content_map = {
                "evidence_mode": "timestamp",
                "units": [{
                    "id": "U0001",
                    "topic": "topic",
                    "claims": ["first claim", "second claim"],
                    "importance": "high",
                    "status": "included",
                    "timestamps": [[0, 20]],
                }],
            }
            content_map, transcript = enrich_content_map_evidence(
                content_map, transcript)
            (folder / "transcript.raw.json").write_text(
                json.dumps(transcript), encoding="utf-8")
            (folder / "content_map.json").write_text(
                json.dumps(content_map), encoding="utf-8")
            with patch(
                    "claim_evidence._run_batch",
                    side_effect=RuntimeError("502 Bad Gateway")):
                metrics = claim_evidence.refine_claim_evidence(
                    folder, concurrency=1)
            recovered = json.loads(
                (folder / "content_map.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["fallback_claim_count"], 2)
        self.assertEqual(
            validate_content_map(recovered, transcript)[0], [])


class ArtifactNormalizationTests(unittest.TestCase):
    def test_integer_conversion_preserves_key_magnitudes(self):
        cases = {
            10: "十",
            15: "十五",
            24: "二十四",
            96: "九十六",
            230: "二百三十",
            1000: "一千",
            1001: "一千零一",
            1500: "一千五百",
            3000: "三千",
            100000: "十万",
            23000000: "二千三百万",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(integer_to_chinese(value), expected)

    def test_dot_claim_ids_are_canonicalized_before_validation(self):
        content_map = {
            "units": [{
                "id": "U0001",
                "claims": ["claim"],
                "importance": "high",
                "status": "included",
            }],
        }
        notes = "完整笔记"
        briefing = "导览内容足够完整。\n\n## 章节\n正文内容。"
        summary = {
            "chapters": [{
                "title": "章节",
                "unit_ids": ["U0001"],
                "claim_ids": ["U0001.C01"],
            }],
        }
        summary = enrich_summary_map_evidence(
            summary, notes, content_map, briefing)
        self.assertEqual(
            summary["chapters"][0]["claim_ids"], ["U0001-C01"])
        self.assertEqual(
            validate_summary_map(
                summary, briefing, content_map, notes),
            [],
        )

    def test_fragment_chapters_merge_with_summary_mapping(self):
        briefing = (
            "这是一个足够长的全局导览，用来说明整集内容和核心问题。"
            "\n\n## 第一章\n" + "甲" * 200
            + "\n\n## 第二章\n" + "乙" * 300
            + "\n\n## 第三章\n" + "丙" * 500
            + "\n\n## 第四章\n" + "丁" * 500
        )
        summary = {
            "chapters": [
                {
                    "title": "第一章",
                    "unit_ids": ["U0001"],
                    "claim_ids": ["U0001-C01"],
                },
                {
                    "title": "第二章",
                    "unit_ids": ["U0002"],
                    "claim_ids": ["U0002-C01"],
                },
                {
                    "title": "第三章",
                    "unit_ids": ["U0003"],
                    "claim_ids": ["U0003-C01"],
                },
                {
                    "title": "第四章",
                    "unit_ids": ["U0004"],
                    "claim_ids": ["U0004-C01"],
                },
            ],
        }
        fixed, fixed_summary, changes = normalize_briefing_artifacts(
            briefing, summary)
        self.assertEqual(fixed.count("\n## "), 3)
        self.assertEqual(
            fixed_summary["chapters"][0]["unit_ids"],
            ["U0001", "U0002"],
        )
        self.assertIn("merged_fragment_chapters", changes)

    def test_numbers_and_known_directive_are_normalized_without_value_drift(self):
        briefing = (
            "节目称 24 英尺、230 比 1、23,000,000 总吨、5% 和 2018 年。"
            "受访者把自主武器指令称为 3009。"
        )
        fixed, _summary, changes = normalize_briefing_artifacts(
            briefing, {"chapters": []})
        self.assertIn("二十四英尺", fixed)
        self.assertIn("二百三十比一", fixed)
        self.assertIn("二千三百万总吨", fixed)
        self.assertIn("百分之五", fixed)
        self.assertIn("二零一八年", fixed)
        self.assertIn("三零零零点零九", fixed)
        self.assertNotRegex(fixed, r"\d")
        self.assertIn("normalized_numbers", changes)
        self.assertIn("normalized_known_terms", changes)

    def test_normalization_refreshes_body_hashes(self):
        briefing = (
            "这是一个足够长的全局导览，用来说明整集内容和核心问题。"
            "\n\n## 章节\n节目称 24 英尺。" + "正文" * 200
        )
        summary = {
            "chapters": [{
                "title": "章节",
                "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
                "body_sha256": "stale",
            }],
        }
        fixed, fixed_summary, _changes = normalize_briefing_artifacts(
            briefing, summary)
        body = fixed.split("## 章节\n", 1)[1]
        self.assertEqual(
            fixed_summary["chapters"][0]["body_sha256"],
            body_sha256(body),
        )


class WranglerIsolationTests(unittest.TestCase):
    def test_wrangler_run_ignores_repository_dotenv_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            site = root / "site"
            site.mkdir()
            (root / ".env").write_text(
                "CLOUDFLARE_API_TOKEN=expired\n", encoding="utf-8")
            with patch.object(catalog, "SITE_DIR", site), \
                    patch.dict(os.environ, {
                        "CLOUDFLARE_API_TOKEN": "expired",
                    }, clear=False), \
                    patch.object(catalog.subprocess, "run") as run:
                run.return_value = type("Result", (), {
                    "returncode": 0,
                    "stdout": "ok",
                    "stderr": "",
                })()
                ok, _output = catalog._run_wrangler(
                    ["npx", "wrangler", "r2", "bucket", "list"])
        self.assertTrue(ok)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["cwd"], site)
        self.assertNotIn("CLOUDFLARE_API_TOKEN", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
