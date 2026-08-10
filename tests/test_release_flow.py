import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import catalog
from release import load_release, prepare_release, update_release_state


class ReleaseStateTests(unittest.TestCase):
    def test_last_successful_state_tracks_only_completed_stages(self):
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

            prepared = prepare_release(folder, mp3, briefing)
            self.assertEqual(prepared["state"], "prepared")
            self.assertEqual(
                prepared["last_successful_state"], "prepared")

            uploaded = update_release_state(folder, "uploaded")
            self.assertEqual(
                uploaded["last_successful_state"], "uploaded")

            failed = update_release_state(
                folder, "failed", error="upload follow-up failed")
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(
                failed["last_successful_state"], "uploaded")

            failed_again = update_release_state(
                folder, "failed", error="retry failed early")
            self.assertEqual(
                failed_again["last_successful_state"], "uploaded")

            deployed = update_release_state(folder, "deployed")
            self.assertEqual(
                deployed["last_successful_state"], "deployed")


class CatalogReleaseFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.content = self.root / "content"
        self.site = self.root / "site"
        self.folder = self.content / "Episode"
        self.folder.mkdir(parents=True)
        self.site.mkdir()
        self.catalog_path = self.content / "播客目录.md"
        (self.folder / "episode.json").write_text(json.dumps({
            "schema_version": 1,
            "slug": "episode-12345678",
            "display_title": "Episode",
            "publish": {"page_path": "episode-12345678"},
        }), encoding="utf-8")
        self.briefing = self.folder / "讲书稿.md"
        self.mp3 = self.folder / "Episode.mp3"
        self.html = self.folder / "Episode - content.html"
        self.briefing.write_text("发布测试讲稿", encoding="utf-8")
        self.mp3.write_bytes(b"x" * 4096)
        self.release = prepare_release(
            self.folder, self.mp3, self.briefing)

    def tearDown(self):
        self.tempdir.cleanup()

    @contextmanager
    def publish_environment(
            self,
            *,
            preflight=True,
            upload=True,
            consistency_errors=None,
            deploy=True,
            verify_report=None,
            config_error=None,
    ):
        verify_report = verify_report or {
            "schema_version": 1,
            "passed": True,
            "errors": [],
            "checks": {},
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                catalog, "CONTENT_DIR", self.content))
            stack.enter_context(patch.object(
                catalog, "SITE_DIR", self.site))
            stack.enter_context(patch.object(
                catalog, "BASE_DIR", self.root))
            stack.enter_context(patch.object(
                catalog, "CATALOG", self.catalog_path))
            stack.enter_context(patch.object(
                catalog, "R2_PUBLIC_URL", "https://audio.example"))
            stack.enter_context(patch.object(
                catalog, "PAGES_BASE_URL", "https://pages.example"))
            stack.enter_context(patch.object(
                catalog,
                "validate_for_stage",
                side_effect=config_error,
            ))
            stack.enter_context(patch.object(
                catalog, "_publish_preflight", return_value=preflight))
            stack.enter_context(patch.object(
                catalog, "_gen_mp3", return_value=self.mp3))
            stack.enter_context(patch.object(catalog, "sync_site"))
            stack.enter_context(patch.object(
                catalog, "rebuild_catalog", return_value=["Episode"]))
            stack.enter_context(patch.object(catalog, "gen_index"))
            stack.enter_context(patch.object(
                catalog,
                "catalog_consistency_errors",
                return_value=list(consistency_errors or []),
            ))
            stack.enter_context(patch.object(
                catalog, "_run", return_value=upload))
            stack.enter_context(patch.object(
                catalog,
                "_run_with_output",
                return_value=(
                    deploy,
                    "https://candidate.podcast-scripts.pages.dev",
                ),
            ))
            stack.enter_context(patch.object(
                catalog,
                "_verify_publish_with_retry",
                return_value=verify_report,
            ))
            yield

    def failure_report(self):
        return json.loads(
            (self.folder / "publish_report.json").read_text(
                encoding="utf-8"))

    def assert_failure(self, stage, last_successful_state):
        report = self.failure_report()
        release = load_release(self.folder)
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed_stage"], stage)
        self.assertTrue(report["error"])
        self.assertEqual(report["release"]["release_id"], release["release_id"])
        self.assertEqual(report["release"]["audio_key"], release["audio_key"])
        self.assertEqual(report["release"]["state"], "failed")
        self.assertEqual(
            report["release"]["last_successful_state"],
            last_successful_state,
        )
        self.assertEqual(release["state"], "failed")
        self.assertEqual(
            release["last_successful_state"], last_successful_state)

    def test_publish_preflight_does_not_reset_failed_release(self):
        update_release_state(self.folder, "uploaded")
        update_release_state(
            self.folder, "failed", error="previous failure")
        before = load_release(self.folder)
        briefing_hash = sha256(self.briefing.read_bytes()).hexdigest()
        self.html.write_text(
            '<meta name="podcast-source-sha256" '
            f'content="{briefing_hash}">'
            f'<audio src="{before["audio_key"]}"></audio>',
            encoding="utf-8",
        )
        ffprobe = SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"codec_name":"mp3","duration":"1"}]}',
        )

        with patch.object(catalog, "CONTENT_DIR", self.content), \
                patch("quality_report.build_quality_report", return_value={
                    "passed": True,
                    "errors": [],
                }), \
                patch("tts.validate_tts_manifest", return_value=[]), \
                patch.object(catalog.subprocess, "run", return_value=ffprobe):
            self.assertTrue(catalog._publish_preflight("Episode"))

        self.assertEqual(load_release(self.folder), before)

    def test_dry_run_accepts_fresh_candidate_without_writing_outputs(self):
        update_release_state(self.folder, "uploaded")
        update_release_state(
            self.folder, "failed", error="previous failure")
        release_before = (self.folder / "release.json").read_bytes()

        with ExitStack() as stack:
            stack.enter_context(patch.object(
                catalog, "CONTENT_DIR", self.content))
            stack.enter_context(patch.object(
                catalog, "SITE_DIR", self.site))
            stack.enter_context(patch.object(
                catalog, "BASE_DIR", self.root))
            stack.enter_context(patch.object(
                catalog, "CATALOG", self.catalog_path))
            stack.enter_context(patch.object(
                catalog, "validate_for_stage"))
            stack.enter_context(patch.object(
                catalog, "_publish_preflight", return_value=True))
            stack.enter_context(patch.object(
                catalog, "_ordered_episode_names", return_value=["Episode"]))
            stack.enter_context(patch.object(
                catalog, "_site_readiness_errors", return_value=[]))
            stack.enter_context(patch.object(
                catalog,
                "episode_stats",
                return_value={"chars": 1000, "duration": 10},
            ))
            consistency = stack.enter_context(patch.object(
                catalog,
                "catalog_consistency_errors",
                side_effect=AssertionError(
                    "dry-run must not validate current site.json"),
            ))
            sync_site = stack.enter_context(patch.object(
                catalog, "sync_site"))
            rebuild = stack.enter_context(patch.object(
                catalog, "rebuild_catalog"))
            gen_index = stack.enter_context(patch.object(
                catalog, "gen_index"))
            stack.enter_context(patch.object(
                catalog, "_run", return_value=True))
            stack.enter_context(patch.object(
                catalog,
                "_run_with_output",
                return_value=(True, "dry-run"),
            ))

            self.assertTrue(catalog.finish("Episode", dry_run=True))

        self.assertEqual(
            (self.folder / "release.json").read_bytes(), release_before)
        self.assertFalse((self.site / "site.json").exists())
        self.assertFalse(self.catalog_path.exists())
        self.assertFalse((self.folder / "publish_report.json").exists())
        consistency.assert_not_called()
        sync_site.assert_not_called()
        rebuild.assert_not_called()
        gen_index.assert_not_called()

    def test_preflight_failure_writes_report_without_advancing_release(self):
        update_release_state(self.folder, "deployed")
        update_release_state(
            self.folder, "failed", error="previous verification failure")
        with self.publish_environment(preflight=False):
            self.assertFalse(catalog.finish("Episode"))
        self.assert_failure("publish_preflight", "deployed")

    def test_r2_failure_records_prepared_as_last_successful_state(self):
        with self.publish_environment(upload=False):
            self.assertFalse(catalog.finish("Episode"))
        self.assert_failure("upload_r2", "prepared")

    def test_catalog_failure_records_uploaded_as_last_successful_state(self):
        with self.publish_environment(
                consistency_errors=["catalog mismatch"]):
            self.assertFalse(catalog.finish("Episode"))
        self.assert_failure("catalog_consistency", "uploaded")

    def test_pages_failure_records_site_ready_as_last_successful_state(self):
        with self.publish_environment(deploy=False):
            self.assertFalse(catalog.finish("Episode"))
        self.assert_failure("deploy_pages", "site_ready")

    def test_remote_failure_preserves_checks_and_deployed_state(self):
        remote_report = {
            "schema_version": 1,
            "passed": False,
            "errors": ["episode unavailable"],
            "checks": {"episode": {"status": 503}},
        }
        with self.publish_environment(verify_report=remote_report):
            self.assertFalse(catalog.finish("Episode"))
        self.assert_failure("verify_publish", "deployed")
        report = self.failure_report()
        self.assertEqual(
            report["checks"]["episode"]["status"], 503)

    def test_unexpected_config_failure_still_writes_publish_report(self):
        update_release_state(self.folder, "uploaded")
        with self.publish_environment(
                config_error=ValueError("invalid publish config")):
            with self.assertRaisesRegex(
                    ValueError, "invalid publish config"):
                catalog.finish("Episode")
        self.assert_failure("config_preflight", "uploaded")

    def test_finish_batch_uploads_all_and_deploys_once(self):
        second = self.content / "Episode Two"
        second.mkdir()
        (second / "episode.json").write_text(json.dumps({
            "schema_version": 1,
            "slug": "episode-two-12345678",
            "display_title": "Episode Two",
            "publish": {"page_path": "episode-two-12345678"},
        }), encoding="utf-8")
        second_briefing = second / "讲书稿.md"
        second_mp3 = second / "Episode Two.mp3"
        second_briefing.write_text("第二期发布测试讲稿", encoding="utf-8")
        second_mp3.write_bytes(b"y" * 4096)
        prepare_release(second, second_mp3, second_briefing)

        verify_report = {
            "schema_version": 1,
            "passed": True,
            "errors": [],
            "checks": {},
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                catalog, "CONTENT_DIR", self.content))
            stack.enter_context(patch.object(
                catalog, "SITE_DIR", self.site))
            stack.enter_context(patch.object(
                catalog, "BASE_DIR", self.root))
            stack.enter_context(patch.object(
                catalog, "CATALOG", self.catalog_path))
            stack.enter_context(patch.object(
                catalog, "R2_PUBLIC_URL", "https://audio.example"))
            stack.enter_context(patch.object(
                catalog, "PAGES_BASE_URL", "https://pages.example"))
            stack.enter_context(patch.object(
                catalog, "validate_for_stage"))
            preflight = stack.enter_context(patch.object(
                catalog, "_publish_preflight", return_value=True))
            stack.enter_context(patch.object(
                catalog, "_candidate_catalog_errors", return_value=[]))
            stack.enter_context(patch.object(
                catalog,
                "_gen_mp3",
                side_effect=lambda folder: (
                    self.mp3 if Path(folder) == self.folder else second_mp3
                ),
            ))
            sync_site = stack.enter_context(patch.object(
                catalog, "sync_site"))
            stack.enter_context(patch.object(
                catalog,
                "rebuild_catalog",
                return_value=["Episode", "Episode Two"],
            ))
            stack.enter_context(patch.object(catalog, "gen_index"))
            stack.enter_context(patch.object(
                catalog,
                "catalog_consistency_errors",
                return_value=[],
            ))
            upload = stack.enter_context(patch.object(
                catalog, "_run", return_value=True))
            deploy = stack.enter_context(patch.object(
                catalog,
                "_run_with_output",
                return_value=(
                    True,
                    "https://candidate.podcast-scripts.pages.dev",
                ),
            ))
            verify = stack.enter_context(patch.object(
                catalog,
                "_verify_publish_with_retry",
                return_value=verify_report,
            ))

            self.assertTrue(catalog.finish_batch(
                ["Episode", "Episode Two"]))

        self.assertEqual(preflight.call_count, 2)
        self.assertEqual(upload.call_count, 2)
        sync_site.assert_called_once_with()
        deploy.assert_called_once()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(load_release(self.folder)["state"], "published")
        self.assertEqual(load_release(second)["state"], "published")


if __name__ == "__main__":
    unittest.main()
