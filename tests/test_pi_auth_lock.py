import json
import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import subagent
from scripts.subagent import (
    PiAuthError,
    _is_auth_failure,
    _model_fallback_allowed,
    _pi_auth_refresh_under_lock,
)


def write_expiry(path, expires_ms):
    path.write_text(json.dumps({"openai": {"type": "oauth", "expires": expires_ms}}))


def ok_runner(calls):
    def run(cmd):
        calls.append(cmd)
        return types.SimpleNamespace(stdout="OK", stderr="", returncode=0)

    return run


class AuthPatternTests(unittest.TestCase):
    def test_auth_patterns_detected(self):
        for text in [
            "Error: OAuth refresh failed for openai",
            '{"error":"invalid_grant"}',
            "refresh token has already been used",
            "Not authenticated. Use /login to sign in",
            "HTTP 401 Unauthorized",
        ]:
            with self.subTest(text=text):
                self.assertTrue(_is_auth_failure("", text))

    def test_non_auth_not_detected(self):
        for text in ["usage limit reached", "run id 4291abc", "", "OK"]:
            with self.subTest(text=text):
                self.assertFalse(_is_auth_failure(text))

    def test_auth_error_not_model_fallback(self):
        self.assertFalse(
            _model_fallback_allowed(PiAuthError("x", metrics={"failure_kind": "authentication"}))
        )


class PiAuthLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.auth = self.tmp_path / "auth.json"
        env = mock.patch.dict(
            os.environ,
            {"SUBAGENT_PI_AUTH_FILE": str(self.auth), "SUBAGENT_PI_PROVIDER": "openai"},
        )
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SUBAGENT_PI_AUTH_LOCK", None)

    def test_fresh_token_skips_probe(self):
        write_expiry(self.auth, int(time.time() * 1000) + 3600_000)
        calls = []
        self.assertEqual(_pi_auth_refresh_under_lock(["pi"], runner=ok_runner(calls)), "fresh")
        self.assertEqual(calls, [])

    def test_expiring_token_probes_once(self):
        write_expiry(self.auth, int(time.time() * 1000) + 60_000)
        calls = []
        self.assertEqual(
            _pi_auth_refresh_under_lock(["pi"], runner=ok_runner(calls)), "refreshed"
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("--provider", calls[0])
        self.assertIn("openai", calls[0])

    def test_probe_auth_failure_raises(self):
        write_expiry(self.auth, 0)

        def bad(cmd):
            return types.SimpleNamespace(
                stdout="", stderr="OAuth refresh failed: invalid_grant", returncode=1
            )

        with self.assertRaises(PiAuthError):
            _pi_auth_refresh_under_lock(["pi"], runner=bad)

    def test_lock_disabled(self):
        with mock.patch.dict(os.environ, {"SUBAGENT_PI_AUTH_LOCK": "off"}):
            self.assertEqual(_pi_auth_refresh_under_lock(["pi"], runner=None), "disabled")

    def test_concurrent_refresh_is_serialized(self):
        write_expiry(self.auth, 0)
        active, peak, lock = [0], [0], threading.Lock()

        def slow(cmd):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with lock:
                active[0] -= 1
            # simulate the winner refreshing the token
            write_expiry(self.auth, int(time.time() * 1000) + 3600_000)
            return types.SimpleNamespace(stdout="OK", stderr="", returncode=0)

        results = []

        def worker():
            results.append(_pi_auth_refresh_under_lock(["pi"], runner=slow))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(peak[0], 1)
        self.assertEqual(sorted(results), ["fresh", "fresh", "fresh", "refreshed"])

    def test_run_task_raises_auth_error_on_empty_output(self):
        env = {
            "SUBAGENT_PI_AUTH_LOCK": "off",
            "SUBAGENT_RUNNER": "pi",
            "SUBAGENT_COMMAND": "pi",
        }

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, 0, "", "OAuth refresh failed: refresh token has already been used"
            )

        with mock.patch.dict(os.environ, env), mock.patch.object(
            subagent, "_run_process", fake_run
        ), mock.patch.object(subagent.time, "sleep", lambda s: None), mock.patch.object(
            subagent.shutil, "which", lambda name: "/usr/bin/" + name, create=True
        ):
            with self.assertRaises(PiAuthError):
                schema = self.tmp_path / "s.json"
                schema.write_text('{"type":"object"}')
                subagent.run_json_task(self.tmp_path, "hello", schema, task_name="writing")


if __name__ == "__main__":
    unittest.main()
