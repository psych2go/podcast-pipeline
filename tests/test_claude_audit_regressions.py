import json
import os
import ssl
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_pipeline
import catalog_publish as catalog
import catalog_site
import fetcher
import process as pipeline_process
import setup_alignment_env
import subagent
from content_map import coverage_report
from html_gen import _build_html, parse_sections
from quality_report import _ai_fact_check_consistency, build_quality_report
from retry import retry_after_seconds
from episode import set_claim_evidence_mode
import tts
import check_public_repo
import release
from validator import normalize_briefing_artifacts


class HtmlInjectionRegressionTests(unittest.TestCase):
    def test_episode_page_escapes_attribute_quotes_and_rejects_script_audio_url(self):
        title = 'Bad" onmouseover="alert(1)'
        html = _build_html(
            title,
            parse_sections("导览。\n\n## 第一章\n正文。"),
            mp3_url="javascript:alert(1)",
        )
        self.assertIn("Bad&quot; onmouseover=&quot;alert(1)", html)
        self.assertNotIn('src="javascript:', html)
        self.assertNotIn('href="javascript:', html)

    def test_catalog_cards_escape_quotes_and_reject_script_source_url(self):
        with tempfile.TemporaryDirectory() as td:
            site = Path(td)
            (site / "site.json").write_text(json.dumps([{
                "folder": "episode",
                "path": "episode-1234",
                "title": 'Bad" onmouseover="alert(1)',
                "source_name": 'Source" autofocus="x',
                "source_url": "javascript:alert(1)",
                "duration": 10,
                "words": 1000,
            }]), encoding="utf-8")
            (site / "index.html").write_text(
                "<!-- STATS:START --><!-- STATS:END -->\n"
                "<!-- CARDS:START --><!-- CARDS:END -->",
                encoding="utf-8",
            )
            with patch.object(catalog_site, "SITE_DIR", site):
                catalog_site.gen_index()
            rendered = (site / "index.html").read_text(encoding="utf-8")
        self.assertIn("&quot;", rendered)
        self.assertNotIn('data-search="bad" onmouseover="', rendered.lower())
        self.assertNotIn('href="javascript:', rendered.lower())


class FetchSecurityRegressionTests(unittest.TestCase):
    @staticmethod
    def _fake_curl_module(get):
        module = types.ModuleType("curl_cffi")
        module.requests = types.SimpleNamespace(get=get)
        return module

    def test_tls_downgrade_requires_actual_podscripts_hostname(self):
        calls = []

        def get(*_args, **_kwargs):
            calls.append(_kwargs)
            if len(calls) == 1:
                raise ssl.SSLCertVerificationError(1, "expired")
            return types.SimpleNamespace(status_code=200, text="x" * 1200)

        with patch.dict(sys.modules, {
                "curl_cffi": self._fake_curl_module(get)}):
            html, metadata = fetcher._try_curl_cffi_with_metadata(
                "https://evil.example/?ref=podscripts.co")
        self.assertIsNone(html)
        self.assertEqual(len(calls), 1)
        self.assertFalse(metadata.get("tls_downgrade", False))

    def test_tls_downgrade_still_allows_real_podscripts_hostname(self):
        calls = []

        def get(*_args, **_kwargs):
            calls.append(_kwargs)
            if len(calls) == 1:
                raise ssl.SSLCertVerificationError(1, "expired")
            return types.SimpleNamespace(status_code=200, text="x" * 1200)

        with patch.dict(sys.modules, {
                "curl_cffi": self._fake_curl_module(get)}):
            html, metadata = fetcher._try_curl_cffi_with_metadata(
                "https://www.podscripts.co/podcasts/example")
        self.assertEqual(html, "x" * 1200)
        self.assertEqual(len(calls), 2)
        self.assertTrue(metadata["tls_downgrade"])

    def test_terminal_404_does_not_retry_transport_chain(self):
        fetcher._HTML_CACHE.clear()
        with patch(
                "fetcher._try_curl_cffi_with_metadata",
                return_value=(None, {"status_code": 404})) as cffi, \
                patch("fetcher._try_curl", return_value=None), \
                patch(
                    "fetcher._try_httpx_with_metadata",
                    return_value=(None, {"status_code": 404})), \
                patch("fetcher.FETCH_MAX_RETRIES", 3), \
                patch("fetcher.time.sleep") as sleep:
            html, metadata = fetcher._fetch_html(
                "https://example.com/missing")
        self.assertIsNone(html)
        self.assertEqual(cffi.call_count, 1)
        self.assertEqual(metadata["fetch_retry_count"], 0)
        sleep.assert_not_called()

    def test_retry_after_is_capped(self):
        self.assertEqual(retry_after_seconds("5"), 5)
        self.assertEqual(retry_after_seconds("86400"), 300)


