import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from content_map import (
    apply_claim_evidence_mapping, body_sha256, coverage_report,
    enrich_content_map_evidence,
    enrich_summary_map_evidence, validate_content_map, validate_summary_map,
)
from diarize import merge_segments_with_speakers
from benchmark import asr_metrics
from quality_report import build_quality_report
from tts import apply_tts_lexicon, run_tts, validate_tts_manifest
import process as pipeline_process
from ai_review import reviewed_hashes
import catalog
from html_gen import _build_html
from publish import verify_publish
from episode import (
    load_episode, stable_slug, update_review_status,
)
from fetcher import (
    _extract_podscripts_segments,
    clean_whisper_hallucinations,
    detect_source_warnings,
    resolve_asr_config,
    chunk_plain_transcript,
    transcribe,
)
from run_report import RunReport


class FetcherTests(unittest.TestCase):
    def test_podscripts_fixture_extracts_only_timestamped_transcript(self):
        fixture = (
            Path(__file__).resolve().parent
            / "fixtures" / "podscripts_sample.html"
        ).read_text(encoding="utf-8")
        segments = _extract_podscripts_segments(fixture)
        self.assertEqual(
            segments,
            [
                {
                    "start": 12,
                    "end": 65,
                    "text": "First transcript sentence.",
                },
                {
                    "start": 65,
                    "end": None,
                    "text": "Second transcript sentence.",
                },
            ],
        )

    def test_explicit_model_wins(self):
        config = resolve_asr_config("balanced", "large-v3-turbo")
        self.assertEqual(config["model_size"], "large-v3-turbo")
        self.assertEqual(resolve_asr_config("fast")["model_size"], "medium")

    def test_cleaner_does_not_remove_real_sponsor_sentence(self):
        text = "This episode is sponsored by Example.\n\nA real point."
        self.assertIn("sponsored by Example", clean_whisper_hallucinations(text))

    def test_source_warnings(self):
        warnings = detect_source_warnings("Editor's Note\nhttps://example.com")
        self.assertIn("contains_urls", warnings)
        self.assertIn("contains_editorial_intro", warnings)

    def test_plain_transcript_is_chunked(self):
        chunks = chunk_plain_transcript("One. Two.\n\nThree. Four.", max_chars=12)
        self.assertEqual(chunks, ["One. Two.", "Three. Four."])

    def test_unified_asr_applies_max_parameters(self):
        word = SimpleNamespace(word=" hello", start=0.0, end=0.5, probability=0.9)
        segment = SimpleNamespace(
            start=0.0, end=0.5, text=" hello", words=[word],
            avg_logprob=-0.1, compression_ratio=1.0,
            no_speech_prob=0.0, temperature=0.0,
        )
        info = SimpleNamespace(language="en", language_probability=0.99)

        class FakeModel:
            def __init__(self):
                self.kwargs = None

            def transcribe(self, _audio, **kwargs):
                self.kwargs = kwargs
                return iter([segment]), info

        fake = FakeModel()
        with patch("fetcher._load_whisper_model", return_value=fake):
            result = transcribe(
                "fake.mp3", quality="max", asr_model="large-v3-turbo",
                diarize_audio=False, return_metadata=True,
            )
        self.assertEqual(result["meta"]["model"], "large-v3-turbo")
        self.assertEqual(fake.kwargs["beam_size"], 12)
        self.assertTrue(fake.kwargs["word_timestamps"])
        self.assertEqual(result["text"], "hello")


