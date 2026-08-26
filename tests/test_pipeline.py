import json
import hashlib
import os
import ssl
import tempfile
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import atomic_io
import config
import fetcher
import quality_report
import tts
from asr_refinement import build_asr_context
from content_map import (
    apply_claim_evidence_mapping, body_sha256, coverage_report,
    enrich_content_map_evidence,
    enrich_summary_map_evidence, init_content_map, transcript_evidence_mode,
    validate_content_map, validate_summary_map,
)
from diarize import merge_segments_with_speakers
from benchmark import asr_metrics
from quality_report import (
    _ai_entity_accuracy_consistency,
    _ai_fact_check_consistency,
    build_quality_report,
)
from tts import apply_tts_lexicon, run_tts, validate_tts_manifest
import process as pipeline_process
from ai_review import rebind_provenance_review, reviewed_hashes
import catalog_core
import catalog_health
import catalog_publish
import catalog_site
from html_gen import _build_html, md_to_html
from publish import verify_publish
from episode import (
    inspect_episode_state, load_episode, set_claim_evidence_mode,
    stable_slug, sync_episode_state, update_review_status,
)
from evidence import migrate_evidence_provenance
from release import load_release, prepare_release
from fetcher import (
    _HTML_CACHE,
    _extract_podscripts_segments,
    _is_certificate_verification_error,
    clean_whisper_hallucinations,
    detect_source_warnings,
    extract_title_from_url,
    fetch_transcript_from_url,
    resolve_asr_config,
    chunk_plain_transcript,
    transcribe,
)
from run_report import RunReport
from sources import source_label


