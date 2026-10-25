"""Dual-runner selection, PTY wrap, and transcription model separation."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import subagent


class RunnerSelectionTests(unittest.TestCase):
    def test_auto_selects_grokbot_from_cursor_agent(self):
        with patch.dict(os.environ, {
                "SUBAGENT_RUNNER": "auto", "CURSOR_AGENT": "1",
                "SAND_BOX_BOOT_ID": "", "SUBAGENT_GROKBOT": "",
                "GROK_BOT": ""}, clear=False):
            self.assertEqual(subagent._runner_kind(), "grokbot")

    def test_auto_selects_pi_outside_box(self):
        with patch.dict(os.environ, {
                "SUBAGENT_RUNNER": "auto", "CURSOR_AGENT": "",
                "SAND_BOX_BOOT_ID": "", "SUBAGENT_GROKBOT": "",
                "GROK_BOT": ""}, clear=False), \
                patch.object(subagent, "_in_grokbot_environment", return_value=False):
            self.assertEqual(subagent._runner_kind(), "pi")

    def test_explicit_runner_kinds(self):
        with patch.dict(os.environ, {"SUBAGENT_RUNNER": "pi"}, clear=False):
            self.assertEqual(subagent._runner_kind(), "pi")
        with patch.dict(os.environ, {"SUBAGENT_RUNNER": "grokbot"}, clear=False):
            self.assertEqual(subagent._runner_kind(), "grokbot")
        with patch.dict(os.environ, {"SUBAGENT_RUNNER": "nope"}, clear=False):
            with self.assertRaises(subagent.SubagentError):
                subagent._runner_kind()


class ModelDefaultTests(unittest.TestCase):
    def test_non_transcription_defaults_to_astra_low(self):
        with patch.dict(os.environ, {
                "SUBAGENT_PI_PROVIDER": "", "SUBAGENT_PI_MODEL": "",
                "PI_PROVIDER": "", "PI_MODEL": "",
                "SUBAGENT_PI_THINKING": "", "SUBAGENT_THINKING": "",
                "SUBAGENT_TRANSCRIPTION_MODEL": "",
                "SUBAGENT_CORRECTION_MODEL": ""}, clear=False):
            self.assertEqual(
                subagent._pi_default_model("ai_review"),
                "openai-codex/gpt-6-astra")
            self.assertEqual(subagent._pi_thinking_level("ai_review"), "low")
            self.assertEqual(
                subagent._pi_default_model("content_writing"),
                "openai-codex/gpt-6-astra")

    def test_transcription_does_not_force_astra(self):
        with patch.dict(os.environ, {
                "SUBAGENT_PI_PROVIDER": "", "SUBAGENT_PI_MODEL": "",
                "PI_PROVIDER": "", "PI_MODEL": "",
                "SUBAGENT_TRANSCRIPTION_MODEL": "",
                "SUBAGENT_CORRECTION_MODEL": "",
                "SUBAGENT_PI_THINKING": "", "SUBAGENT_THINKING": ""}, clear=False):
            self.assertIsNone(subagent._pi_default_model("transcript_correction"))
            self.assertIsNone(
                subagent._pi_default_model("transcript_correction_3"))
            self.assertIsNone(subagent._pi_thinking_level("transcript_correction"))

    def test_transcription_honors_dedicated_model_knob(self):
        with patch.dict(os.environ, {
                "SUBAGENT_TRANSCRIPTION_MODEL": "gpt-5.6-sol",
                "SUBAGENT_PI_PROVIDER": "openai-codex",
                "SUBAGENT_PI_MODEL": "gpt-6-astra"}, clear=False):
            self.assertEqual(
                subagent._pi_default_model("transcript_correction_1"),
                "openai-codex/gpt-5.6-sol")

    def test_pi_command_includes_thinking(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "s.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            cmd = subagent._pi_command(
                ["pi"], task="t", schema_path=schema, write_files=False,
                enable_search=False, model="openai-codex/gpt-6-astra",
                thinking="low")
            self.assertEqual(cmd[cmd.index("--thinking") + 1], "low")
            self.assertEqual(cmd[cmd.index("--model") + 1],
                             f"{subagent._pi_provider()}/gpt-6-astra")


class PtyWrapTests(unittest.TestCase):
    def test_needs_pty_for_grokbot_auto(self):
        with patch.dict(os.environ, {
                "SUBAGENT_RUNNER": "grokbot", "SUBAGENT_PI_PTY": "auto"},
                clear=False):
            self.assertTrue(subagent._needs_pi_pty())

    def test_pty_never_disables(self):
        with patch.dict(os.environ, {
                "SUBAGENT_RUNNER": "grokbot", "SUBAGENT_PI_PTY": "never"},
                clear=False):
            self.assertFalse(subagent._needs_pi_pty())

    def test_wrap_with_pty_prefixes_wrapper(self):
        wrapped = subagent._wrap_with_pty(["/bin/pi", "-p"])
        self.assertEqual(wrapped[-2:], ["/bin/pi", "-p"])
        self.assertIn("run_with_pty.py", wrapped[1])


class HostBridgeTests(unittest.TestCase):
    def test_host_bridge_timeout_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "SUBAGENT_RUNNER": "grokbot",
                "SUBAGENT_GROKBOT_BACKEND": "host",
                "SUBAGENT_HOST_BRIDGE_DIR": tmp,
                "SUBAGENT_HOST_BRIDGE_POLL": "0.01",
                "SUBAGENT_TIMEOUT": "0"}, clear=False):
            with self.assertRaises(subagent.SubagentError) as raised:
                subagent._run(tmp, "task", task_name="ai_review", timeout=0)
            self.assertEqual(
                raised.exception.failure_metrics["failure_kind"], "timeout")

    def test_host_bridge_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Path(tmp) / "bridge"
            schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                      "required": ["ok"], "additionalProperties": False}
            schema_path = Path(tmp) / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

            def fill_bad_response(folder, task, **kwargs):
                return {
                    "response": '{"ok":"nope"}',
                    "duration_ms": 1,
                    "retry_count": 0,
                    "runner_index": 0,
                    "command": "host-bridge",
                    "model": "x",
                    "task_name": kwargs["task_name"],
                }
            with patch.dict(os.environ, {
                    "SUBAGENT_RUNNER": "grokbot",
                    "SUBAGENT_GROKBOT_BACKEND": "host",
                    "SUBAGENT_HOST_BRIDGE_DIR": str(bridge),
                    "SUBAGENT_HOST_BRIDGE_POLL": "0.01",
                    "SUBAGENT_MODEL": "",
                    "SUBAGENT_MODEL_CHAIN": "",
                    "SUBAGENT_AI_REVIEW_MODEL_CHAIN": ""}, clear=False), \
                    patch.object(subagent, "_run_host_bridge",
                                 side_effect=fill_bad_response):
                with self.assertRaises(subagent.SubagentError) as raised:
                    subagent._run(
                        tmp, "task", task_name="ai_review",
                        schema_path=schema_path)
                self.assertEqual(
                    raised.exception.failure_metrics["failure_kind"], "schema")

    def test_host_bridge_success_returns_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Path(tmp) / "bridge"
            bridge.mkdir()

            def serve(folder, task, **kwargs):
                return {
                    "response": '{"ok":true}',
                    "duration_ms": 2,
                    "retry_count": 0,
                    "runner_index": 0,
                    "command": "host-bridge",
                    "model": "host",
                    "task_name": kwargs["task_name"],
                }

            schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                      "required": ["ok"], "additionalProperties": False}
            schema_path = Path(tmp) / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            with patch.dict(os.environ, {
                    "SUBAGENT_RUNNER": "grokbot",
                    "SUBAGENT_GROKBOT_BACKEND": "host",
                    "SUBAGENT_HOST_BRIDGE_DIR": str(bridge)}, clear=False), \
                    patch.object(subagent, "_run_host_bridge", side_effect=serve):
                result = subagent._run(
                    tmp, "task", task_name="ai_review", schema_path=schema_path)
                self.assertEqual(result["response"], '{"ok":true}')


class QualityStillFailClosedTests(unittest.TestCase):
    def test_empty_pi_output_still_falls_back(self):
        # Preserve prior invariant under dual-runner defaults.
        from types import SimpleNamespace

        def event(text=None, **extra):
            message = {"role": "assistant", "content": [] if text is None else [
                {"type": "text", "text": text}], **extra}
            return json.dumps({"type": "message_end", "message": message},
                              ensure_ascii=False)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "SUBAGENT_COMMAND": "pi",
                "SUBAGENT_FALLBACK_COMMAND": "codex",
                "SUBAGENT_MAX_RETRIES": "0",
                "SUBAGENT_RUNNER": "pi",
                "SUBAGENT_PI_PTY": "never",
                "SUBAGENT_PI_MODEL": "",
                "SUBAGENT_PI_PROVIDER": "",
                "SUBAGENT_MODEL": ""}, clear=False), \
                patch.object(subagent.shutil, "which",
                             side_effect=lambda name: "/bin/" + name), \
                patch.object(subagent, "_runner_environment", return_value={}), \
                patch.object(subagent, "_run_process", side_effect=[
                    SimpleNamespace(returncode=0, stdout=event(), stderr=""),
                    SimpleNamespace(returncode=0, stdout="codex result",
                                    stderr=""),
                ]):
            result = subagent._run(tmp, "task", task_name="ai_review")
            self.assertEqual(result["response"], "codex result")
            self.assertEqual(result["runner_index"], 1)


if __name__ == "__main__":
    unittest.main()