class QualityReportTests(unittest.TestCase):
    def test_empty_pending_map_cannot_pass(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "local_asr",
                "segments": [{"start": 0, "end": 1, "text": "hello"}],
                "meta": {},
            }), encoding="utf-8")
            (folder / "content_map.json").write_text(json.dumps({
                "units": [{
                    "id": "U0001", "topic": "", "claims": [],
                    "importance": "medium", "status": "pending",
                    "timestamps": [[0, 1]],
                }]
            }), encoding="utf-8")
            (folder / "summary_map.json").write_text(
                json.dumps({"chapters": []}), encoding="utf-8")
            report = build_quality_report(folder)
            self.assertFalse(report["passed"])

    def test_missing_ai_review_blocks_complete_episode(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            briefing = "导览。\n\n## 章节\n完整正文。"
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "third_party_transcript",
                "segments": [{"start": 0, "end": 1, "text": "source"}],
                "meta": {"transcript_chars": 6},
            }), encoding="utf-8")
            (folder / "content_map.json").write_text(json.dumps({
                "units": [{
                    "id": "U0001", "topic": "主题", "claims": ["事实"],
                    "importance": "high", "status": "included",
                    "timestamps": [[0, 1]],
                }]
            }), encoding="utf-8")
            (folder / "summary_map.json").write_text(json.dumps({
                "chapters": [{
                    "title": "章节",
                    "unit_ids": ["U0001"],
                    "claim_ids": ["U0001-C01"],
                    "body_sha256": body_sha256("完整正文。"),
                }]
            }), encoding="utf-8")
            (folder / "讲书稿.md").write_text(briefing, encoding="utf-8")
            (folder / "中文完整笔记.md").write_text(
                briefing + "\n补充内容补充内容补充内容补充内容。", encoding="utf-8")
            (folder / "来源.md").write_text(
                "- 转录质量：官方字幕\n", encoding="utf-8")
            report = build_quality_report(folder)
            self.assertFalse(report["passed"])
            self.assertIn("缺少 ai_review.json，不能自动发布", report["errors"])

    def test_notes_ratio_is_warning_not_publish_gate(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            briefing = "导览内容足够长。\n\n## 章节\n这是一段精编正文。"
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "third_party_transcript",
                "segments": [{"start": 0, "end": 1, "text": "source"}],
                "meta": {},
            }), encoding="utf-8")
            (folder / "content_map.json").write_text(json.dumps({
                "units": [{
                    "id": "U0001", "topic": "主题", "claims": ["事实"],
                    "importance": "high", "status": "included",
                    "timestamps": [[0, 1]],
                }]
            }), encoding="utf-8")
            (folder / "summary_map.json").write_text(json.dumps({
                "chapters": [{
                    "title": "章节", "unit_ids": ["U0001"],
                    "claim_ids": ["U0001-C01"],
                    "body_sha256": body_sha256("这是一段精编正文。"),
                }]
            }), encoding="utf-8")
            (folder / "讲书稿.md").write_text(briefing, encoding="utf-8")
            (folder / "中文完整笔记.md").write_text(briefing, encoding="utf-8")
            (folder / "来源.md").write_text(
                "- 转录质量：官方字幕\n", encoding="utf-8")
            report = build_quality_report(folder)
            self.assertTrue(any(
                "字数接近" in warning for warning in report["warnings"]))

    def test_corrected_transcript_is_in_review_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "转录_纠错.txt").write_text("corrected", encoding="utf-8")
            hashes = reviewed_hashes(folder)
            self.assertIn("转录_纠错.txt", hashes)


class BenchmarkTests(unittest.TestCase):
    def test_asr_metrics(self):
        result = asr_metrics("The price is 10x", "The price is 10x")
        self.assertEqual(result["wer"], 0.0)
        self.assertEqual(result["number_recall"], 1.0)


class DiarizationTests(unittest.TestCase):
    def test_word_level_speaker_split(self):
        segments = [{
            "start": 0.0,
            "end": 4.0,
            "text": "hello world reply",
            "words": [
                {"word": "hello", "start": 0.0, "end": 1.0},
                {"word": "world", "start": 1.0, "end": 2.0},
                {"word": "reply", "start": 2.1, "end": 3.0},
            ],
        }]
        turns = [(0.0, 2.0, "SPEAKER_00"), (2.0, 4.0, "SPEAKER_01")]
        result = merge_segments_with_speakers(segments, turns)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["speaker"], "SPEAKER_00")
        self.assertEqual(result[1]["speaker"], "SPEAKER_01")


    def test_cleaned_text_and_metadata_survive_split(self):
        segments = [{
            "start": 0.0,
            "end": 2.0,
            "text": "CLEANED TEXT",
            "avg_logprob": -2.0,
            "no_speech_prob": 0.7,
            "words": [
                {"word": " RAW", "start": 0.0, "end": 1.0, "probability": 0.2},
                {"word": " WORDS", "start": 1.0, "end": 2.0, "probability": 0.2},
            ],
        }]
        result = merge_segments_with_speakers(segments, [(0.0, 2.0, "SPEAKER_00")])
        self.assertEqual(result[0]["text"], "CLEANED TEXT")
        self.assertEqual(result[0]["avg_logprob"], -2.0)
        self.assertEqual(result[0]["no_speech_prob"], 0.7)


    def test_changed_word_sequence_does_not_fake_speaker(self):
        words = [
            {"word": " ad", "start": 0.0, "end": 0.5},
            {"word": " important", "start": 1.0, "end": 1.5},
        ]
        result = merge_segments_with_speakers([{
            "start": 0.0, "end": 1.5, "text": "important", "words": words,
        }], [(0.0, 0.75, "SPEAKER_AD"), (0.75, 2.0, "SPEAKER_REAL")])
        self.assertIsNone(result[0]["speaker"])
        self.assertTrue(result[0]["needs_review"])
        self.assertEqual(result[0]["speaker_alignment"], "unresolved")