class FetcherTests(unittest.TestCase):

    def test_podscripts_void_tags_do_not_leak_group_depth(self):
        html = """
        <div class="single-sentence">
          <span class="pod_timestamp_indicator">00:01</span>
          <span class="transcript-text">First<br>line<img src="x">.</span>
        </div>
        <div class="single-sentence">
          <span class="pod_timestamp_indicator">00:02</span>
          <span class="transcript-text">Second.</span>
        </div>
        """
        segments = _extract_podscripts_segments(html)
        self.assertEqual([item["text"] for item in segments], [
            "First line.", "Second.",
        ])

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

    def test_podscripts_accepts_timestamp_copy_variants(self):
        html = """
        <div class="single-sentence">
          <span class="pod_timestamp_indicator">Starts at 01:02</span>
          <span class="transcript-text">First.</span>
        </div>
        <div class="single-sentence">
          <span class="pod_timestamp_indicator">Begins at 01:03:04</span>
          <span class="transcript-text">Second.</span>
        </div>
        """
        segments = _extract_podscripts_segments(html)
        self.assertEqual(segments[0]["start"], 62)
        self.assertEqual(segments[1]["start"], 3784)

    def test_podscripts_without_timestamps_is_labeled_honestly(self):
        text = "Transcript sentence. " * 40
        html = (
            '<div class="single-sentence">'
            '<span class="pod_timestamp_indicator">Time unavailable</span>'
            f'<span class="transcript-text">{text}</span>'
            "</div>"
        )
        with patch("fetcher._try_rss_transcript", return_value=None), \
                patch("fetcher._fetch_html", return_value=(html, {})):
            result = fetch_transcript_from_url(
                "https://podscripts.co/example", return_metadata=True)
        self.assertFalse(result["meta"]["timestamped"])
        self.assertEqual(
            result["meta"]["extractor"],
            "podscripts_transcript_text_no_timestamps",
        )

    def test_title_and_transcript_reuse_same_cached_page(self):
        text = "Transcript sentence. " * 40
        html = (
            "<title>Episode Transcript</title>"
            '<div class="single-sentence">'
            '<span class="pod_timestamp_indicator">Starting point is 00:12</span>'
            f'<span class="transcript-text">{text}</span>'
            "</div>"
        )
        _HTML_CACHE.clear()
        with patch(
                "fetcher._try_curl_cffi_with_metadata",
                return_value=(html, {"transport": "curl_cffi"})) as fetch, \
                patch("fetcher._try_rss_transcript", return_value=None):
            self.assertEqual(
                extract_title_from_url("https://podscripts.co/example"),
                "Episode",
            )
            result = fetch_transcript_from_url(
                "https://podscripts.co/example", return_metadata=True)
        self.assertTrue(result["meta"]["timestamped"])
        fetch.assert_called_once()

    def test_fetch_html_retries_the_transport_chain(self):
        html = "<html>" + ("content " * 200) + "</html>"
        _HTML_CACHE.clear()
        with patch(
                "fetcher._try_curl_cffi_with_metadata",
                side_effect=[
                    (None, {"status_code": 503}),
                    (html, {"transport": "curl_cffi"}),
                ]), \
                patch("fetcher._try_curl", return_value=None), \
                patch(
                    "fetcher._try_httpx_with_metadata",
                    return_value=(None, {"status_code": 503})), \
                patch("fetcher.FETCH_MAX_RETRIES", 2), \
                patch("fetcher.API_RETRY_BACKOFF", 2), \
                patch("fetcher.time.sleep") as sleep:
            result, metadata = fetcher._fetch_html(
                "https://example.com/transcript")
        self.assertEqual(result, html)
        self.assertEqual(metadata["fetch_retry_count"], 1)
        sleep.assert_called_once_with(2)

    def test_tls_downgrade_guard_only_accepts_certificate_errors(self):
        self.assertTrue(_is_certificate_verification_error(
            ssl.SSLCertVerificationError(1, "certificate expired")))
        self.assertFalse(_is_certificate_verification_error(
            RuntimeError("connection reset")))

    def test_explicit_model_wins(self):
        config = resolve_asr_config("balanced", "large-v3-turbo")
        self.assertEqual(config["model_size"], "large-v3-turbo")
        self.assertEqual(
            resolve_asr_config("balanced")["model_size"],
            "large-v3-turbo",
        )
        self.assertEqual(
            resolve_asr_config("max")["model_size"],
            "large-v3",
        )
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

    def test_asr_auto_language_and_missing_diarization_token_degrade_cleanly(self):
        word = SimpleNamespace(
            word=" hello", start=0.0, end=0.5, probability=0.9)
        segment = SimpleNamespace(
            start=0.0, end=0.5, text=" hello", words=[word],
            avg_logprob=-0.1, compression_ratio=1.0,
            no_speech_prob=0.0, temperature=0.0,
        )
        info = SimpleNamespace(language="zh", language_probability=0.55)

        class FakeModel:
            def __init__(self):
                self.kwargs = None

            def transcribe(self, _audio, **kwargs):
                self.kwargs = kwargs
                return iter([segment]), info

        fake = FakeModel()
        with patch("fetcher._load_whisper_model", return_value=fake), \
                patch(
                    "config.require_hf_token",
                    side_effect=RuntimeError("HF_TOKEN missing"),
                ):
            result = transcribe(
                "fake.mp3",
                diarize_audio=True,
                language=None,
                return_metadata=True,
            )
        self.assertNotIn("language", fake.kwargs)
        self.assertEqual(result["meta"]["requested_language"], "auto")
        self.assertFalse(result["meta"]["diarization"])
        self.assertEqual(
            result["meta"]["diarization_warning"], "missing_hf_token")

    def test_adaptive_asr_redecodes_difficult_ranges(self):
        word = SimpleNamespace(
            word=" crop", start=5.0, end=6.0, probability=0.2)
        segment = SimpleNamespace(
            start=5.0, end=7.0, text=" Example crop made 15 billion",
            words=[word, word, word],
            avg_logprob=-2.0, compression_ratio=2.6,
            no_speech_prob=0.0, temperature=0.0,
        )
        info = SimpleNamespace(language="en", language_probability=0.99)

        class FakeModel:
            def transcribe(self, _audio, **_kwargs):
                return iter([segment]), info

        candidate = [{
            "start": 5.0,
            "end": 7.0,
            "text": "Example Corp made $15 billion",
            "avg_logprob": -0.1,
            "compression_ratio": 1.0,
            "no_speech_prob": 0.0,
            "temperature": 0.0,
            "words": [
                {
                    "word": " Example",
                    "start": 5.0,
                    "end": 5.4,
                    "probability": 0.95,
                },
                {
                    "word": " Corp",
                    "start": 5.4,
                    "end": 5.8,
                    "probability": 0.95,
                },
                {
                    "word": " made",
                    "start": 5.8,
                    "end": 6.1,
                    "probability": 0.95,
                },
                {
                    "word": " $15",
                    "start": 6.1,
                    "end": 6.5,
                    "probability": 0.95,
                },
                {
                    "word": " billion",
                    "start": 6.5,
                    "end": 7.0,
                    "probability": 0.95,
                },
            ],
        }]
        context = build_asr_context(
            title="Example Corp Revenue",
            hotwords="Example Corp",
        )
        with patch("fetcher._load_whisper_model", return_value=FakeModel()), \
                patch(
                    "fetcher._transcribe_audio_range",
                    return_value=candidate,
                ) as range_decode:
            result = transcribe(
                "fake.mp3",
                quality="balanced",
                asr_context=context,
                diarize_audio=False,
                return_metadata=True,
            )
        range_decode.assert_called_once()
        self.assertEqual(result["text"], "Example Corp made $15 billion")
        self.assertEqual(
            result["meta"]["adaptive_refinement"]["accepted_ranges"], 1)
        self.assertEqual(
            result["segments"][0]["refinement"]["kind"],
            "adaptive_redecode",
        )

    def test_adaptive_asr_can_be_disabled(self):
        segment = SimpleNamespace(
            start=0.0, end=2.0, text=" uncertain words",
            words=[], avg_logprob=-2.0, compression_ratio=2.6,
            no_speech_prob=0.0, temperature=0.0,
        )
        info = SimpleNamespace(language="en", language_probability=0.99)

        class FakeModel:
            def transcribe(self, _audio, **_kwargs):
                return iter([segment]), info

        with patch("fetcher._load_whisper_model", return_value=FakeModel()), \
                patch("fetcher._transcribe_audio_range") as range_decode:
            result = transcribe(
                "fake.mp3",
                quality="balanced",
                adaptive_refinement=False,
                diarize_audio=False,
                return_metadata=True,
            )
        range_decode.assert_not_called()
        self.assertFalse(
            result["meta"]["adaptive_refinement"]["enabled"])

    def test_alignment_runs_before_exclusive_diarization(self):
        word = SimpleNamespace(
            word=" hello", start=0.0, end=0.5, probability=0.9)
        segment = SimpleNamespace(
            start=0.0, end=0.5, text=" hello", words=[word],
            avg_logprob=-0.1, compression_ratio=1.0,
            no_speech_prob=0.0, temperature=0.0,
        )
        info = SimpleNamespace(language="en", language_probability=0.99)

        class FakeModel:
            def transcribe(self, _audio, **_kwargs):
                return iter([segment]), info

        aligned_segment = {
            "start": 0.1,
            "end": 0.4,
            "text": "hello",
            "words": [{
                "word": "hello",
                "start": 0.1,
                "end": 0.4,
                "probability": 0.95,
            }],
        }

        def diarize_after_alignment(_audio, segments, **_kwargs):
            self.assertEqual(segments[0]["start"], 0.1)
            self.assertEqual(
                segments[0]["words"][0]["probability"], 0.95)
            item = dict(segments[0])
            item["speaker"] = "SPEAKER_00"
            return {
                "segments": [item],
                "meta": {
                    "model": (
                        "pyannote/"
                        "speaker-diarization-community-1"
                    ),
                    "exclusive_used": True,
                },
            }

        with patch("fetcher._load_whisper_model", return_value=FakeModel()), \
                patch("asr_alignment.align_segments", return_value={
                    "segments": [aligned_segment],
                    "meta": {
                        "enabled": True,
                        "adapter": "whisperx",
                        "status": "complete",
                        "word_timestamp_coverage": 1.0,
                    },
                }), \
                patch("config.require_hf_token", return_value="token"), \
                patch(
                    "diarize.diarize_and_merge",
                    side_effect=diarize_after_alignment,
                ):
            result = transcribe(
                "fake.mp3",
                quality="balanced",
                align_audio=True,
                diarize_audio=True,
                adaptive_refinement=False,
                return_metadata=True,
            )
        self.assertTrue(result["meta"]["alignment"]["enabled"])
        self.assertTrue(result["meta"]["diarization_exclusive"])
        self.assertEqual(result["segments"][0]["speaker"], "SPEAKER_00")


