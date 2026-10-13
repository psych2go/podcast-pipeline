import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import agent_pipeline
from scripts import catalog_publish as catalog
from scripts import claim_evidence
from scripts import process
from scripts import subagent
from scripts.content_finalizer import (
    ContentFinalizationError,
    finalize_content_artifacts,
    generate_safe_tts_lexicon,
    validate_tts_lexicon_semantics,
    validate_tts_readiness,
)
from scripts.content_map import (
    apply_claim_evidence_mapping,
    body_sha256,
    canonicalize_claim_evidence_order,
    enrich_content_map_evidence,
    enrich_summary_map_evidence,
    validate_content_map,
    validate_summary_map,
)
from scripts.validator import (
    FLOOR_BAND_MAX,
    MIN_CHAPTER_CHARS,
    MAX_CHAPTER_CHARS,
    TARGET_MIN_CHAPTER_CHARS,
    integer_to_chinese,
    normalize_briefing_artifacts,
    structure_report,
)
from scripts import catalog_core, catalog_site
from scripts.quality_report import build_quality_report
from scripts.subagent import SubagentError, _model_fallback_allowed, _runner_failure


class DeterministicPreReviewTests(unittest.TestCase):
    def test_claim_evidence_is_canonicalized_to_transcript_order(self):
        transcript = {
            "segments": [
                {"id": "S0001", "text": "first"},
                {"id": "S0002", "text": "second"},
                {"id": "S0003", "text": "third"},
            ],
        }
        content_map = {
            "schema_version": 3,
            "units": [{
                "id": "U0001",
                "claims": ["claim"],
                "evidence": {
                    "segment_ids": ["S0001", "S0002", "S0003"],
                },
            }],
        }
        mapped, _transcript = apply_claim_evidence_mapping(
            content_map,
            transcript,
            [{
                "claim_id": "U0001-C01",
                "segment_ids": ["S0003", "S0001", "S0002"],
                "primary_segment_ids": ["S0003", "S0002"],
                "context_segment_ids": ["S0001"],
                "confidence": "high",
                "rationale": "All three ordered segments support the claim.",
            }],
        )
        unit = mapped["units"][0]
        self.assertEqual(
            unit["claim_evidence"]["C01"],
            ["S0001", "S0002", "S0003"],
        )
        self.assertEqual(
            unit["claim_evidence_roles"]["C01"]["primary_segment_ids"],
            ["S0002", "S0003"],
        )
        self.assertEqual(
            unit["claim_evidence_roles"]["C01"]["context_segment_ids"],
            ["S0001"],
        )

    def test_canonicalizer_repairs_existing_order_and_hash(self):
        transcript = {
            "segments": [
                {"id": "S0001", "text": "first"},
                {"id": "S0002", "text": "second"},
            ],
        }
        content_map = {"units": [{
            "claim_evidence": {"C01": ["S0002", "S0001"]},
            "claim_evidence_sha256": {"C01": "stale"},
        }]}
        canonicalize_claim_evidence_order(content_map, transcript)
        unit = content_map["units"][0]
        self.assertEqual(unit["claim_evidence"]["C01"], ["S0001", "S0002"])
        self.assertNotEqual(unit["claim_evidence_sha256"]["C01"], "stale")

    def test_canonicalizer_does_not_promote_missing_context_to_primary(self):
        transcript = {
            "segments": [
                {"id": "S0001", "text": "first"},
                {"id": "S0002", "text": "second"},
            ],
        }
        content_map = {"units": [{
            "claim_evidence": {"C01": ["S0002", "S0001"]},
            "claim_evidence_roles": {"C01": {
                "primary_segment_ids": ["S0002"],
                "context_segment_ids": [],
            }},
            "claim_evidence_sha256": {"C01": "stale"},
        }]}
        canonicalize_claim_evidence_order(content_map, transcript)
        role = content_map["units"][0]["claim_evidence_roles"]["C01"]
        self.assertEqual(role["primary_segment_ids"], ["S0002"])
        self.assertEqual(role["context_segment_ids"], [])

    def test_tts_fraction_semantics_and_math_symbols_fail_fast(self):
        self.assertTrue(validate_tts_lexicon_semantics({
            "Spin-3/二 matter": "自旋三分之二物质",
        }))
        self.assertTrue(validate_tts_lexicon_semantics({
            "3/2": "三比二",
        }))
        self.assertTrue(validate_tts_lexicon_semantics({
            "3/2 and 5/4": "二分之三和五比四",
        }))
        self.assertEqual(validate_tts_lexicon_semantics({
            "Spin-3/二 matter": "自旋二分之三物质",
        }), [])
        self.assertTrue(any(
            "难读符号" in issue
            for issue in validate_tts_readiness("由虚 W− 介导", {})
        ))