class ContentMapTests(unittest.TestCase):
    def setUp(self):
        self.content_map = {
            "units": [
                {"id": "U0001", "topic": "A", "claims": ["a"], "importance": "high", "status": "included", "timestamps": [[0, 1]]},
                {"id": "U0002", "topic": "B", "claims": ["b"], "importance": "medium", "status": "included", "timestamps": [[1, 2]]},
            ]
        }

    def test_valid_map(self):
        errors, _ = validate_content_map(self.content_map)
        self.assertEqual(errors, [])

    def test_high_coverage_gate(self):
        result = coverage_report(self.content_map, {"chapters": [{"title": "A", "unit_ids": ["U0001"]}]})
        self.assertFalse(result["passed"])
        self.assertEqual(result["high_coverage"], 1.0)
        self.assertEqual(result["medium_coverage"], 0.0)

    def test_unknown_unit_fails(self):
        result = coverage_report(self.content_map, {"chapters": [{"title": "X", "unit_ids": ["U9999"]}]})
        self.assertFalse(result["passed"])
        self.assertEqual(result["unknown_unit_ids"], ["U9999"])

    def test_summary_map_must_match_briefing_headings(self):
        errors = validate_summary_map(
            {"chapters": [{"title": "Wrong", "unit_ids": ["U0001"]}]},
            "导览。\n\n## Right\n正文。",
        )
        self.assertTrue(any("不存在" in error for error in errors))

    def test_high_unit_cannot_be_hidden_as_excluded(self):
        self.content_map["units"][0]["status"] = "excluded"
        self.content_map["units"][0]["notes"] = "duplicate"
        result = coverage_report(self.content_map, {"chapters": []})
        self.assertFalse(result["passed"])
        self.assertEqual(result["high_missing"], ["U0001"])

    def test_claim_ids_and_body_hash_must_match(self):
        briefing = "导览。\n\n## A\n正文甲。\n\n## B\n正文乙。"
        summary_map = {
            "chapters": [
                {
                    "title": "A",
                    "unit_ids": ["U0001"],
                    "claim_ids": ["U0001-C01"],
                    "body_sha256": body_sha256("正文甲。"),
                },
                {
                    "title": "B",
                    "unit_ids": ["U0002"],
                    "claim_ids": ["U0002-C01"],
                    "body_sha256": body_sha256("正文乙。"),
                },
            ]
        }
        self.assertEqual(
            validate_summary_map(summary_map, briefing, self.content_map), [])
        self.assertTrue(coverage_report(self.content_map, summary_map)["passed"])
        summary_map["chapters"][0]["body_sha256"] = "stale"
        errors = validate_summary_map(summary_map, briefing, self.content_map)
        self.assertTrue(any("body_sha256" in error for error in errors))

    def test_claim_evidence_is_bound_to_transcript_segments(self):
        transcript = {
            "segments": [
                {"id": "S0001", "start": 0, "end": 10, "text": "source one"},
                {"id": "S0002", "start": 10, "end": 20, "text": "source two"},
            ]
        }
        content_map = {
            "units": [{
                "id": "U0001",
                "topic": "A",
                "claims": ["claim"],
                "importance": "high",
                "status": "included",
                "timestamps": [[0, 20]],
            }]
        }
        content_map, transcript = enrich_content_map_evidence(
            content_map, transcript)
        self.assertEqual(
            validate_content_map(content_map, transcript), ([], []))
        transcript["segments"][0]["text"] = "changed"
        errors, _ = validate_content_map(content_map, transcript)
        self.assertTrue(any(
            "source_sha256" in error for error in errors))

    def test_multi_claim_units_require_precise_evidence(self):
        transcript = {
            "segments": [
                {
                    "id": f"S{index:04d}",
                    "start": index - 1,
                    "end": index,
                    "text": f"source {index}",
                }
                for index in range(1, 5)
            ]
        }
        content_map = {
            "units": [{
                "id": "U0001",
                "topic": "A",
                "claims": ["first claim", "second claim"],
                "importance": "high",
                "status": "included",
                "timestamps": [[0, 4]],
            }]
        }
        content_map, transcript = enrich_content_map_evidence(
            content_map, transcript)
        errors, _ = validate_content_map(content_map, transcript)
        self.assertTrue(any("缺少证据片段" in error for error in errors))

        mappings = [
            {
                "claim_id": "U0001-C01",
                "segment_ids": ["S0001", "S0002"],
                "confidence": "high",
                "rationale": "前两个片段直接支持第一条 claim。",
            },
            {
                "claim_id": "U0001-C02",
                "segment_ids": ["S0003", "S0004"],
                "confidence": "medium",
                "rationale": "后两个片段直接支持第二条 claim。",
            },
        ]
        content_map, transcript = apply_claim_evidence_mapping(
            content_map, transcript, mappings)
        self.assertEqual(
            validate_content_map(content_map, transcript), ([], []))

        content_map["units"][0]["claim_evidence"]["C01"] = [
            "S0001", "S0002", "S0003", "S0004"]
        content_map["units"][0]["claim_evidence"]["C02"] = [
            "S0001", "S0002", "S0003", "S0004"]
        errors, _ = validate_content_map(content_map, transcript)
        self.assertTrue(any(
            "全量复用整个单元证据" in error for error in errors))

    def test_claim_evidence_hash_detects_source_change(self):
        transcript = {
            "segments": [
                {"id": "S0001", "start": 0, "end": 1, "text": "source"},
            ]
        }
        content_map = {
            "units": [{
                "id": "U0001",
                "topic": "A",
                "claims": ["claim"],
                "importance": "high",
                "status": "included",
                "timestamps": [[0, 1]],
            }]
        }
        content_map, transcript = enrich_content_map_evidence(
            content_map, transcript)
        transcript["segments"][0]["text"] = "changed"
        errors, _ = validate_content_map(content_map, transcript)
        self.assertTrue(any(
            "claim evidence hash" in error for error in errors))

    def test_v2_notes_claims_are_bound_to_notes_hash(self):
        content_map = {
            "schema_version": 2,
            "units": [{
                "id": "U0001",
                "topic": "A",
                "claims": ["claim"],
                "importance": "high",
                "status": "included",
                "timestamps": [[0, 1]],
            }],
        }
        briefing = "导览。\n\n## A\n正文。"
        summary_map = {
            "chapters": [{
                "title": "A",
                "unit_ids": ["U0001"],
                "claim_ids": ["U0001-C01"],
                "body_sha256": body_sha256("正文。"),
            }]
        }
        summary_map = enrich_summary_map_evidence(
            summary_map, "完整笔记。", content_map)
        self.assertEqual(validate_summary_map(
            summary_map, briefing, content_map, "完整笔记。"), [])
        errors = validate_summary_map(
            summary_map, briefing, content_map, "笔记已修改。")
        self.assertTrue(any("notes_sha256" in error for error in errors))


