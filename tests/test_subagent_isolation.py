import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import subagent


class SubagentIsolationTests(unittest.TestCase):
    def test_codex_runner_preserves_provider_but_removes_hooks_and_mcps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            episode = root / "episode"
            episode.mkdir()
            source_home = root / "source-codex"
            source_home.mkdir()
            (source_home / "auth.json").write_text(
                '{"token":"test"}', encoding="utf-8")
            (source_home / "catalog.json").write_text(
                '{"models":[]}', encoding="utf-8")
            (source_home / "config.toml").write_text(
                'model_provider="custom"\n'
                'model="gpt-test"\n'
                'model_catalog_json="catalog.json"\n'
                'model_reasoning_effort="high"\n'
                '[model_providers.custom]\n'
                'name="Custom"\n'
                'base_url="https://provider.example/v1"\n'
                'wire_api="responses"\n'
                'requires_openai_auth=true\n'
                '[features]\n'
                'hooks=true\n'
                '[mcp_servers.unwanted]\n'
                'command="claudit"\n'
                '[hooks.state.bad]\n'
                'trusted_hash="secret"\n'
                '[projects."/tmp"]\n'
                'trust_level="trusted"\n',
                encoding="utf-8",
            )
            observed = {}

            def fake_process(cmd, *, cwd, env, timeout):
                observed["env"] = env
                isolated = Path(env["CODEX_HOME"])
                observed["auth"] = (
                    (isolated / "auth.json").read_text(encoding="utf-8")
                    if (isolated / "auth.json").exists() else None
                )
                observed["config"] = tomllib.loads(
                    (isolated / "config.toml").read_text(encoding="utf-8"))
                observed["catalog"] = (
                    isolated / "catalog.json").read_text(encoding="utf-8")
                observed["config_mode"] = (
                    isolated / "config.toml").stat().st_mode & 0o777
                return type("Result", (), {
                    "returncode": 0,
                    "stdout": '{"ok": true}',
                    "stderr": "",
                })()

            with patch.dict(os.environ, {
                    "CODEX_HOME": str(source_home),
                    "SUBAGENT_COMMAND": "codex",
                    "SUBAGENT_MAX_RETRIES": "0",
                }, clear=False), \
                    patch("subagent.shutil.which", return_value="/bin/codex"), \
                    patch("subagent._run_process", side_effect=fake_process):
                subagent._run(
                    episode,
                    "return JSON",
                    task_name="isolated_home",
                )

            isolated = Path(observed["env"]["CODEX_HOME"])
            self.assertNotEqual(isolated, source_home)
            self.assertEqual(observed["auth"], '{"token":"test"}')
            config = observed["config"]
            self.assertEqual(config["model_provider"], "custom")
            self.assertEqual(config["model"], "gpt-test")
            self.assertEqual(
                config["model_providers"]["custom"]["base_url"],
                "https://provider.example/v1",
            )
            self.assertFalse(config["features"]["hooks"])
            self.assertFalse(config["features"]["plugins"])
            self.assertFalse(config["features"]["remote_plugin"])
            self.assertFalse(config["features"]["workspace_dependencies"])
            self.assertNotIn("mcp_servers", config)
            self.assertNotIn("hooks", config)
            self.assertNotIn("projects", config)
            self.assertEqual(observed["catalog"], '{"models":[]}')
            self.assertEqual(observed["config_mode"], 0o600)

    def test_codex_profile_is_sanitized_and_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_home = root / "source-codex"
            source_home.mkdir()
            (source_home / "auth.json").write_text(
                '{"token":"test"}', encoding="utf-8")
            (source_home / "batch.config.toml").write_text(
                'model="gpt-profile"\n'
                '[features]\nhooks=true\n'
                '[mcp_servers.bad]\ncommand="bad"\n',
                encoding="utf-8",
            )
            isolated = root / "isolated"
            isolated.mkdir()

            subagent._copy_sanitized_codex_config(
                source_home,
                isolated,
                ["/bin/codex", "exec", "--profile", "batch"],
            )

            profile = tomllib.loads(
                (isolated / "batch.config.toml").read_text(encoding="utf-8"))
            self.assertEqual(profile["model"], "gpt-profile")
            self.assertFalse(profile["features"]["hooks"])
            self.assertFalse(profile["features"]["plugins"])
            self.assertNotIn("mcp_servers", profile)

    def test_process_timeout_terminates_the_process_group(self):
        process = MagicMock()
        process.pid = 123
        process.communicate.side_effect = [
            subagent.subprocess.TimeoutExpired(["codex"], 1),
            ("partial", "timed out"),
        ]
        process.poll.return_value = None

        with patch("subagent.subprocess.Popen", return_value=process), \
                patch("subagent.os.getpgid", return_value=456), \
                patch("subagent.os.killpg") as killpg:
            with self.assertRaises(subagent.subprocess.TimeoutExpired):
                subagent._run_process(
                    ["codex"], cwd=Path("."), env={}, timeout=1)

        killpg.assert_any_call(456, subagent.signal.SIGTERM)

    def test_legacy_call_only_stages_files_named_in_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            named_input = folder / "input.txt"
            unrelated = folder / "secret.txt"
            output = folder / "output.md"
            named_input.write_text("source evidence", encoding="utf-8")
            unrelated.write_text("must not be staged", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                workspace = Path(workspace)
                self.assertTrue((workspace / "input.txt").exists())
                self.assertFalse((workspace / "secret.txt").exists())
                (workspace / "output.md").write_text(
                    "generated content", encoding="utf-8")
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                result = subagent.run_edit_task(
                    folder,
                    "Read input.txt and write output.md.",
                    task_name="legacy_compatibility",
                    allowed_files=[output],
                )

            self.assertEqual(result["input_files"], ["input.txt"])
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "generated content",
            )

    def test_allowed_output_is_committed_from_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            source = folder / "input.txt"
            output = folder / "output.md"
            source.write_text("source evidence", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                workspace = Path(workspace)
                self.assertNotEqual(workspace, folder)
                self.assertEqual(
                    (workspace / "input.txt").read_text(encoding="utf-8"),
                    "source evidence",
                )
                (workspace / "output.md").write_text(
                    "generated content", encoding="utf-8")
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                result = subagent.run_edit_task(
                    folder,
                    "Read input.txt and write output.md.",
                    task_name="isolation_success",
                    input_files=[source],
                    allowed_files=[output],
                    required_files=[output],
                )

            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "generated content",
            )
            self.assertEqual(result["committed_files"], ["output.md"])
            self.assertEqual(source.read_text(encoding="utf-8"), "source evidence")

    def test_unauthorized_new_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            source = folder / "input.txt"
            output = folder / "output.md"
            source.write_text("source evidence", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                workspace = Path(workspace)
                (workspace / "output.md").write_text(
                    "generated content", encoding="utf-8")
                (workspace / "rogue.txt").write_text(
                    "unauthorized", encoding="utf-8")
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                with self.assertRaisesRegex(
                        subagent.SubagentError, "未允许的文件"):
                    subagent.run_edit_task(
                        folder,
                        "Read input.txt and write output.md.",
                        task_name="isolation_rogue",
                        input_files=[source],
                        allowed_files=[output],
                        required_files=[output],
                    )

            self.assertFalse(output.exists())
            self.assertFalse((folder / "rogue.txt").exists())
            self.assertEqual(source.read_text(encoding="utf-8"), "source evidence")

    def test_input_tampering_is_rejected_without_real_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            source = folder / "input.txt"
            output = folder / "output.md"
            source.write_text("source evidence", encoding="utf-8")
            output.write_text("previous output", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                workspace = Path(workspace)
                (workspace / "input.txt").write_text(
                    "tampered evidence", encoding="utf-8")
                (workspace / "output.md").write_text(
                    "generated content", encoding="utf-8")
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                with self.assertRaisesRegex(
                        subagent.SubagentError, "input_files"):
                    subagent.run_edit_task(
                        folder,
                        "Read input.txt and update output.md.",
                        task_name="isolation_tamper",
                        input_files=[source],
                        allowed_files=[output],
                        required_files=[output],
                    )

            self.assertEqual(source.read_text(encoding="utf-8"), "source evidence")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous output")

    def test_remove_missing_outputs_does_not_seed_stale_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "episode"
            folder.mkdir()
            output = folder / "optional.txt"
            output.write_text("stale", encoding="utf-8")

            def fake_run(workspace, task, **kwargs):
                self.assertFalse((Path(workspace) / "optional.txt").exists())
                return {"response": "done"}

            with patch("subagent._run", side_effect=fake_run):
                result = subagent.run_edit_task(
                    folder,
                    "Delete optional.txt when no replacement is needed.",
                    task_name="remove_stale_output",
                    allowed_files=[output],
                    remove_missing_outputs=True,
                )

            self.assertFalse(output.exists())
            self.assertEqual(result["removed_files"], ["optional.txt"])


if __name__ == "__main__":
    unittest.main()