class SubagentRecoveryTests(unittest.TestCase):
    def test_pre_review_tts_lexicon_repair_is_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            briefing = folder / "讲书稿.md"
            briefing.write_text(
                "血液pH、LinkedIn和A/B测试。", encoding="utf-8")

            def repair(_folder, _task, **kwargs):
                self.assertEqual(
                    [Path(path).name for path in kwargs["allowed_files"]],
                    ["tts_lexicon.json"],
                )
                (folder / "tts_lexicon.json").write_text(json.dumps({
                    "pH": "酸碱度",
                    "LinkedIn": "领英",
                    "A/B": "A B",
                }), encoding="utf-8")

            with patch("scripts.agent_pipeline.run_edit_task", side_effect=repair):
                result = agent_pipeline._ensure_tts_lexicon_ready(
                    folder, briefing)
        self.assertTrue(result["repaired"])
        self.assertEqual(result["entries"], 3)

    def test_pre_review_tts_lexicon_rejects_sentence_scale_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            briefing = folder / "讲书稿.md"
            briefing.write_text(
                "血液pH、LinkedIn和A/B测试需要准确朗读。", encoding="utf-8")
            existing = {"LinkedIn": "领英"}
            (folder / "tts_lexicon.json").write_text(
                json.dumps(existing), encoding="utf-8")

            def repair(_folder, _task, **_kwargs):
                (folder / "tts_lexicon.json").write_text(json.dumps({
                    **existing,
                    "血液pH、LinkedIn和A/B测试需要准确朗读": "完全不同事实",
                }), encoding="utf-8")

            with patch("scripts.agent_pipeline.run_edit_task", side_effect=repair):
                with self.assertRaisesRegex(RuntimeError, "key 范围过大"):
                    agent_pipeline._ensure_tts_lexicon_ready(folder, briefing)
            restored = json.loads(
                (folder / "tts_lexicon.json").read_text(encoding="utf-8"))
        self.assertEqual(restored, existing)

    def test_low_confidence_claim_repair_is_applied_deterministically(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            content_map = folder / "content_map.json"
            content_map.write_text(json.dumps({
                "units": [
                    {
                        "id": "U10000", "topic": "target",
                        "claims": ["supported fact and unsupported addition"],
                        "claim_modalities": ["general_claim"],
                        "evidence": {"segment_ids": ["S0001"]},
                        "claim_evidence": {"C01": ["S0001"]},
                        "claim_evidence_sha256": {"C01": "old"},
                        "claim_evidence_notes": {
                            "C01": {"confidence": "low", "rationale": "too broad"},
                        },
                    },
                    {"id": "U0020", "topic": "untouched", "claims": ["keep"]},
                ],
            }), encoding="utf-8")
            raw = folder / "transcript.raw.json"
            raw.write_text(json.dumps({
                "segments": [{"id": "S0001", "text": "supported fact"}],
            }), encoding="utf-8")
            response = {
                "payload": {"units": [{
                    "unit_id": "U10000",
                    "claims": ["supported fact"],
                    "claim_modalities": ["actual_event"],
                }]},
            }
            error = RuntimeError(
                "U10000-C01: claim evidence confidence=low，需人工复核")
            with patch(
                    "scripts.agent_pipeline.run_json_task",
                    return_value=response) as repair:
                unit_ids = agent_pipeline._repair_low_confidence_claims(
                    folder, raw, error)
            repaired = json.loads(content_map.read_text(encoding="utf-8"))
        self.assertEqual(unit_ids, ["U10000"])
        self.assertEqual(repaired["units"][0]["claims"], ["supported fact"])
        self.assertEqual(
            repaired["units"][0]["claim_modalities"], ["actual_event"])
        self.assertEqual(repaired["units"][0]["claim_evidence"], {})
        self.assertEqual(repaired["units"][0]["claim_evidence_sha256"], {})
        self.assertEqual(repaired["units"][0]["claim_evidence_notes"], {})
        self.assertEqual(repaired["units"][1]["claims"], ["keep"])
        self.assertIn("U10000", repair.call_args.args[1])
        self.assertNotIn("U0020", repair.call_args.args[1])

    def test_output_schema_is_normalized_for_strict_codex_outputs(self):
        schema = {
            "type": "object",
            "properties": {
                "result": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "value": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        }

        normalized = subagent.prepare_output_schema(schema)

        self.assertIsNot(normalized, schema)
        self.assertFalse(normalized["additionalProperties"])
        self.assertEqual(normalized["required"], ["result"])
        result = normalized["properties"]["result"]
        self.assertFalse(result["additionalProperties"])
        self.assertEqual(result["required"], ["ok", "items"])
        item = result["properties"]["items"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(item["required"], ["value"])

    def test_fallback_runner_is_used_after_transient_primary_failure(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {
                    "SUBAGENT_COMMAND": "primary-agent",
                    "SUBAGENT_FALLBACK_COMMAND": "backup-agent",
                    "SUBAGENT_MAX_RETRIES": "0",
                }, clear=False), \
                patch("scripts.subagent.shutil.which", side_effect=lambda name: f"/bin/{name}"), \
                patch("scripts.subagent._run_process") as run:
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

    def test_claim_evidence_strict_mode_blocks_when_runner_is_down(self):
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
            for unit in content_map["units"]:
                unit["claim_evidence"] = {}
                unit["claim_evidence_sha256"] = {}
                unit["claim_evidence_notes"] = {}
            (folder / "transcript.raw.json").write_text(
                json.dumps(transcript), encoding="utf-8")
            (folder / "content_map.json").write_text(
                json.dumps(content_map), encoding="utf-8")
            with patch(
                    "scripts.claim_evidence._run_batch",
                    side_effect=RuntimeError("502 Bad Gateway")):
                with self.assertRaisesRegex(
                        RuntimeError, "strict mode forbids fallback"):
                    claim_evidence.refine_claim_evidence(
                        folder, concurrency=1)

    def test_claim_evidence_explicit_degraded_mode_records_fallback(self):
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
            for unit in content_map["units"]:
                unit["claim_evidence"] = {}
                unit["claim_evidence_sha256"] = {}
                unit["claim_evidence_notes"] = {}
            (folder / "transcript.raw.json").write_text(
                json.dumps(transcript), encoding="utf-8")
            (folder / "content_map.json").write_text(
                json.dumps(content_map), encoding="utf-8")
            with patch(
                    "scripts.claim_evidence._run_batch",
                    side_effect=RuntimeError("502 Bad Gateway")):
                metrics = claim_evidence.refine_claim_evidence(
                    folder, concurrency=1, allow_fallback=True)
            recovered = json.loads(
                (folder / "content_map.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["fallback_claim_count"], 2)
        self.assertEqual(
            validate_content_map(recovered, transcript)[0], [])

    def test_claim_evidence_string_retry_labels_are_supported(self):
        self.assertEqual(claim_evidence._batch_label(2), "002")
        self.assertEqual(
            claim_evidence._batch_label("2_1"), "2_1")

    def test_claim_evidence_low_confidence_retries_single_unit(self):
        batch = [{
            "unit_id": "U0001",
            "claims": [{"claim_id": "U0001-C01", "text": "claim"}],
            "segments": [{"id": "S0001", "text": "source"}],
        }]
        low = [{
            "claim_id": "U0001-C01",
            "segment_ids": ["S0001"],
            "confidence": "low",
            "rationale": "首次证据不足，需要按单元重新核对。",
        }]
        high = [{
            "claim_id": "U0001-C01",
            "segment_ids": ["S0001"],
            "confidence": "high",
            "rationale": "单元复核确认原文直接支持该主张。",
        }]
        with patch(
                "scripts.claim_evidence._run_batch",
                return_value=({"claims": high}, {"duration_ms": 1})) as run:
            mappings, wrappers = (
                claim_evidence._retry_low_confidence_units(
                    Path("/tmp"), batch, low, "model", 2))
        self.assertEqual(mappings, high)
        self.assertEqual(len(wrappers), 1)
        self.assertEqual(run.call_args.args[3], "max")
        self.assertEqual(run.call_args.args[4], "2_low_1")

    def test_claim_evidence_strict_mode_retries_failed_batch_per_unit(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            transcript = {
                "meta": {
                    "timestamped": True,
                    "evidence_mode": "timestamp",
                },
                "segments": [
                    {"id": "S0001", "start": 0, "end": 5,
                     "text": "first source A"},
                    {"id": "S0002", "start": 5, "end": 10,
                     "text": "first source B"},
                    {"id": "S0003", "start": 10, "end": 15,
                     "text": "second source A"},
                    {"id": "S0004", "start": 15, "end": 20,
                     "text": "second source B"},
                ],
            }
            content_map = {
                "schema_version": 3,
                "evidence_mode": "timestamp",
                "units": [
                    {
                        "id": "U0001", "topic": "one",
                        "claims": ["first claim A", "first claim B"],
                        "importance": "high", "status": "included",
                        "timestamps": [[0, 10]],
                    },
                    {
                        "id": "U0002", "topic": "two",
                        "claims": ["second claim A", "second claim B"],
                        "importance": "high", "status": "included",
                        "timestamps": [[10, 20]],
                    },
                ],
            }
            content_map, transcript = enrich_content_map_evidence(
                content_map, transcript)
            (folder / "transcript.raw.json").write_text(
                json.dumps(transcript), encoding="utf-8")
            (folder / "content_map.json").write_text(
                json.dumps(content_map), encoding="utf-8")

            def run_batch(_folder, batch, _model, _effort, batch_index):
                if len(batch) > 1:
                    raise RuntimeError("batch too large")
                item = batch[0]
                mappings = []
                for index, claim in enumerate(item["claims"]):
                    mappings.append({
                        "claim_id": claim["claim_id"],
                        "segment_ids": [item["segments"][index]["id"]],
                        "confidence": "high",
                        "rationale": "单个单元重试后直接支持该主张。",
                    })
                return ({"claims": mappings}, {
                    "retry_count": 0,
                    "duration_ms": 1,
                    "task_name": str(batch_index),
                })

            with patch("scripts.claim_evidence._run_batch", side_effect=run_batch):
                metrics = claim_evidence.refine_claim_evidence(
                    folder,
                    concurrency=1,
                    max_batch_chars=100000,
                )

        self.assertEqual(metrics["claim_count"], 4)
        self.assertEqual(metrics["fallback_claim_count"], 0)
        self.assertEqual(metrics["recovered_unit_count"], 2)


class ArtifactNormalizationTests(unittest.TestCase):
    def test_finalizer_aligns_summary_titles_after_number_normalization(self):
        briefing = (
            "导览内容足够长，用来说明全篇结构和核心问题。\n\n"
            "## 第一章 45岁后的评估\n\n"
            "正文内容。"
        )
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "完全不同的标题",
                "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
            }],
        }

        finalized, aligned, changes = finalize_content_artifacts(
            briefing, summary)

        self.assertIn("四十五岁", finalized)
        self.assertEqual(
            aligned["chapters"][0]["title"],
            "第一章四十五岁后的评估",
        )
        self.assertIn("normalized_numbers", changes)

    def test_finalizer_preserves_mixed_alphanumeric_brand_names(self):
        briefing = (
            "导览内容足够长，用来说明全篇结构和核心问题。\n\n"
            "## a16z 与 GPT-4 的四十五岁测试\n\n"
            "a16z 讨论 GPT-4，也比较 45 岁群体。"
        )
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "placeholder",
                "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
            }],
        }

        finalized, aligned, changes = finalize_content_artifacts(
            briefing, summary)

        self.assertIn("a16z", finalized)
        self.assertIn("GPT-4", finalized)
        self.assertIn("四十五岁", finalized)
        self.assertEqual(
            aligned["chapters"][0]["title"],
            "a16z 与 GPT-4 的四十五岁测试",
        )
        self.assertIn("normalized_numbers", changes)

    def test_finalizer_merges_trailing_conclusion_mapping(self):
        briefing = (
            "导览内容足够长，用来说明全篇结构和核心问题。\n\n"
            "## 第一章\n\n正文一。\n\n"
            "## 第二章\n\n正文二。"
        )
        summary = {
            "schema_version": 2,
            "chapters": [
                {"title": "A", "unit_ids": ["U0001"],
                 "claim_ids": ["U0001-C01"]},
                {"title": "B", "unit_ids": ["U0002"],
                 "claim_ids": ["U0002-C01"]},
                {"title": "结语：回到主线", "unit_ids": ["U0003"],
                 "claim_ids": ["U0003-C01"]},
            ],
        }

        _briefing, aligned, _changes = finalize_content_artifacts(
            briefing, summary)

        self.assertEqual(len(aligned["chapters"]), 2)
        self.assertEqual(
            aligned["chapters"][1]["unit_ids"],
            ["U0002", "U0003"],
        )
        self.assertEqual(aligned["chapters"][1]["title"], "第二章")

    def test_safe_tts_lexicon_adds_only_exact_abbreviations(self):
        lexicon = generate_safe_tts_lexicon(
            "AI 使用 GPU，也讨论 ImageNet 和 ESPN+。",
            {"AI": "人工智能"},
        )

        self.assertEqual(lexicon["AI"], "人工智能")
        self.assertEqual(lexicon["GPU"], "G P U")
        self.assertEqual(lexicon["ESPN+"], "E S P N 加")
        self.assertNotIn("ImageNet", lexicon)

    def test_finalizer_splits_long_chapter_at_paragraph_and_unit_boundary(self):
        briefing = (
            "这是一个足够长的全局导览，用来说明全篇结构和核心问题。\n\n"
            "## 关键转折\n\n"
            + "甲" * 520
            + "\n\n"
            + "乙" * 520
        )
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "关键转折",
                "unit_ids": ["U0001", "U0002"],
                "claim_ids": ["U0001-C01", "U0002-C01"],
            }],
        }

        finalized, aligned, changes = finalize_content_artifacts(
            briefing, summary)

        self.assertIn("## 关键转折：上篇", finalized)
        self.assertIn("## 关键转折：下篇", finalized)
        self.assertEqual(
            [chapter["unit_ids"] for chapter in aligned["chapters"]],
            [["U0001"], ["U0002"]],
        )
        self.assertEqual(
            [chapter["claim_ids"] for chapter in aligned["chapters"]],
            [["U0001-C01"], ["U0002-C01"]],
        )
        self.assertIn("split_long_chapters", changes)
        self.assertTrue(all(
            chapter.get("body_sha256")
            for chapter in aligned["chapters"]
        ))

    def test_finalizer_blocks_unsafe_long_chapter_split(self):
        briefing = (
            "这是一个足够长的全局导览，用来说明全篇结构和核心问题。\n\n"
            "## 单一证据链\n\n"
            + "甲" * 1100
        )
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "单一证据链",
                "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
            }],
        }

        with self.assertRaisesRegex(
                ContentFinalizationError, "无法安全拆分超长章节"):
            finalize_content_artifacts(briefing, summary)

    def test_tts_readiness_validates_actual_post_lexicon_text(self):
        issues = validate_tts_readiness(
            "GPU 架构 architecture，版本 2/3，ImageNet+。",
            {"GPU": "G P U", "architecture": "架构"},
        )

        self.assertTrue(any("阿拉伯数字" in issue for issue in issues))
        self.assertTrue(any("难读符号" in issue for issue in issues))
        self.assertTrue(any("重复表达" in issue for issue in issues))
        self.assertTrue(any("英文串" in issue for issue in issues))

    def test_tts_readiness_accepts_safely_normalized_input(self):
        issues = validate_tts_readiness(
            "AI 使用 GPU 训练模型。",
            {"AI": "人工智能", "GPU": "G P U"},
        )

        self.assertEqual(issues, [])

    def test_tts_and_html_steps_do_not_mutate_review_bound_content(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            briefing, summary, _changes = finalize_content_artifacts(
                "这是一个足够长的全局导览，用来说明全篇结构、证据范围和核心问题。"
                "\n\n## 核心章节\n\n" + "正文内容" * 120,
                {
                    "schema_version": 2,
                    "chapters": [{
                        "title": "核心章节",
                        "unit_ids": ["U0001"],
                        "claim_ids": ["U0001-C01"],
                    }],
                },
            )
            briefing_path = folder / "讲书稿.md"
            summary_path = folder / "summary_map.json"
            briefing_path.write_text(briefing, encoding="utf-8")
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            before_briefing = briefing_path.read_bytes()
            before_summary = summary_path.read_bytes()
            tts_result = type("TTSResult", (), {
                "ok": True,
                "summary": "ok",
                "__str__": lambda self: "ok",
            })()

            with patch("scripts.process.validate_for_stage"), \
                    patch("scripts.process._run_quality_gate", return_value=True), \
                    patch("scripts.process.run_tts", return_value=tts_result), \
                    patch("scripts.process.prepare_release", return_value={}), \
                    patch("scripts.process.md_to_html", return_value=folder / "out.html"):
                self.assertTrue(process.run_tts_step(
                    folder, "Episode", "讲书稿.md", 1.0, False, True,
                ))
                self.assertTrue(process.run_html_step(
                    folder, "Episode", "讲书稿.md",
                ))

            self.assertEqual(briefing_path.read_bytes(), before_briefing)
            self.assertEqual(summary_path.read_bytes(), before_summary)

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
            "notes_claim_ids": ["U0001.C01"],
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

    def test_normalizer_preserves_numbered_titles_and_hyphenated_brands(self):
        briefing = (
            "20,000 Leagues Under the Sea、Fantasia 2000 与 7-Eleven，"
            "相关资料发布于 2018 年。"
        )
        fixed, _summary, changes = normalize_briefing_artifacts(
            briefing, {"chapters": []})
        self.assertIn("20,000 Leagues Under the Sea", fixed)
        self.assertIn("Fantasia 2000", fixed)
        self.assertIn("7-Eleven", fixed)
        self.assertIn("二零一八年", fixed)
        self.assertIn("normalized_numbers", changes)

    def test_numbers_are_normalized_without_episode_specific_fact_rewrite(self):
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
        self.assertIn("三千零九", fixed)
        self.assertNotIn("正式编号为", fixed)
        self.assertNotRegex(fixed, r"\d")
        self.assertIn("normalized_numbers", changes)
        self.assertNotIn("normalized_known_terms", changes)

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



class ChapterDepthGateTests(unittest.TestCase):
    def _briefing(self, sizes, preamble=None):
        preamble = preamble or (
            "这是一个足够长的全局导览，用来说明本期主线、争议焦点和收听价值。"
        )
        parts = [preamble]
        for index, size in enumerate(sizes, start=1):
            parts.append(f"## 章节{index}\n\n" + ("论" * size))
        return "\n\n".join(parts)

    def test_floor_hugging_chapters_fail_structure_and_strict_quality(self):
        # Majority in [400, 480], mean also below 520.
        briefing = self._briefing([420, 430, 410, 440])
        warnings = structure_report(briefing)
        self.assertTrue(any("贴地板" in w for w in warnings))
        self.assertTrue(any("章均过浅" in w for w in warnings))

        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text(briefing, encoding="utf-8")
            report = build_quality_report(folder, strict=True)
            joined = "\n".join(report.get("errors", []))
            self.assertIn("贴地板", joined)
            self.assertFalse(report.get("passed", True))

    def test_healthy_mean_chapters_pass_depth_gate(self):
        briefing = self._briefing([560, 600, 620, 580])
        warnings = structure_report(briefing)
        self.assertFalse(any("贴地板" in w for w in warnings))
        self.assertFalse(any("章均过浅" in w for w in warnings))
        self.assertGreaterEqual(
            sum([560, 600, 620, 580]) / 4, TARGET_MIN_CHAPTER_CHARS)

        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text(briefing, encoding="utf-8")
            report = build_quality_report(folder, strict=True)
            depth_errors = [
                error for error in report.get("errors", [])
                if "贴地板" in error or "章均过浅" in error
            ]
            self.assertEqual(depth_errors, [])

    def test_fragment_below_min_still_warned(self):
        briefing = self._briefing([200, 600, 600])
        warnings = structure_report(briefing)
        self.assertTrue(any("碎片章" in w for w in warnings))
        self.assertTrue(any(f"< {MIN_CHAPTER_CHARS}" in w for w in warnings))

    def test_fragment_merge_threshold_unchanged(self):
        # normalize_briefing_artifacts only merges while chapter count > 3.
        briefing = (
            "这是一个足够长的全局导览，用来说明整集内容和核心问题。"
            "\n\n## 短章\n\n" + ("短" * 120)
            + "\n\n## 邻章\n\n" + ("长" * 500)
            + "\n\n## 中章\n\n" + ("中" * 500)
            + "\n\n## 尾章\n\n" + ("尾" * 500)
        )
        summary = {
            "chapters": [
                {"title": "短章", "unit_ids": ["U0001"], "claim_ids": ["U0001-C01"]},
                {"title": "邻章", "unit_ids": ["U0002"], "claim_ids": ["U0002-C01"]},
                {"title": "中章", "unit_ids": ["U0003"], "claim_ids": ["U0003-C01"]},
                {"title": "尾章", "unit_ids": ["U0004"], "claim_ids": ["U0004-C01"]},
            ],
        }
        fixed, _summary, changes = normalize_briefing_artifacts(briefing, summary)
        self.assertIn("merged_fragment_chapters", changes)
        # Fragment merges into the left neighbor title; the short body is gone
        # as a standalone chapter heading pair with only 120 chars.
        self.assertNotIn("## 邻章", fixed)
        self.assertEqual(fixed.count("\n## "), 3)

    def test_finalizer_split_still_respects_min_max(self):
        self.assertEqual(MIN_CHAPTER_CHARS, 400)
        self.assertEqual(MAX_CHAPTER_CHARS, 1000)
        self.assertEqual(TARGET_MIN_CHAPTER_CHARS, 520)
        self.assertEqual(FLOOR_BAND_MAX, 480)
        briefing = (
            "这是一个足够长的全局导览，用来说明全篇结构和核心问题。\n\n"
            "## 长转折\n\n"
            + ("甲" * 520)
            + "\n\n"
            + ("乙" * 520)
        )
        summary = {
            "schema_version": 2,
            "chapters": [{
                "title": "长转折",
                "unit_ids": ["U0001", "U0002"],
                "claim_ids": ["U0001-C01", "U0002-C01"],
            }],
        }
        finalized, aligned, changes = finalize_content_artifacts(
            briefing, summary)
        self.assertIn("split_long_chapters", changes)
        bodies = [
            chapter.split("\n\n", 1)[1]
            for chapter in finalized.split("\n\n## ")[1:]
        ]
        # After split each side should stay within the merge/split band.
        from scripts.validator import _zh_chars
        for body in bodies:
            n = _zh_chars(body)
            self.assertGreaterEqual(n, MIN_CHAPTER_CHARS)
            self.assertLessEqual(n, MAX_CHAPTER_CHARS)


class FinishIsolationTests(unittest.TestCase):
    def _episode(self, content, name, *, briefing=True, mp3=False, html=False):
        folder = content / name
        folder.mkdir(parents=True, exist_ok=True)
        if briefing:
            (folder / "讲书稿.md").write_text("正文内容", encoding="utf-8")
        if mp3:
            (folder / f"{name}.mp3").write_bytes(b"x" * 2048)
        if html:
            (folder / f"{name} - content.html").write_text(
                "<html></html>", encoding="utf-8")
        return folder

    def test_sync_only_ignores_wip_sibling_without_mp3(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content = root / "content"
            site = root / "site"
            site.mkdir(parents=True)
            self._episode(content, "Ready", mp3=True, html=True)
            self._episode(content, "WIP", mp3=False, html=False)
            (site / "site.json").write_text("[]", encoding="utf-8")
            (content / "Ready" / "content_map.json").write_text(
                "{}", encoding="utf-8")

            with patch.object(catalog_core, "CONTENT_DIR", content), \
                    patch.object(catalog_core, "SITE_DIR", site), \
                    patch.object(catalog_site, "CONTENT_DIR", content), \
                    patch.object(catalog_site, "SITE_DIR", site), \
                    patch(
                        "scripts.quality_report.build_quality_report",
                        return_value={"passed": True, "errors": []},
                    ), \
                    patch.object(
                        catalog_site, "episode_stats",
                        return_value={"chars": 1000, "duration": 10},
                    ), \
                    patch.object(
                        catalog_core, "episode_stats",
                        return_value={"chars": 1000, "duration": 10},
                    ):
                names = catalog_core._ordered_episode_names(include={"Ready"})
                self.assertEqual(names, ["Ready"])
                self.assertNotIn(
                    "WIP", catalog_core._ordered_episode_names())
                errors = catalog_site._site_readiness_errors(
                    ["Ready", "WIP"], existing={}, strict_names={"Ready"})
                self.assertFalse(any("WIP" in error for error in errors))
                catalog_site.sync_site(only="Ready")
                entries = catalog_core._load_site_entries()
                self.assertEqual(
                    [entry["folder"] for entry in entries], ["Ready"])

    def test_sync_still_fails_when_published_sibling_broken(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content = root / "content"
            site = root / "site"
            site.mkdir(parents=True)
            self._episode(content, "LiveBroken", mp3=False, html=False)
            self._episode(content, "Ready", mp3=True, html=True)
            (content / "Ready" / "content_map.json").write_text(
                "{}", encoding="utf-8")
            (site / "site.json").write_text(
                json.dumps([{"folder": "LiveBroken", "duration": 1, "words": 1}]),
                encoding="utf-8",
            )
            with patch.object(catalog_core, "CONTENT_DIR", content), \
                    patch.object(catalog_core, "SITE_DIR", site), \
                    patch.object(catalog_site, "CONTENT_DIR", content), \
                    patch.object(catalog_site, "SITE_DIR", site), \
                    patch(
                        "scripts.quality_report.build_quality_report",
                        return_value={"passed": True, "errors": []},
                    ):
                with self.assertRaises(SystemExit) as ctx:
                    catalog_site.sync_site(only="Ready")
                self.assertIn("未就绪", str(ctx.exception))
                errors = catalog_site._site_readiness_errors(
                    catalog_core._ordered_episode_names(include={"Ready"}),
                    existing={"LiveBroken": {}},
                    strict_names={"Ready"},
                )
                self.assertTrue(any("LiveBroken" in error for error in errors))
                # site.json must remain unchanged (sync aborted)
                entries = json.loads(
                    (site / "site.json").read_text(encoding="utf-8"))
                self.assertEqual(entries[0]["folder"], "LiveBroken")


class BackendFailoverTests(unittest.TestCase):
    def test_billing_quota_errors_allow_model_fallback(self):
        kind, status = _runner_failure(
            'ERROR: HTTP 429: {"code":"1113"} 余额不足', "")
        self.assertEqual(kind, "billing")
        self.assertEqual(status, 429)
        self.assertTrue(_model_fallback_allowed(
            SubagentError("x", metrics={"failure_kind": "billing"})))
        self.assertTrue(_model_fallback_allowed(
            SubagentError("x", metrics={"failure_kind": "rate_limit"})))



class WranglerIsolationTests(unittest.TestCase):
    def test_wrangler_run_preserves_explicit_environment_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            site = root / "site"
            site.mkdir()
            with patch.object(catalog, "SITE_DIR", site), \
                    patch.dict(os.environ, {
                        "CLOUDFLARE_API_TOKEN": "ci-secret",
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
        self.assertEqual(
            run.call_args.kwargs["env"]["CLOUDFLARE_API_TOKEN"],
            "ci-secret",
        )

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