class EpisodeTests(unittest.TestCase):
    def test_manifest_separates_display_title_from_slug_and_storage(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Long Folder Name & Guest"
            folder.mkdir()
            (folder / "来源.md").write_text(
                "# 来源信息\n"
                "- 标题：Title | Guest\n"
                "- 链接：https://example.com/episode\n"
                "- 转录质量：已纠错\n",
                encoding="utf-8",
            )
            (folder / "Long Folder Name & Guest.mp3").write_bytes(b"x")
            manifest = load_episode(folder, create=True)

            self.assertEqual(manifest["display_title"], "Title | Guest")
            self.assertEqual(
                manifest["storage_name"], "Long Folder Name & Guest")
            self.assertRegex(
                manifest["slug"], r"^[a-z0-9-]+-[a-f0-9]{8}$")
            self.assertEqual(
                manifest["publish"]["audio_key"],
                "Long Folder Name & Guest/Long Folder Name & Guest.mp3",
            )

    def test_review_status_does_not_overwrite_transcript_status(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "来源.md").write_text(
                "- 转录质量：已纠错\n", encoding="utf-8")
            load_episode(folder, create=True)
            update_review_status(folder, True)
            text = (folder / "来源.md").read_text(encoding="utf-8")
            self.assertIn("- 转录质量：已纠错", text)
            self.assertIn("- 内容审查：AI已审查（通过）", text)
            self.assertEqual(
                load_episode(folder)["quality"]["content_review_status"],
                "passed",
            )

    def test_stable_slug_is_deterministic(self):
        self.assertEqual(
            stable_slug("Title | Guest", "https://example.com/e"),
            stable_slug("Title | Guest", "https://example.com/e"),
        )


class TTSTests(unittest.TestCase):
    def test_lexicon_uses_longest_match_first(self):
        text = apply_tts_lexicon(
            "OpenAI 和 AI",
            {"AI": "人工智能", "OpenAI": "Open A I"},
        )
        self.assertEqual(text, "Open A I 和 人工智能")

    def test_lexicon_does_not_cascade_or_replace_ascii_substrings(self):
        text = apply_tts_lexicon(
            "OpenAI uses AI tokens.",
            {"OpenAI": "AI公司", "AI": "人工智能", "token": "令牌"},
        )
        self.assertEqual(text, "AI公司 uses 人工智能 tokens.")

    def test_failed_section_does_not_replace_final_audio(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text(
                "开场内容足够。\n\n"
                "## 第一章\n第一章正文足够。\n\n"
                "## 第二章\n第二章正文足够。",
                encoding="utf-8",
            )
            final = folder / "episode.mp3"
            final.write_bytes(b"previous-final-audio")

            with patch(
                    "tts.synth_chunks_concurrent",
                    side_effect=[
                        [b"ID3" + b"a" * 2048],
                        RuntimeError("temporary failure"),
                    ]), \
                    patch("tts.merge_mp3s") as merge, \
                    patch("tts.time.sleep"):
                result = run_tts(
                    str(folder), "讲书稿.md", "episode", concurrency=1)

            self.assertFalse(result.ok)
            self.assertEqual(final.read_bytes(), b"previous-final-audio")
            merge.assert_not_called()
            manifest = json.loads(
                (folder / "tts_manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["completed"])
            self.assertEqual(manifest["failed_sections"], ["01_第一章.mp3"])

    def test_failed_merge_does_not_replace_final_audio(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text(
                "开场内容足够。\n\n## 第一章\n第一章正文足够。",
                encoding="utf-8",
            )
            final = folder / "episode.mp3"
            final.write_bytes(b"previous-final-audio")

            with patch(
                    "tts.synth_chunks_concurrent",
                    return_value=[b"ID3" + b"a" * 2048]), \
                    patch("tts.merge_mp3s", return_value=False), \
                    patch("tts.time.sleep"):
                result = run_tts(
                    str(folder), "讲书稿.md", "episode", concurrency=1)

            self.assertFalse(result.ok)
            self.assertEqual(final.read_bytes(), b"previous-final-audio")
            manifest = json.loads(
                (folder / "tts_manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["completed"])
            self.assertIn("merge_error", manifest)

    def test_tts_cache_uses_configuration_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text(
                "开场内容足够。\n\n"
                "## 第一章\n第一章正文足够。",
                encoding="utf-8",
            )

            def fake_merge(_inputs, output, **_kwargs):
                Path(output).write_bytes(b"ID3" + b"m" * 4096)
                return True

            with patch(
                    "tts.synth_chunks_concurrent",
                    return_value=[b"ID3" + b"a" * 2048]) as synth, \
                    patch("tts.merge_mp3s", side_effect=fake_merge), \
                    patch("tts.time.sleep"):
                first = run_tts(
                    str(folder), "讲书稿.md", "episode",
                    speed=1.0, concurrency=1)
                first_calls = synth.call_count
                second = run_tts(
                    str(folder), "讲书稿.md", "episode",
                    speed=1.0, concurrency=1)
                cached_calls = synth.call_count
                third = run_tts(
                    str(folder), "讲书稿.md", "episode",
                    speed=1.25, concurrency=1)

            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            self.assertTrue(third.ok)
            self.assertEqual(first_calls, 2)
            self.assertEqual(cached_calls, first_calls)
            self.assertEqual(synth.call_count, first_calls + 2)

    def test_tts_manifest_detects_changed_briefing(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            briefing = folder / "讲书稿.md"
            briefing.write_text(
                "开场内容足够。\n\n## 第一章\n第一章正文足够。",
                encoding="utf-8",
            )

            def fake_merge(_inputs, output, **_kwargs):
                Path(output).write_bytes(b"ID3" + b"m" * 4096)
                return True

            with patch(
                    "tts.synth_chunks_concurrent",
                    return_value=[b"ID3" + b"a" * 2048]), \
                    patch("tts.merge_mp3s", side_effect=fake_merge), \
                    patch("tts.time.sleep"):
                result = run_tts(
                    str(folder), "讲书稿.md", "episode", concurrency=1)

            self.assertTrue(result.ok)
            self.assertEqual(
                validate_tts_manifest(folder, "讲书稿.md", "episode"), [])
            briefing.write_text(
                "开场内容已经修改。\n\n## 第一章\n第一章正文足够。",
                encoding="utf-8",
            )
            self.assertTrue(any(
                "指纹已过期" in error
                for error in validate_tts_manifest(
                    folder, "讲书稿.md", "episode")
            ))


class ProcessTests(unittest.TestCase):
    def test_html_generation_cannot_bypass_quality_gate(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "讲书稿.md").write_text(
                "完整导览内容足够长，可以进入正文。\n\n"
                "## 第一章\n这是第一章的正文内容。",
                encoding="utf-8",
            )
            with patch("process._run_quality_gate", return_value=False), \
                    patch("process.md_to_html") as html:
                self.assertFalse(
                    pipeline_process.run_html_step(folder, "episode", "讲书稿.md"))
                html.assert_not_called()

    def test_quality_gate_auto_reviews_only_missing_or_stale_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "content_map.json").write_text("{}", encoding="utf-8")
            missing = {
                "passed": False,
                "errors": [
                    "来源质量未通过自动关口: 待人工抽查",
                    "缺少 ai_review.json，不能自动发布",
                ],
                "warnings": [],
            }
            passed = {"passed": True, "errors": [], "warnings": []}
            with patch(
                    "quality_report.build_quality_report",
                    side_effect=[missing, passed]), \
                    patch("ai_review.review_episode") as review:
                self.assertTrue(pipeline_process._run_quality_gate(folder))
                review.assert_called_once()

    def test_quality_gate_does_not_repeat_current_failed_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "content_map.json").write_text("{}", encoding="utf-8")
            failed = {
                "passed": False,
                "errors": [
                    "来源质量未通过自动关口: AI审查未通过",
                    "AI 最终审查未通过",
                ],
                "warnings": [],
            }
            with patch(
                    "quality_report.build_quality_report",
                    return_value=failed), \
                    patch("ai_review.review_episode") as review:
                self.assertFalse(pipeline_process._run_quality_gate(folder))
                review.assert_not_called()

    def test_quality_gate_requires_content_map_unless_legacy_is_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self.assertFalse(pipeline_process._run_quality_gate(
                folder, auto_ai_review=False))
            self.assertTrue(pipeline_process._run_quality_gate(
                folder, auto_ai_review=False, allow_legacy=True))


class CatalogTests(unittest.TestCase):
    def test_audio_duration_prefers_ffprobe(self):
        result = SimpleNamespace(returncode=0, stdout="1186.5\n")
        with patch("catalog.subprocess.run", return_value=result):
            self.assertEqual(
                catalog._audio_duration_minutes(Path("episode.mp3")), 20)

    def test_new_site_entry_requires_strict_quality_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            content = Path(td) / "content"
            folder = content / "Episode"
            folder.mkdir(parents=True)
            (folder / "讲书稿.md").write_text("正文", encoding="utf-8")
            (folder / "Episode.mp3").write_bytes(b"x" * 2048)
            (folder / "Episode - content.html").write_text(
                "<html></html>", encoding="utf-8")
            with patch.object(catalog, "CONTENT_DIR", content):
                errors = catalog._site_readiness_errors(
                    ["Episode"], existing={})
                legacy_errors = catalog._site_readiness_errors(
                    ["Episode"], existing={"Episode": {}})
            self.assertTrue(any(
                "content_map.json" in error for error in errors))
            self.assertEqual(legacy_errors, [])

    def test_rebuild_catalog_and_site_share_current_stats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content = root / "content"
            site = root / "site"
            catalog_path = content / "播客目录.md"
            site.mkdir(parents=True)
            for name in ("First", "Second"):
                folder = content / name
                folder.mkdir(parents=True)
                (folder / "讲书稿.md").write_text(
                    "正文内容", encoding="utf-8")
                (folder / f"{name}.mp3").write_bytes(b"x" * 2048)
                (folder / f"{name} - content.html").write_text(
                    "<html></html>", encoding="utf-8")
            (site / "site.json").write_text(json.dumps([
                {"folder": "First", "duration": 1, "words": 1},
                {"folder": "Second", "duration": 1, "words": 1},
            ]), encoding="utf-8")
            stats = {
                "First": {"chars": 2100, "duration": 20},
                "Second": {"chars": 3200, "duration": 30},
            }
            with patch.object(catalog, "CONTENT_DIR", content), \
                    patch.object(catalog, "SITE_DIR", site), \
                    patch.object(catalog, "CATALOG", catalog_path), \
                    patch.object(
                        catalog, "episode_stats",
                        side_effect=lambda name: stats[name]):
                catalog.rebuild_catalog()
                self.assertIn("20min", catalog_path.read_text(
                    encoding="utf-8"))
                self.assertTrue(catalog.catalog_consistency_errors())
                catalog.sync_site()
                catalog.rebuild_catalog()
                self.assertEqual(catalog.catalog_consistency_errors(), [])

    def test_finish_fails_when_remote_verification_fails(self):
        with tempfile.TemporaryDirectory() as td:
            content = Path(td) / "content"
            site = Path(td) / "site"
            folder = content / "Episode"
            folder.mkdir(parents=True)
            site.mkdir()
            mp3 = folder / "Episode.mp3"
            mp3.write_bytes(b"x" * 4096)
            with patch.object(catalog, "CONTENT_DIR", content), \
                    patch.object(catalog, "SITE_DIR", site), \
                    patch.object(catalog, "BASE_DIR", Path(td)), \
                    patch.object(catalog, "_publish_preflight", return_value=True), \
                    patch.object(catalog, "sync_site"), \
                    patch.object(catalog, "rebuild_catalog", return_value=["Episode"]), \
                    patch.object(
                        catalog, "catalog_consistency_errors",
                        return_value=[]), \
                    patch.object(catalog, "gen_index"), \
                    patch.object(catalog, "_gen_mp3", return_value=mp3), \
                    patch.object(catalog, "_run", return_value=True), \
                    patch.object(
                        catalog, "_run_with_output",
                        return_value=(True, "https://abc.podcast-scripts.pages.dev"),
                    ), \
                    patch.object(
                        catalog, "verify_publish",
                        return_value={
                            "passed": False,
                            "errors": ["episode unavailable"],
                        },
                    ), \
                    patch.object(
                        catalog, "R2_PUBLIC_URL",
                        "https://audio.example.com",
                    ), \
                    patch.object(
                        catalog, "PAGES_BASE_URL",
                        "https://podcast.example.com",
                    ), \
                    patch.object(catalog, "write_publish_report") as write:
                self.assertFalse(catalog.finish("Episode"))
                write.assert_called_once()

    def test_publish_retry_handles_pages_propagation(self):
        failed = {
            "passed": False,
            "errors": ["单期页面缺少音频播放器"],
        }
        passed = {"passed": True, "errors": []}
        with patch.object(
                catalog, "verify_publish",
                side_effect=[failed, passed]) as verify, \
                patch.object(catalog.time, "sleep") as sleep:
            report = catalog._verify_publish_with_retry(
                "home", "episode", "audio", "title", Path("audio.mp3"),
                attempts=3, delay=1,
            )
        self.assertTrue(report["passed"])
        self.assertEqual(verify.call_count, 2)
        sleep.assert_called_once_with(1)


class PublishTests(unittest.TestCase):
    def test_verify_publish_checks_pages_and_r2_range(self):
        title = "Episode & Guest"
        audio_size = 4096

        def handler(request):
            if request.url.path == "/":
                return httpx.Response(200, text=f"<h3>{title}</h3>")
            if request.url.path == "/episode/content.html":
                return httpx.Response(
                    308, headers={"location": "/episode/content"})
            if request.url.path == "/episode/content":
                return httpx.Response(
                    200,
                    text=(
                        f"<title>{title}</title>"
                        '<audio id="podcastAudio"></audio>'
                    ),
                )
            if request.url.path == "/audio.mp3" and request.method == "HEAD":
                return httpx.Response(200, headers={
                    "content-type": "audio/mpeg",
                    "content-length": str(audio_size),
                    "accept-ranges": "bytes",
                })
            if request.url.path == "/audio.mp3":
                return httpx.Response(
                    206,
                    content=b"x" * 1024,
                    headers={
                        "content-type": "audio/mpeg",
                        "content-range": f"bytes 0-1023/{audio_size}",
                    },
                )
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as td:
            mp3 = Path(td) / "audio.mp3"
            mp3.write_bytes(b"x" * audio_size)
            with httpx.Client(
                    transport=httpx.MockTransport(handler),
                    follow_redirects=True) as client:
                report = verify_publish(
                    "https://example.test/",
                    "https://example.test/episode/content.html",
                    "https://media.test/audio.mp3",
                    title,
                    mp3,
                    client=client,
                )

        self.assertTrue(report["passed"])
        self.assertEqual(report["checks"]["episode"]["status"], 200)
        self.assertEqual(report["checks"]["audio_range"]["status"], 206)

    def test_verify_publish_rejects_remote_size_mismatch(self):
        def handler(request):
            if request.url.path == "/":
                return httpx.Response(200, text="Episode")
            if request.url.path == "/content":
                return httpx.Response(
                    200,
                    text='<h1>Episode</h1><audio id="podcastAudio"></audio>',
                )
            if request.method == "HEAD":
                return httpx.Response(200, headers={
                    "content-type": "audio/mpeg",
                    "content-length": "1",
                    "accept-ranges": "bytes",
                })
            return httpx.Response(
                206,
                content=b"x" * 1024,
                headers={"content-range": "bytes 0-1023/4096"},
            )

        with tempfile.TemporaryDirectory() as td:
            mp3 = Path(td) / "audio.mp3"
            mp3.write_bytes(b"x" * 4096)
            with httpx.Client(
                    transport=httpx.MockTransport(handler),
                    follow_redirects=True) as client:
                report = verify_publish(
                    "https://example.test/",
                    "https://example.test/content",
                    "https://media.test/audio.mp3",
                    "Episode",
                    mp3,
                    client=client,
                )

        self.assertFalse(report["passed"])
        self.assertTrue(any(
            "大小" in error for error in report["errors"]))


class RunReportTests(unittest.TestCase):
    def test_run_report_records_success_and_failure_stages(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            report = RunReport(folder, "test", {"mode": "unit"})
            with report.stage("success") as stage:
                stage.metrics["count"] = 2
            with report.stage("failed") as stage:
                stage.fail("expected failure")
            report.finish(False, "command failed")

            payload = json.loads(
                (folder / "run_report.json").read_text(encoding="utf-8"))
            run = payload["runs"][-1]
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["stages"][0]["status"], "passed")
            self.assertEqual(run["stages"][0]["metrics"]["count"], 2)
            self.assertEqual(run["stages"][1]["status"], "failed")

            second = RunReport(folder, "test-again")
            second.finish(True)
            payload = json.loads(
                (folder / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(payload["runs"]), 2)
            self.assertEqual(payload["runs"][-1]["status"], "passed")


class HtmlTests(unittest.TestCase):
    def test_content_page_has_editorial_and_accessible_structure(self):
        html = _build_html(
            "Episode",
            [(-1, None, "导览。"), (0, "章节", "正文。")],
            word_count=10,
            date_str="2026-08-02",
            mp3_url="episode.mp3",
            source_sha256="a" * 64,
        )
        self.assertIn('class="hero-kicker"', html)
        self.assertIn('class="player-heading"', html)
        self.assertIn('class="page-footer"', html)
        self.assertIn('aria-controls="toc"', html)
        self.assertIn('id="main-content"', html)
        self.assertIn(
            '<meta name="podcast-source-sha256" content="' + "a" * 64 + '">',
            html,
        )
        self.assertNotIn("onmouseover=", html)

    def test_homepage_source_link_is_not_behind_a_card_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            site = Path(td)
            (site / "site.json").write_text(
                json.dumps([{
                    "folder": "episode",
                    "path": "episode",
                    "title": "Episode",
                    "source_name": "Source",
                    "source_url": "https://example.com",
                    "duration": 10,
                    "words": 1000,
                }]),
                encoding="utf-8",
            )
            (site / "index.html").write_text(
                "<!-- STATS:START --><!-- STATS:END -->"
                "<!-- CARDS:START --><!-- CARDS:END -->",
                encoding="utf-8",
            )
            with patch.object(catalog, "SITE_DIR", site):
                catalog.gen_index()
            html = (site / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="episode-card-main"', html)
        self.assertIn('class="episode-source"', html)
        self.assertNotIn('class="episode-card-link"', html)

    def test_mobile_player_shares_top_row_with_toc_button(self):
        html = _build_html(
            "Episode",
            [(-1, None, "导览。"), (0, "章节", "正文。")],
            word_count=10,
            date_str="2026-08-02",
            mp3_url="episode.mp3",
        )
        self.assertIn(".toc-toggle { top: 0.75rem; left: 0.75rem;", html)
        self.assertIn(
            "position: fixed;\n"
            "        top: 0.75rem;\n"
            "        left: 4.25rem;\n"
            "        right: 0.75rem;",
            html,
        )
        self.assertIn("width: auto;\n        margin: 0;", html)
        self.assertIn("scroll-margin-top: 10rem;", html)


if __name__ == "__main__":
    unittest.main()