class SubagentIsolationRegressionTests(unittest.TestCase):
    def test_runner_environment_exposes_provider_key_but_not_pipeline_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_home = root / "codex-home"
            source_home.mkdir()
            (source_home / "config.toml").write_text(
                'model_provider="custom"\n'
                '[model_providers.custom]\n'
                'name="custom"\n'
                'base_url="https://provider.example/v1"\n'
                'env_key="ZAI_API_KEY"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {
                "CODEX_HOME": str(source_home),
                "PATH": os.environ.get("PATH", "/usr/bin"),
                "ZAI_API_KEY": "provider-secret",
                "FISH_KEY": "tts-secret",
                "HF_TOKEN": "hf-secret",
                "CLOUDFLARE_API_TOKEN": "cf-secret",
            }, clear=False):
                env = subagent._runner_environment(
                    root / "tmp", ["/usr/bin/codex", "exec"])
        self.assertEqual(env["ZAI_API_KEY"], "provider-secret")
        self.assertIn("PATH", env)
        self.assertNotIn("FISH_KEY", env)
        self.assertNotIn("HF_TOKEN", env)
        self.assertNotIn("CLOUDFLARE_API_TOKEN", env)


class EvidenceAndQualityRegressionTests(unittest.TestCase):
    def test_external_fact_used_as_fact_requires_supported_verdict_and_url(self):
        review = {
            "schema_version": 3,
            "fact_checks": [{
                "subclaim_id": "U0001-C01-F01",
                "parent_claim_id": "U0001-C01",
                "claim": "External objective fact",
                "claim_type": "public_fact",
                "claim_origin": "external_source",
                "speaker_role": "not_applicable",
                "assertion_type": "fact",
                "verification_mode": "web_required",
                "risk_domain": "general",
                "verdict": "uncertain",
                "publication_status": "used_as_fact",
                "evidence_segment_ids": [],
                "source_urls": [],
                "checked_at": "2026-08-15T00:00:00Z",
                "notes": "",
            }],
        }
        errors, _warnings = _ai_fact_check_consistency(
            review, valid_claim_ids={"U0001-C01"})
        self.assertTrue(any("supported" in error for error in errors), errors)
        self.assertTrue(any("网页来源" in error for error in errors), errors)

    def test_speaker_reported_fact_cannot_be_unattributed_fact(self):
        review = {
            "schema_version": 3,
            "fact_checks": [{
                "claim": "A guest reports a third-party fact",
                "parent_claim_id": "U0001-C01",
                "subclaim_id": "U0001-C01-F01",
                "claim_type": "not_applicable",
                "claim_origin": "speaker_reported",
                "speaker_role": "guest",
                "assertion_type": "fact",
                "verification_mode": "transcript_attribution",
                "risk_domain": "general",
                "verdict": "accurately_reported",
                "publication_status": "used_as_fact",
                "evidence_segment_ids": ["S0001"],
                "source_urls": [],
                "checked_at": "2026-08-15T00:00:00Z",
                "notes": "",
            }],
        }
        errors, _warnings = _ai_fact_check_consistency(
            review, valid_claim_ids={"U0001-C01"})
        self.assertTrue(any("必须明确归因" in error for error in errors), errors)

    def test_excluded_high_unit_with_reason_is_not_missing(self):
        content_map = {
            "units": [{
                "id": "U0001",
                "importance": "high",
                "status": "excluded",
                "notes": "重复内容，已在其他单元覆盖",
                "claims": [],
            }],
        }
        result = coverage_report(content_map, {"chapters": []})
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["high_missing"], [])
        self.assertEqual(result["high_total"], 0)
        self.assertEqual(result["high_coverage"], 1.0)

    def test_generic_normalizer_does_not_inject_episode_specific_fact(self):
        fixed, _summary, changes = normalize_briefing_artifacts(
            "受访者把自主武器指令称为 3009。",
            {"chapters": []},
        )
        self.assertNotIn("正式编号为", fixed)
        self.assertNotIn("三零零零点零九", fixed)
        self.assertNotIn("normalized_known_terms", changes)

    def test_degraded_claim_evidence_is_blocked_by_quality_report(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "transcript.raw.json").write_text(json.dumps({
                "source_kind": "web_transcript",
                "segments": [],
                "meta": {},
            }), encoding="utf-8")
            (folder / "content_map.json").write_text(json.dumps({
                "schema_version": 3,
                "units": [],
                "claim_evidence_refiner": {
                    "command": "codex-subagent+deterministic-fallback",
                    "fallback_claim_count": 1,
                },
            }), encoding="utf-8")
            report = build_quality_report(folder)
        self.assertTrue(any(
            "严格发布禁止降级证据" in error
            for error in report["errors"]
        ), report["errors"])

    def test_new_episode_cannot_enable_legacy_evidence_mode(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "只读"):
                set_claim_evidence_mode(Path(td), "legacy_broad")

    def test_existing_source_metadata_is_upgraded_to_v8(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            source = "https://example.com/episode"
            text = "existing evidence " * 30
            metadata = pipeline_process._prepare_evidence_metadata({
                "source": source,
                "source_kind": "web_transcript",
                "segments": [{"start": 0, "end": 1, "text": text}],
                "meta": {"timestamped": True},
            }, text)
            (folder / "原始转录.txt").write_text(text, encoding="utf-8")
            (folder / "transcript.raw.json").write_text(
                json.dumps(metadata), encoding="utf-8")
            (folder / "来源.md").write_text(
                "# 来源信息\n\n## 处理信息\n- pipeline 版本：v7\n",
                encoding="utf-8",
            )
            self.assertTrue(pipeline_process.fetch_transcript(
                source, folder, "Episode", None))
            source_text = (folder / "来源.md").read_text(encoding="utf-8")
        self.assertIn("- pipeline 版本：v8", source_text)
        self.assertNotIn("- pipeline 版本：v7", source_text)


class ContractRegressionTests(unittest.TestCase):
    def test_correction_prompt_uses_full_tls_downgrade_policy(self):
        prompt = agent_pipeline._correction_prompt("web_transcript")
        self.assertIn("tls_downgrade=true", prompt)
        self.assertIn("不要修改 原始转录.txt", prompt)

    def test_finish_batch_appends_run_report_for_each_episode(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(catalog, "CONTENT_DIR", Path(td)), \
                patch.object(catalog, "_finish_batch_impl", return_value=True):
            self.assertTrue(catalog.finish_batch(["A", "B"]))
            for name in ("A", "B"):
                payload = json.loads(
                    (Path(td) / name / "run_report.json").read_text(
                        encoding="utf-8"))
                self.assertEqual(
                    payload["runs"][-1]["command"],
                    "catalog.finish-batch",
                )
                self.assertEqual(payload["runs"][-1]["status"], "passed")

    def test_detached_head_without_ci_branch_context_is_unknown(self):
        with patch("check_public_repo.current_branch", return_value=""), \
                patch.dict(os.environ, {
                    "GITHUB_HEAD_REF": "",
                    "GITHUB_REF_NAME": "",
                }, clear=False):
            self.assertEqual(check_public_repo.branch_context(), "")
        self.assertTrue(check_public_repo.is_private_branch("private/test"))
        self.assertTrue(check_public_repo.is_private_branch("origin/private-test"))
        self.assertFalse(check_public_repo.is_private_branch("main"))
        with patch("check_public_repo.branch_context", return_value=""), \
                patch("check_public_repo.tracked_files", return_value=[]), \
                patch("check_public_repo.find_violations", return_value=[]), \
                patch.object(sys, "argv", ["check_public_repo.py"]):
            self.assertEqual(check_public_repo.main(), 3)


class RuntimeHardeningRegressionTests(unittest.TestCase):
    def test_release_provenance_includes_ci_and_optional_requirements(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=root, check=True)
            (root / "content" / "Episode").mkdir(parents=True)
            (root / "scripts").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "scripts" / "pipeline.py").write_text(
                "print('ok')\n", encoding="utf-8")
            requirement = root / "requirements-asr-gpu.txt"
            requirement.write_text("package==1\n", encoding="utf-8")
            workflow = root / ".github" / "workflows" / "ci.yml"
            workflow.write_text("name: CI\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "baseline"], cwd=root, check=True)
            baseline = release._git_provenance(root / "content" / "Episode")
            requirement.write_text("package==2\n", encoding="utf-8")
            changed_requirement = release._git_provenance(
                root / "content" / "Episode")
            requirement.write_text("package==1\n", encoding="utf-8")
            workflow.write_text("name: Hardened CI\n", encoding="utf-8")
            changed_workflow = release._git_provenance(
                root / "content" / "Episode")
        self.assertNotEqual(
            baseline["git_diff_sha256"],
            changed_requirement["git_diff_sha256"],
        )
        self.assertNotEqual(
            baseline["git_diff_sha256"],
            changed_workflow["git_diff_sha256"],
        )

    def test_long_title_is_truncated_on_utf8_boundary(self):
        title = pipeline_process.sanitize_title("播客" * 200)
        self.assertLessEqual(len(title.encode("utf-8")), 180)
        self.assertTrue(title)

    def test_tts_metrics_failure_does_not_flip_completed_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "tts_manifest.json").write_text(json.dumps({
                "config": {},
                "sections": [],
            }), encoding="utf-8")
            with patch(
                    "process.build_tts_plan",
                    side_effect=RuntimeError("bad manifest")):
                self.assertEqual(
                    pipeline_process._tts_metrics(folder, "讲书稿.md"), {})

    def test_tts_removes_atomic_hidden_temp_files(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            audio = folder / "audio"
            audio.mkdir()
            stale = audio / ".01_section.mp3.random.tmp.mp3"
            stale.write_bytes(b"partial")
            with patch("tts.validate_for_stage"):
                result = tts.run_tts(
                    str(folder), "missing.md", "episode", concurrency=1)
            self.assertFalse(result.ok)
            self.assertFalse(stale.exists())


class AlignmentSetupRegressionTests(unittest.TestCase):
    def test_setup_downloads_required_punkt_tab_resource(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "alignment"
            (target / "bin").mkdir(parents=True)
            (target / "bin" / "python").touch()
            (target / "bin" / "pip").touch()
            site_packages = target / "lib" / "site-packages"
            calls = []
            with patch.object(
                    setup_alignment_env, "run",
                    side_effect=lambda command: calls.append(command)), \
                    patch.object(
                        setup_alignment_env.subprocess,
                        "check_output",
                        return_value=str(site_packages)), \
                    patch.object(
                        sys, "argv",
                        ["setup_alignment_env.py", "--env", str(target)]):
                setup_alignment_env.main()
        self.assertIn([
            str(target / "bin" / "python"),
            "-m", "nltk.downloader", "punkt_tab",
        ], calls)


if __name__ == "__main__":
    unittest.main()