class QualityReportTests(unittest.TestCase):
    def test_provenance_rebind_rejects_semantic_changes(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            folder.mkdir()
            (folder / "episode.json").write_text(json.dumps({
                "schema_version": 1,
                "storage_name": "Episode",
                "slug": "episode-12345678",
                "display_title": "Episode",
                "source": {"url": "", "label": "", "kind": "local_transcript"},
                "quality": {},
                "publish": {"page_path": "episode-12345678"},
            }), encoding="utf-8")
            (folder / "来源.md").write_text(
                "- 转录质量：官方字幕\n", encoding="utf-8")
            (folder / "原始转录.txt").write_text(
                "original transcript", encoding="utf-8")
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "local_transcript",
                "segments": [],
                "meta": {},
            }), encoding="utf-8")
            (folder / "Episode - 原始音频.mp3").write_bytes(b"audio")
            review = {
                "passed": True,
                "transcript_quality": {
                    "passed": True,
                    "score": 95,
                    "issues": ["官方字幕无时间戳"],
                },
                "issues": [],
                "fact_checks": [],
                "reviewed_files": reviewed_hashes(folder),
            }
            (folder / "ai_review.json").write_text(
                json.dumps(review, ensure_ascii=False), encoding="utf-8")

            migrate_evidence_provenance(folder)
            sync_episode_state(folder)
            rebound = rebind_provenance_review(folder)
            self.assertEqual(
                rebound["provenance_rebind"]["method"], "metadata_only")
            self.assertEqual(
                rebound["transcript_quality"]["corrected_score"], 95)
            self.assertEqual(
                rebound["transcript_quality"]["accuracy_basis"],
                "semantic_review_only",
            )
            self.assertIn(
                "历史 ASR 转录",
                rebound["transcript_quality"]["issues"][0],
            )

            (folder / "原始转录.txt").write_text(
                "semantic change", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "语义文件变化"):
                rebind_provenance_review(folder)

    def test_legacy_asr_is_inferred_from_original_audio(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            folder.mkdir()
            audio = folder / "Episode - 原始音频.mp3"
            audio.write_bytes(b"legacy audio")
            raw_path = folder / "transcript.raw.json"
            raw_path.write_text(json.dumps({
                "source": str(folder / "legacy transcript.txt"),
                "source_kind": "local_transcript",
                "segments": [{
                    "id": "S0001",
                    "start": None,
                    "end": None,
                    "text": "legacy transcript",
                    "synthetic_boundary": True,
                }],
                "meta": {
                    "timestamped": False,
                    "evidence_mode": "text_anchor",
                },
            }), encoding="utf-8")

            migrated = migrate_evidence_provenance(folder)

            self.assertEqual(migrated["source_kind"], "legacy_asr")
            self.assertEqual(
                migrated["provenance"]["origin_kind"], "legacy_asr")
            self.assertEqual(
                migrated["provenance"]["original_audio"]["file"],
                audio.name,
            )
            self.assertEqual(
                len(migrated["provenance"]["original_audio"]["sha256"]), 64)

    def test_legacy_asr_requires_correction_even_when_raw_kind_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            folder.mkdir()
            (folder / "Episode - 原始音频.mp3").write_bytes(b"audio")
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "local_transcript",
                "segments": [{
                    "id": "S0001",
                    "start": None,
                    "end": None,
                    "text": "legacy transcript",
                    "synthetic_boundary": True,
                }],
                "meta": {
                    "timestamped": False,
                    "evidence_mode": "text_anchor",
                },
            }), encoding="utf-8")

            report = build_quality_report(folder)

            self.assertTrue(any(
                "ASR 单集缺少 转录_纠错.txt" in error
                for error in report["errors"]
            ))
            self.assertEqual(
                report["transcript"]["effective_source_kind"],
                "legacy_asr",
            )

    def test_explicit_non_asr_provenance_cannot_hide_original_audio(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            folder.mkdir()
            (folder / "Episode - 原始音频.mp3").write_bytes(b"audio")
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "local_transcript",
                "provenance": {
                    "schema_version": 1,
                    "origin_kind": "local_transcript",
                    "source": "legacy.txt",
                },
                "segments": [],
                "meta": {},
            }), encoding="utf-8")

            report = build_quality_report(folder)

            self.assertTrue(any(
                "目录存在原始音频" in error
                for error in report["errors"]
            ))

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

    def test_balanced_asr_deterministic_quality_signals_block(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "local_asr",
                "segments": [{
                    "id": "S0001",
                    "start": 0,
                    "end": 1,
                    "text": "uncertain",
                    "avg_logprob": -2.0,
                }],
                "meta": {
                    "quality": "balanced",
                    "language": "en",
                    "language_probability": 0.4,
                },
            }), encoding="utf-8")
            report = build_quality_report(folder)
        self.assertIn(
            "本地 ASR 低置信度片段超过 15%", report["errors"])
        self.assertTrue(any(
            "语言识别置信度过低" in error
            for error in report["errors"]
        ))

    def test_quality_report_observes_post_diarization_review_flags(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "local_asr",
                "segments": [{
                    "id": "S0001",
                    "start": 0,
                    "end": 1,
                    "text": "speaker uncertain",
                    "speaker_alignment": "unresolved",
                    "needs_review": True,
                }],
                "meta": {
                    "quality": "balanced",
                    "adaptive_refinement": {
                        "enabled": True,
                        "remaining_segments": 0,
                    },
                },
            }), encoding="utf-8")
            report = build_quality_report(folder)
        self.assertEqual(
            report["transcript"]["adaptive_refinement"][
                "remaining_segments"],
            1,
        )
        self.assertTrue(any(
            "仍有待复核片段" in warning
            for warning in report["warnings"]
        ))

    def test_quality_report_observes_alignment_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "local_asr",
                "segments": [{
                    "id": "S0001",
                    "start": 0,
                    "end": 1,
                    "text": "source",
                }],
                "meta": {
                    "quality": "balanced",
                    "alignment": {
                        "enabled": False,
                        "adapter": "whisper_timestamps",
                        "status": "failed",
                    },
                    "alignment_warning": "alignment_failed",
                },
            }), encoding="utf-8")
            report = build_quality_report(folder)
        self.assertTrue(any(
            "强制对齐已回退" in warning
            for warning in report["warnings"]
        ))

    def test_publish_report_does_not_enable_v2_compatibility(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "web_transcript",
                "segments": [{
                    "id": "S0001", "start": 0, "end": 1, "text": "source",
                }],
                "meta": {},
            }), encoding="utf-8")
            (folder / "content_map.json").write_text(json.dumps({
                "schema_version": 2,
                "units": [{
                    "id": "U0001",
                    "topic": "topic",
                    "claims": ["claim"],
                    "importance": "high",
                    "status": "included",
                    "timestamps": [[0, 1]],
                }],
            }), encoding="utf-8")
            (folder / "publish_report.json").write_text(
                '{"passed": true}', encoding="utf-8")
            report = build_quality_report(folder)
        self.assertTrue(any(
            "证据 schema 过旧" in error for error in report["errors"]))

    def test_explicit_episode_marker_enables_v2_compatibility(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "web_transcript",
                "segments": [{
                    "id": "S0001", "start": 0, "end": 1, "text": "source",
                }],
                "meta": {},
            }), encoding="utf-8")
            (folder / "content_map.json").write_text(json.dumps({
                "schema_version": 2,
                "units": [{
                    "id": "U0001",
                    "topic": "topic",
                    "claims": ["claim"],
                    "importance": "high",
                    "status": "included",
                    "timestamps": [[0, 1]],
                }],
            }), encoding="utf-8")
            (folder / "episode.json").write_text(json.dumps({
                "schema_version": 1,
                "quality": {"claim_evidence_mode": "legacy_broad"},
            }), encoding="utf-8")
            (folder / "publish_report.json").write_text(json.dumps({
                "passed": True,
                "checked_at": "2026-08-14T12:00:00+00:00",
            }), encoding="utf-8")
            report = build_quality_report(
                folder, today=date(2026, 8, 31))
        self.assertFalse(any(
            "证据 schema 过旧" in error for error in report["errors"]))
        self.assertTrue(any(
            "evidence v2" in warning for warning in report["warnings"]))

    def test_episode_marker_without_prefreeze_publication_cannot_enable_v2(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "web_transcript",
                "segments": [{
                    "id": "S0001", "start": 0, "end": 1, "text": "source",
                }],
                "meta": {},
            }), encoding="utf-8")
            (folder / "content_map.json").write_text(json.dumps({
                "schema_version": 2,
                "units": [{
                    "id": "U0001", "topic": "topic",
                    "claims": ["claim"], "importance": "high",
                    "status": "included", "timestamps": [[0, 1]],
                }],
            }), encoding="utf-8")
            (folder / "episode.json").write_text(json.dumps({
                "schema_version": 1,
                "quality": {"claim_evidence_mode": "legacy_broad"},
            }), encoding="utf-8")
            report = build_quality_report(
                folder, today=date(2026, 8, 31))
        self.assertTrue(any(
            "缺少冻结日前成功发布证明" in error
            for error in report["errors"]))

    def test_postfreeze_publish_report_cannot_backdate_v2_compatibility(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "web_transcript", "segments": [], "meta": {},
            }), encoding="utf-8")
            (folder / "content_map.json").write_text(json.dumps({
                "schema_version": 2, "units": [],
            }), encoding="utf-8")
            (folder / "episode.json").write_text(json.dumps({
                "schema_version": 1,
                "quality": {"claim_evidence_mode": "legacy_broad"},
            }), encoding="utf-8")
            (folder / "publish_report.json").write_text(json.dumps({
                "passed": True,
                "checked_at": "2026-08-15T00:00:00+08:00",
            }), encoding="utf-8")
            report = build_quality_report(
                folder, today=date(2026, 8, 31))
        self.assertTrue(any(
            "缺少冻结日前成功发布证明" in error
            for error in report["errors"]))

    def test_ai_fact_checks_distinguish_excluded_from_used_unsupported_claims(self):
        errors, warnings = _ai_fact_check_consistency({
            "schema_version": 2,
            "fact_checks": [{
                "claim": "not published",
                "claim_type": "public_fact",
                "verification_mode": "web_required",
                "verdict": "unsupported",
                "publication_status": "excluded",
                "evidence_segment_ids": [],
                "source_urls": [],
            }],
        })
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

        errors, _warnings = _ai_fact_check_consistency({
            "schema_version": 2,
            "fact_checks": [{
                "claim": "published as fact",
                "claim_type": "public_fact",
                "verification_mode": "web_required",
                "verdict": "unsupported",
                "publication_status": "used_as_fact",
                "evidence_segment_ids": [],
                "source_urls": [],
            }],
        })
        self.assertTrue(any("published as fact" in error for error in errors))

    def test_guest_firsthand_claim_passes_without_public_url_when_attributed(self):
        errors, warnings = _ai_fact_check_consistency({
            "schema_version": 2,
            "fact_checks": [{
                "claim": "嘉宾称内部项目转化率提高百分之二十",
                "claim_type": "guest_firsthand",
                "verification_mode": "transcript_attribution",
                "verdict": "faithfully_attributed",
                "publication_status": "attributed_or_qualified",
                "evidence_segment_ids": ["S0123"],
                "source_urls": [],
            }],
        })
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_guest_firsthand_claim_cannot_be_promoted_to_objective_fact(self):
        errors, _warnings = _ai_fact_check_consistency({
            "schema_version": 2,
            "fact_checks": [{
                "claim": "内部项目转化率提高百分之二十",
                "claim_type": "guest_firsthand",
                "verification_mode": "transcript_attribution",
                "verdict": "faithfully_attributed",
                "publication_status": "used_as_fact",
                "evidence_segment_ids": ["S0123"],
                "source_urls": [],
            }],
        })
        self.assertTrue(any("必须明确归因" in error for error in errors))

    def test_public_fact_requires_web_source_when_used_as_fact(self):
        errors, _warnings = _ai_fact_check_consistency({
            "schema_version": 2,
            "fact_checks": [{
                "claim": "公司正式名称为 Example Incorporated",
                "claim_type": "public_fact",
                "verification_mode": "web_required",
                "verdict": "supported",
                "publication_status": "used_as_fact",
                "evidence_segment_ids": ["S0001"],
                "source_urls": [],
            }],
        })
        self.assertTrue(any("缺少网页来源" in error for error in errors))

    def test_entity_accuracy_incorrect_name_is_a_hard_error(self):
        errors, warnings = _ai_entity_accuracy_consistency({
            "schema_version": 2,
            "entity_accuracy": {
                "passed": False,
                "issues": ["公司名称写错"],
                "checked_entities": [{
                    "observed": "Wrong Corp",
                    "canonical": "Right Corp",
                    "verdict": "incorrect",
                }],
            },
        })
        self.assertTrue(any("实体准确性" in error for error in errors))
        self.assertTrue(any("Wrong Corp" in error for error in errors))
        self.assertEqual(warnings, [])


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

    def test_high_unit_can_be_explicitly_excluded_with_reason(self):
        self.content_map["units"][0]["status"] = "excluded"
        self.content_map["units"][0]["notes"] = "duplicate"
        result = coverage_report(self.content_map, {
            "chapters": [{
                "title": "B",
                "unit_ids": ["U0002"],
                "claim_ids": ["U0002-C01"],
            }],
        })
        self.assertTrue(result["passed"])
        self.assertEqual(result["high_missing"], [])

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

    def test_text_anchor_evidence_accepts_transcript_without_timestamps(self):
        transcript = {
            "meta": {
                "timestamped": False,
                "evidence_mode": "text_anchor",
            },
            "segments": [{
                "id": "S0001",
                "start": None,
                "end": None,
                "synthetic_boundary": True,
                "text": "plain transcript source",
            }],
        }
        raw_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False)
        json.dump(transcript, raw_file, ensure_ascii=False)
        raw_file.close()
        raw_path = Path(raw_file.name)
        output = Path(tempfile.gettempdir()) / "unused-content-map.json"
        content_map = init_content_map(raw_path, output)
        raw_path.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        content_map["evidence_mode"] = "text_anchor"
        unit = content_map["units"][0]
        unit.update({
            "topic": "A",
            "claims": ["claim"],
            "importance": "high",
            "status": "included",
        })
        content_map, transcript = enrich_content_map_evidence(
            content_map, transcript)
        content_map, transcript = apply_claim_evidence_mapping(
            content_map,
            transcript,
            [{
                "claim_id": "U0001-C01",
                "segment_ids": ["S0001"],
                "confidence": "high",
                "rationale": "原始文本直接支持该 claim。",
            }],
        )
        errors, warnings = validate_content_map(content_map, transcript)
        self.assertEqual(errors, [])
        self.assertTrue(any("无时间戳" in warning for warning in warnings))

    def test_text_anchor_enrichment_preserves_existing_segment_ids(self):
        transcript = {
            "meta": {"timestamped": False, "evidence_mode": "text_anchor"},
            "segments": [{
                "id": "S0001",
                "start": None,
                "end": None,
                "text": "source",
            }],
        }
        content_map = {
            "schema_version": 3,
            "evidence_mode": "text_anchor",
            "units": [{
                "id": "U0001",
                "topic": "A",
                "claims": ["claim"],
                "importance": "high",
                "status": "included",
                "timestamps": [],
                "evidence": {
                    "mode": "text_anchor",
                    "segment_ids": ["S0001"],
                    "source_sha256": "",
                },
                "claim_evidence": {},
            }],
        }
        content_map, _ = enrich_content_map_evidence(
            content_map, transcript)
        self.assertEqual(
            content_map["units"][0]["evidence"]["segment_ids"], ["S0001"])
        self.assertEqual(validate_content_map(content_map, transcript), ([], []))

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

    def test_multiple_broad_claims_are_rejected_even_with_one_precise_claim(self):
        transcript = {
            "segments": [
                {
                    "id": f"S{index:04d}",
                    "start": index - 1,
                    "end": index,
                    "text": f"source {index}",
                }
                for index in range(1, 4)
            ]
        }
        content_map = {
            "units": [{
                "id": "U0001",
                "topic": "A",
                "claims": ["first", "second", "third"],
                "importance": "high",
                "status": "included",
                "timestamps": [[0, 3]],
            }]
        }
        content_map, transcript = enrich_content_map_evidence(
            content_map, transcript)
        content_map, transcript = apply_claim_evidence_mapping(
            content_map,
            transcript,
            [
                {
                    "claim_id": "U0001-C01",
                    "segment_ids": ["S0001", "S0002", "S0003"],
                    "confidence": "high",
                    "rationale": "全部三个片段共同支持第一条综合 claim。",
                },
                {
                    "claim_id": "U0001-C02",
                    "segment_ids": ["S0001", "S0002", "S0003"],
                    "confidence": "high",
                    "rationale": "全部三个片段共同支持第二条综合 claim。",
                },
                {
                    "claim_id": "U0001-C03",
                    "segment_ids": ["S0003"],
                    "confidence": "high",
                    "rationale": "第三个片段直接支持第三条具体 claim。",
                },
            ],
        )
        errors, _ = validate_content_map(content_map, transcript)
        self.assertTrue(any(
            "至少两条 claim 全量复用" in error for error in errors))

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
            "notes_claim_ids": ["U0001-C01"],
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
    def test_episode_state_is_derived_from_evidence_and_files(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            folder.mkdir()
            (folder / "Episode - 原始音频.mp3").write_bytes(b"audio")
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "local_transcript",
                "segments": [],
                "meta": {},
            }), encoding="utf-8")
            (folder / "转录_纠错.txt").write_text(
                "corrected transcript", encoding="utf-8")
            (folder / "episode.json").write_text(json.dumps({
                "schema_version": 1,
                "storage_name": "Episode",
                "slug": "episode-12345678",
                "display_title": "Episode",
                "source": {
                    "url": "https://example.com/episode",
                    "label": "example.com",
                    "kind": "local_transcript",
                    "extractor": "",
                },
                "quality": {
                    "mode": "strict",
                    "transcript_status": "官方字幕",
                    "correction_status": "not_required_or_pending",
                    "content_review_status": "pending",
                },
                "publish": {"page_path": "episode-12345678"},
            }), encoding="utf-8")

            state = inspect_episode_state(folder)
            self.assertEqual(state["source_kind"], "legacy_asr")
            self.assertEqual(state["correction_status"], "corrected")
            self.assertTrue(state["transcript_status"].startswith("已纠错"))

            sync_episode_state(folder, processing_date="2026-08-07")
            episode = load_episode(folder)
            source = (folder / "来源.md").read_text(encoding="utf-8")
            self.assertEqual(episode["source"]["kind"], "legacy_asr")
            self.assertEqual(
                episode["quality"]["correction_status"], "corrected")
            self.assertIn("- 转录方式：历史本地 ASR", source)
            self.assertNotIn("- 转录质量：官方字幕", source)

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

    def test_failed_review_becomes_stale_when_reviewed_input_changes(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            load_episode(folder, create=True)
            briefing = folder / "讲书稿.md"
            briefing.write_text("original", encoding="utf-8")
            digest = hashlib.sha256(briefing.read_bytes()).hexdigest()
            (folder / "ai_review.json").write_text(json.dumps({
                "passed": False,
                "reviewed_files": {"讲书稿.md": digest},
            }), encoding="utf-8")
            self.assertEqual(
                inspect_episode_state(folder)["content_review_status"],
                "failed",
            )
            briefing.write_text("repaired", encoding="utf-8")
            self.assertEqual(
                inspect_episode_state(folder)["content_review_status"],
                "stale",
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

    def test_stable_slug_normalizes_http_and_https_identity(self):
        self.assertEqual(
            stable_slug("Title", "http://example.com/episode"),
            stable_slug("Title", "https://example.com/episode"),
        )

    def test_release_id_and_audio_key_are_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            folder.mkdir()
            (folder / "episode.json").write_text(json.dumps({
                "schema_version": 1,
                "slug": "episode-12345678",
                "display_title": "Episode",
                "publish": {"page_path": "episode-12345678"},
            }), encoding="utf-8")
            mp3 = folder / "Episode.mp3"
            briefing = folder / "讲书稿.md"
            mp3.write_bytes(b"audio")
            briefing.write_text("briefing", encoding="utf-8")
            first = prepare_release(folder, mp3, briefing)
            second = prepare_release(folder, mp3, briefing)
            self.assertEqual(first["release_id"], second["release_id"])
            self.assertEqual(first["audio_key"], second["audio_key"])
            self.assertEqual(load_release(folder)["state"], "prepared")

    def test_source_label_is_shared_and_normalizes_www(self):
        self.assertEqual(
            source_label("https://www.podscripts.co/episode"),
            "podscripts.co",
        )


class TTSTests(unittest.TestCase):
    def setUp(self):
        self.fish_key = patch.dict(
            os.environ, {"FISH_KEY": "test-fish-key"})
        self.fish_key.start()

    def tearDown(self):
        self.fish_key.stop()

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

    def test_tts_honors_retry_after_and_then_succeeds(self):
        responses = [
            SimpleNamespace(
                status_code=429, headers={"retry-after": "5"},
                text="", content=b""),
            SimpleNamespace(
                status_code=503, headers={}, text="", content=b""),
            SimpleNamespace(
                status_code=200, headers={}, text="", content=b"audio"),
        ]
        client = SimpleNamespace(post=lambda *_args, **_kwargs: responses.pop(0))
        usage = tts.TTSUsage()
        with patch("tts.MAX_RETRIES", 3), \
                patch("tts.RETRY_BACKOFF", 2), \
                patch("tts.time.sleep") as sleep:
            result = tts.synth_chunk(client, "hello", usage=usage)
        self.assertEqual(result, b"audio")
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list], [5, 4])
        self.assertEqual(usage.retry_count, 2)
        self.assertEqual(usage.last_error, "HTTP 503")

    def test_tts_exhausts_retryable_5xx_without_final_sleep(self):
        response = SimpleNamespace(
            status_code=500, headers={}, text="", content=b"")
        client = SimpleNamespace(post=lambda *_args, **_kwargs: response)
        with patch("tts.MAX_RETRIES", 3), \
                patch("tts.RETRY_BACKOFF", 1), \
                patch("tts.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "重试 3 次"):
                tts.synth_chunk(client, "hello")
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list], [1, 2])


