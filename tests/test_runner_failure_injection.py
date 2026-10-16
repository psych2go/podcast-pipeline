"""Fail-closed execution contracts, using synthetic temporary workspaces only."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import subagent
from scripts.atomic_io import atomic_write_bytes


class ExecutionContractTests(unittest.TestCase):
    def test_old_success_cannot_mask_command_or_content_failure(self):
        for kind, output in (
            ("timeout", subprocess.TimeoutExpired("codex", 1)),
            ("empty_output", ""), ("schema", '{"ok":"wrong"}'),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                old = Path(td) / "ai_review.json"
                old.write_text('{"passed":true}')
                schema = Path(td) / "schema.json"
                schema.write_text(json.dumps({"type": "object", "required": ["ok"],
                                              "properties": {"ok": {"type": "boolean"}}}))
                effect = output if isinstance(output, Exception) else None
                with patch.dict(os.environ, {"SUBAGENT_RUNNER": "pi",
                                             "SUBAGENT_MAX_RETRIES": "0"}, clear=True), \
                        patch.object(subagent, "_runner_commands", return_value=[["codex"]]), \
                        patch.object(subagent, "_runner_environment", return_value={}), \
                        patch.object(subagent, "_run_process", side_effect=effect,
                                     return_value=subprocess.CompletedProcess([], 0, output, "")):
                    with self.assertRaises(subagent.SubagentError) as caught:
                        subagent._run(td, "new task", task_name="ai_review", schema_path=schema)
                metrics = caught.exception.failure_metrics
                self.assertEqual(metrics["failure_kind"], kind)
                self.assertFalse(metrics["content_ok"])
                self.assertEqual(metrics["actual_model"], "unknown")
                self.assertEqual(old.read_text(), '{"passed":true}')

    def test_semantic_reject_is_content_success_not_model_fallback(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {"SUBAGENT_RUNNER": "pi",
                                       "SUBAGENT_MAX_RETRIES": "0",
                                       "SUBAGENT_PI_PTY": "never"}, clear=True), \
                patch.object(subagent, "_runner_commands", return_value=[["pi"]]), \
                patch.object(subagent, "_runner_environment", return_value={}), \
                patch.object(subagent, "_extract_pi_response", return_value='{"passed":false}'), \
                patch.object(subagent, "_run_process", return_value=
                             subprocess.CompletedProcess([], 0, "event", "")) as runner:
            result = subagent._run(
                td, "task", task_name="transcript_correction",
                _model_candidates_override=["a", "b"])
            self.assertEqual(runner.call_count, 1)
            self.assertTrue(result["content_ok"])
            self.assertIsNone(result["thinking"])
            self.assertIsNone(result["fallback_reason"])

    def test_command_and_model_fallback_attribution(self):
        for models in (False, True):
            with self.subTest(models=models), tempfile.TemporaryDirectory() as td, \
                    patch.dict(os.environ, {"SUBAGENT_RUNNER": "pi",
                                           "SUBAGENT_MAX_RETRIES": "0"}, clear=True), \
                    patch.object(subagent, "_runner_commands",
                                 return_value=[["codex"]] if models else [["codex"], ["codex"]]), \
                    patch.object(subagent, "_runner_environment", return_value={}), \
                    patch.object(subagent, "_run_process", side_effect=[
                        subprocess.CompletedProcess([], 0, "", ""),
                        subprocess.CompletedProcess([], 0, "valid", "")]):
                result = subagent._run(td, "task", task_name="test",
                                      _model_candidates_override=["a", "b"] if models else [None])
                self.assertEqual(result["fallback_reason"], "empty_output")
                self.assertEqual(result["attempt_count"], 2)
                self.assertTrue(result["content_ok"])
                self.assertEqual(result["actual_model"], "unknown")


class HostProtocolTests(unittest.TestCase):
    def run_host(self, mutation=None, *, timeout=10, schema=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = [100.0]
            schema_path = root / "schema.json" if schema else None
            if schema_path:
                schema_path.write_text(json.dumps(schema))

            def write(path, data):
                atomic_write_bytes(path, data)
                if path.name != "latest.request.json":
                    return
                request = json.loads(Path(json.loads(data)["request_path"]).read_text())
                self.assertEqual(request["protocol_version"], 1)
                self.assertEqual(request["stage"], "test")
                self.assertEqual(request["allowed_folder"], td)
                self.assertEqual(request["required_capabilities"], ["search"])
                self.assertEqual(request["deadline"], 100 + timeout)
                response = {"id": request["id"],
                            "input_fingerprint": request["input_fingerprint"],
                            "response": '{"ok":true}'}
                if mutation:
                    response = mutation(response, now)
                if response is not None:
                    data = (response if isinstance(response, bytes)
                            else json.dumps(response).encode())
                    atomic_write_bytes(path.parent / f'{request["id"]}.response.json', data)

            with patch.dict(os.environ, {"SUBAGENT_RUNNER": "grokbot"}, clear=True), \
                    patch.object(subagent, "atomic_write_bytes", side_effect=write), \
                    patch.object(subagent.time, "time", side_effect=lambda: now[0]):
                return subagent._run_host_bridge(root, "changed input", task_name="test",
                                                 enable_search=True, timeout=timeout,
                                                 schema_path=schema_path)

    def test_bound_response_success_unknown_model(self):
        result = self.run_host()
        self.assertTrue(result["content_ok"])
        self.assertEqual(result["actual_model"], "unknown")

    def test_wrong_identity_and_old_input_response_fail(self):
        for field in ("id", "input_fingerprint"):
            with self.subTest(field=field):
                def mutate(response, now, field=field):
                    response[field] = "leftover-old-input"
                    return response
                with self.assertRaises(subagent.SubagentError) as caught:
                    self.run_host(mutate)
                self.assertEqual(caught.exception.failure_metrics["failure_kind"],
                                 "host_bridge_mismatch")
                self.assertFalse(caught.exception.failure_metrics["content_ok"])

    def test_expired_response_fails(self):
        def late(response, now):
            now[0] = 111
            return response
        with self.assertRaises(subagent.SubagentError) as caught:
            self.run_host(late)
        self.assertEqual(caught.exception.failure_metrics["failure_kind"], "host_bridge_stale")

    def test_empty_and_schema_failures_are_not_success(self):
        for text, kind, schema in (
            ("", "empty_output", None),
            ('{"ok":"bad"}', "schema", {"type": "object", "properties": {
                "ok": {"type": "boolean"}}}),
        ):
            with self.subTest(kind=kind):
                def invalid(response, now, text=text):
                    response["response"] = text
                    return response
                with self.assertRaises(subagent.SubagentError) as caught:
                    self.run_host(invalid, schema=schema)
                self.assertEqual(caught.exception.failure_metrics["failure_kind"], kind)
                self.assertFalse(caught.exception.failure_metrics["content_ok"])

    def test_partial_json_fails(self):
        with self.assertRaises(subagent.SubagentError) as caught:
            self.run_host(lambda response, now: b'{"id":')
        self.assertEqual(caught.exception.failure_metrics["failure_kind"], "host_bridge_invalid")

    def test_no_consumer_times_out(self):
        with self.assertRaises(subagent.SubagentError) as caught:
            self.run_host(lambda response, now: None, timeout=0.001)
        self.assertEqual(caught.exception.failure_metrics["failure_kind"], "timeout")

    def test_external_and_symlink_roots_refused(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            link = Path(td) / "link"
            link.symlink_to(outside, target_is_directory=True)
            for root in (outside, str(link)):
                with patch.dict(os.environ, {"SUBAGENT_HOST_BRIDGE_DIR": root}, clear=True):
                    with self.assertRaises(subagent.SubagentError):
                        subagent._run_host_bridge(td, "task", task_name="test", timeout=1)


if __name__ == "__main__":
    unittest.main()