class ProcessTests(unittest.TestCase):
    def test_new_briefing_name_wins_over_legacy_prefixed_file(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            folder.mkdir()
            legacy = folder / "Episode - 讲书稿.md"
            current = folder / "讲书稿.md"
            legacy.write_text("legacy", encoding="utf-8")
            current.write_text("current", encoding="utf-8")

            name, path = pipeline_process.detect_briefing(folder)
            self.assertEqual(name, "讲书稿.md")
            self.assertEqual(path, current)
            self.assertEqual(catalog_core._find_briefing(folder), current)
            self.assertEqual(quality_report._find_briefing(folder), current)

    @staticmethod
    def _write_evidence(folder, text, source):
        metadata = pipeline_process._prepare_evidence_metadata({
            "source": source,
            "source_kind": "web_transcript",
            "segments": [{
                "start": 0,
                "end": 1,
                "text": text,
            }],
            "meta": {"timestamped": True},
        }, text)
        (folder / "原始转录.txt").write_text(text, encoding="utf-8")
        (folder / "transcript.raw.json").write_text(
            json.dumps(metadata, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_existing_evidence_is_reused_without_refetch(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            source = "https://example.com/episode"
            old_text = "old evidence " * 30
            self._write_evidence(folder, old_text, source)
            original_raw = (
                folder / "transcript.raw.json").read_bytes()
            with patch("process.fetch_transcript_from_url") as fetch:
                self.assertTrue(pipeline_process.fetch_transcript(
                    source, folder, "Episode", None))
            fetch.assert_not_called()
            self.assertEqual(
                (folder / "原始转录.txt").read_text(encoding="utf-8"),
                old_text,
            )
            self.assertEqual(
                (folder / "transcript.raw.json").read_bytes(),
                original_raw,
            )

    def test_existing_evidence_rejects_different_source(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._write_evidence(
                folder, "old evidence " * 30,
                "https://example.com/first",
            )
            with self.assertRaisesRegex(RuntimeError, "不同 source"):
                pipeline_process.fetch_transcript(
                    "https://example.com/second",
                    folder,
                    "Episode",
                    None,
                )

    def test_force_refetch_archives_previous_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            source = "https://example.com/episode"
            old_text = "old evidence " * 30
            new_text = "new evidence " * 30
            self._write_evidence(folder, old_text, source)
            with patch(
                    "process.fetch_transcript_from_url",
                    return_value={
                        "text": new_text,
                        "segments": [{
                            "start": 0,
                            "end": 2,
                            "text": new_text,
                        }],
                        "meta": {
                            "timestamped": True,
                            "extractor": "test",
                        },
                    }):
                self.assertTrue(pipeline_process.fetch_transcript(
                    source,
                    folder,
                    "Episode",
                    None,
                    force_refetch=True,
                ))
            self.assertEqual(
                (folder / "原始转录.txt").read_text(encoding="utf-8"),
                new_text,
            )
            archives = list(
                (folder / "evidence_history").glob("*/原始转录.txt"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(
                archives[0].read_text(encoding="utf-8"), old_text)

    def test_failed_force_refetch_leaves_previous_evidence_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            source = "https://example.com/episode"
            old_text = "old evidence " * 30
            self._write_evidence(folder, old_text, source)
            old_raw = (folder / "transcript.raw.json").read_bytes()
            with patch(
                    "process.fetch_transcript_from_url",
                    return_value=None):
                self.assertFalse(pipeline_process.fetch_transcript(
                    source,
                    folder,
                    "Episode",
                    None,
                    force_refetch=True,
                ))
            self.assertEqual(
                (folder / "原始转录.txt").read_text(encoding="utf-8"),
                old_text,
            )
            self.assertEqual(
                (folder / "transcript.raw.json").read_bytes(), old_raw)
            self.assertFalse((folder / "evidence_history").exists())

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
                "error_details": [
                    {"code": "source_review_status",
                     "message": "来源质量未通过自动关口: 待人工抽查"},
                    {"code": "ai_review_missing",
                     "message": "缺少 ai_review.json，不能自动发布"},
                ],
                "warnings": [],
            }
            passed = {"passed": True, "errors": [],
                      "error_details": [], "warnings": []}
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

    def test_html_stage_blocks_instead_of_mutating_review_bound_content(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            folder.mkdir()
            briefing = folder / "讲书稿.md"
            briefing.write_text(
                "开场。\n\n## 第一章\n**正文**", encoding="utf-8")
            (folder / "summary_map.json").write_text(json.dumps({
                "schema_version": 2,
                "chapters": [{
                    "title": "第一章",
                    "unit_ids": ["U0001"],
                    "claim_ids": ["U0001-C01"],
                }],
            }), encoding="utf-8")
            before = briefing.read_bytes()
            report = RunReport(folder, "test-auto-fix")
            with patch("process._run_structure_check"), \
                    patch("process._run_quality_gate", return_value=False):
                self.assertFalse(pipeline_process.run_html_step(
                    folder,
                    folder.name,
                    briefing.name,
                    run_report=report,
                ))
            report.finish(False, "expected quality failure")
            payload = json.loads(
                (folder / "run_report.json").read_text(encoding="utf-8"))
            metrics = payload["runs"][-1]["stages"][0]["metrics"]
            after = briefing.read_bytes()
        self.assertEqual(before, after)
        self.assertIn(
            "normalized_formatting",
            metrics["normalization_changes_required"],
        )
        self.assertEqual(
            metrics["briefing_sha256"], hashlib.sha256(before).hexdigest())


class CatalogTests(unittest.TestCase):
    def test_audio_duration_prefers_ffprobe(self):
        result = SimpleNamespace(returncode=0, stdout="1186.5\n")
        with patch("catalog_core.subprocess.run", return_value=result):
            self.assertEqual(
                catalog_core._audio_duration_minutes(Path("episode.mp3")), 20)

    def test_new_site_entry_requires_strict_quality_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            content = Path(td) / "content"
            folder = content / "Episode"
            folder.mkdir(parents=True)
            (folder / "讲书稿.md").write_text("正文", encoding="utf-8")
            (folder / "Episode.mp3").write_bytes(b"x" * 2048)
            (folder / "Episode - content.html").write_text(
                "<html></html>", encoding="utf-8")
            with patch.object(catalog_site, "CONTENT_DIR", content):
                errors = catalog_site._site_readiness_errors(
                    ["Episode"], existing={})
                legacy_errors = catalog_site._site_readiness_errors(
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
            with patch.object(catalog_core, "CONTENT_DIR", content), \
                    patch.object(catalog_core, "SITE_DIR", site), \
                    patch.object(catalog_site, "CONTENT_DIR", content), \
                    patch.object(catalog_site, "SITE_DIR", site), \
                    patch.object(catalog_core, "CATALOG", catalog_path), \
                    patch.object(catalog_site, "CATALOG", catalog_path), \
                    patch.object(
                        catalog_core, "episode_stats",
                        side_effect=lambda name: stats[name]), \
                    patch.object(
                        catalog_site, "episode_stats",
                        side_effect=lambda name: stats[name]):
                catalog_core.rebuild_catalog()
                self.assertIn("20min", catalog_path.read_text(
                    encoding="utf-8"))
                self.assertTrue(catalog_site.catalog_consistency_errors())
                catalog_site.sync_site()
                catalog_core.rebuild_catalog()
                self.assertEqual(catalog_site.catalog_consistency_errors(), [])

    def test_finish_fails_when_remote_verification_fails(self):
        with tempfile.TemporaryDirectory() as td:
            content = Path(td) / "content"
            site = Path(td) / "site"
            folder = content / "Episode"
            folder.mkdir(parents=True)
            site.mkdir()
            mp3 = folder / "Episode.mp3"
            mp3.write_bytes(b"x" * 4096)
            with patch.object(catalog_publish, "CONTENT_DIR", content), \
                    patch.object(catalog_publish, "SITE_DIR", site), \
                    patch.object(catalog_publish, "BASE_DIR", Path(td)), \
                    patch.object(catalog_publish, "_publish_preflight", return_value=True), \
                    patch.object(catalog_publish, "sync_site"), \
                    patch.object(catalog_publish, "rebuild_catalog", return_value=["Episode"]), \
                    patch.object(
                        catalog_publish, "catalog_consistency_errors",
                        return_value=[]), \
                    patch.object(catalog_publish, "gen_index"), \
                    patch.object(catalog_publish, "_gen_mp3", return_value=mp3), \
                    patch.object(catalog_publish, "_run", return_value=True), \
                    patch.object(
                        catalog_publish, "_run_with_output",
                        return_value=(True, "https://abc.podcast-scripts.pages.dev"),
                    ), \
                    patch.object(
                        catalog_publish, "verify_publish",
                        return_value={
                            "passed": False,
                            "errors": ["episode unavailable"],
                        },
                    ), \
                    patch.object(catalog_publish, "validate_for_stage"), \
                    patch.object(catalog_publish, "write_publish_report") as write:
                self.assertFalse(catalog_publish.finish("Episode"))
                write.assert_called_once()

    def test_publish_retry_handles_pages_propagation(self):
        failed = {
            "passed": False,
            "errors": ["单期页面缺少音频播放器"],
            "error_details": [{
                "code": "publish_episode_player_missing",
                "message": "单期页面缺少音频播放器",
            }],
        }
        passed = {"passed": True, "errors": []}
        with patch.object(
                catalog_publish, "verify_publish",
                side_effect=[failed, passed]) as verify, \
                patch.object(catalog_publish.time, "sleep") as sleep:
            report = catalog_publish._verify_publish_with_retry(
                "home", "episode", "audio", "title", Path("audio.mp3"),
                attempts=3, delay=1,
            )
        self.assertTrue(report["passed"])
        self.assertEqual(verify.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_health_report_aggregates_failures_cost_and_unpublished(self):
        now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            content = Path(td)
            failed = content / "Failed Episode"
            passed = content / "Passed Episode"
            ignored = content / "Ignored Episode"
            for folder in (failed, passed, ignored):
                folder.mkdir()
                (folder / "episode.json").write_text(
                    '{"quality":{"mode":"strict"}}', encoding="utf-8")

            (failed / "run_report.json").write_text(json.dumps({
                "schema_version": 1,
                "runs": [{
                    "command": "process",
                    "started_at": "2026-08-04T12:00:00+00:00",
                    "status": "failed",
                    "metadata": {
                        "source": "https://example.com/episode",
                    },
                    "stages": [{
                        "name": "fetch",
                        "status": "failed",
                        "duration_seconds": 4,
                        "error": "HTTP 503",
                        "metrics": {
                            "retry_count": 2,
                            "tls_downgrade": True,
                        },
                    }],
                }],
            }), encoding="utf-8")
            (failed / "quality_report.json").write_text(
                '{"passed":false,"errors":["fetch failed"]}',
                encoding="utf-8",
            )

            (passed / "run_report.json").write_text(json.dumps({
                "schema_version": 1,
                "runs": [{
                    "command": "process",
                    "started_at": "2026-08-05T10:00:00+00:00",
                    "status": "passed",
                    "metadata": {},
                    "stages": [
                        {
                            "name": "fetch",
                            "status": "passed",
                            "duration_seconds": 2,
                            "metrics": {},
                        },
                        {
                            "name": "ai_review",
                            "status": "passed",
                            "duration_seconds": 10,
                            "metrics": {
                                "reported_cost_usd": 1.25,
                                "usage": {
                                    "input_tokens": 100,
                                    "output_tokens": 20,
                                },
                            },
                        },
                    ],
                }],
            }), encoding="utf-8")
            (passed / "quality_report.json").write_text(
                '{"passed":true}', encoding="utf-8")

            (ignored / "run_report.json").write_text(
                '{"schema_version":999,"runs":[]}', encoding="utf-8")

            report = catalog_health.build_health_report(
                content, since="7d", now=now)

        self.assertIn("| fetch | 2 | 1 | 50.0% | 3.0s |", report)
        self.assertIn("- 重试总数：2", report)
        self.assertIn("- TLS 降级次数：1", report)
        self.assertIn("- AI 已报告成本：$1.2500", report)
        self.assertIn("| 1 | example.com |", report)
        self.assertIn("| Failed Episode | fetch | HTTP 503 |", report)


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

    def test_concurrent_run_report_instances_do_not_lose_each_other(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            first = RunReport(folder, "first")
            second = RunReport(folder, "second")
            first.finish(True)
            second.finish(True)
            payload = json.loads(
                (folder / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {run["command"] for run in payload["runs"]},
                {"first", "second"},
            )
            self.assertTrue(all(
                run["status"] == "passed" for run in payload["runs"]))


class AtomicIOTests(unittest.TestCase):
    def test_failed_replace_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "report.json"
            target.write_text("old", encoding="utf-8")
            with patch(
                    "atomic_io.os.replace",
                    side_effect=OSError("simulated failure")):
                with self.assertRaisesRegex(OSError, "simulated failure"):
                    atomic_io.atomic_write_text(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.*")), [])


class ConfigTests(unittest.TestCase):
    def test_tts_config_fails_before_api_use_when_key_is_missing(self):
        with patch.dict(os.environ, {"FISH_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "FISH_KEY"):
                config.validate_for_stage("tts")


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

    def test_long_hero_title_drops_show_suffix_but_keeps_full_metadata(self):
        title = (
            "A Very Long Episode Title About Several Important Topics "
            "Across Technology and Business — Podcast Show with Hosts"
        )
        html = _build_html(
            title,
            [(-1, None, "导览。"), (0, "章节", "正文。")],
            word_count=10,
            date_str="2026-08-10",
            mp3_url="episode.mp3",
        )
        self.assertIn(f"<title>{title} — 讲稿</title>", html)
        self.assertIn(
            ">A Very Long Episode Title About Several Important Topics "
            "Across Technology and Business</h1>",
            html,
        )
        self.assertNotIn(">Podcast Show with Hosts</h1>", html)

    def test_markdown_h1_is_not_repeated_inside_intro(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Episode"
            folder.mkdir()
            md = folder / "讲书稿.md"
            out = folder / "content.html"
            md.write_text(
                "# 重复标题\n\n导览正文。\n\n## 第一章\n章节正文。",
                encoding="utf-8",
            )
            md_to_html(md, out, podcast_title="页面标题")
            html = out.read_text(encoding="utf-8")
        self.assertNotIn("# 重复标题", html)
        self.assertIn("<p>导览正文。</p>", html)

    def test_homepage_source_link_is_not_behind_a_card_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            site = Path(td)
            (site / "index.html").write_text(
                "<!-- STATS:START --><!-- STATS:END -->\n"
                "<!-- CARDS:START --><!-- CARDS:END -->",
                encoding="utf-8",
            )
            (site / "site.json").write_text(json.dumps([{
                "folder": "Episode",
                "path": "episode-12345678",
                "title": "Episode",
                "source_name": "Official source",
                "source_url": "https://example.com/source",
                "duration": 10,
                "words": 1000,
            }]), encoding="utf-8")
            with patch.object(catalog_site, "SITE_DIR", site):
                catalog_site.gen_index()
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
        self.assertRegex(
            html, r"(?s)\.toc-toggle \{[^}]*top: 0\.75rem;[^}]*left: 0\.75rem;")
        self.assertIn(
            "position: fixed;\n"
            "        top: 0.75rem;\n"
            "        left: 4.25rem;\n"
            "        right: 0.75rem;",
            html,
        )
        self.assertRegex(
            html, r"(?s)\.player \{[^}]*margin: 0;[^}]*width: auto;")
        self.assertIn("scroll-margin-top: 10rem;", html)


if __name__ == "__main__":
    unittest.main()
