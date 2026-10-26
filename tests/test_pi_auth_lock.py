import json
import subprocess
import threading
import time
import types

import pytest

from scripts import subagent
from scripts.subagent import (
    PiAuthError,
    _is_auth_failure,
    _model_fallback_allowed,
    _pi_auth_refresh_under_lock,
)


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    monkeypatch.setenv("SUBAGENT_PI_AUTH_FILE", str(auth))
    monkeypatch.setenv("SUBAGENT_PI_PROVIDER", "openai")
    monkeypatch.delenv("SUBAGENT_PI_AUTH_LOCK", raising=False)
    return auth


def write_expiry(path, expires_ms):
    path.write_text(json.dumps({"openai": {"type": "oauth", "expires": expires_ms}}))


def ok_runner(calls):
    def run(cmd):
        calls.append(cmd)
        return types.SimpleNamespace(stdout="OK", stderr="", returncode=0)
    return run


@pytest.mark.parametrize(
    "text",
    [
        "Error: OAuth refresh failed for openai",
        '{"error":"invalid_grant"}',
        "refresh token has already been used",
        "Not authenticated. Use /login to sign in",
        "HTTP 401 Unauthorized",
    ],
)
def test_auth_patterns_detected(text):
    assert _is_auth_failure("", text)


@pytest.mark.parametrize("text", ["usage limit reached", "run id 4291abc", "", "OK"])
def test_non_auth_not_detected(text):
    assert not _is_auth_failure(text)


def test_fresh_token_skips_probe(auth_env):
    write_expiry(auth_env, int(time.time() * 1000) + 3600_000)
    calls = []
    assert _pi_auth_refresh_under_lock(["pi"], runner=ok_runner(calls)) == "fresh"
    assert calls == []


def test_expiring_token_probes_once(auth_env):
    write_expiry(auth_env, int(time.time() * 1000) + 60_000)
    calls = []
    assert _pi_auth_refresh_under_lock(["pi"], runner=ok_runner(calls)) == "refreshed"
    assert len(calls) == 1 and "--provider" in calls[0] and "openai" in calls[0]


def test_probe_auth_failure_raises(auth_env):
    write_expiry(auth_env, 0)
    bad = lambda cmd: types.SimpleNamespace(
        stdout="", stderr="OAuth refresh failed: invalid_grant", returncode=1
    )
    with pytest.raises(PiAuthError):
        _pi_auth_refresh_under_lock(["pi"], runner=bad)


def test_lock_disabled(auth_env, monkeypatch):
    monkeypatch.setenv("SUBAGENT_PI_AUTH_LOCK", "off")
    assert _pi_auth_refresh_under_lock(["pi"], runner=None) == "disabled"


def test_concurrent_refresh_is_serialized(auth_env):
    write_expiry(auth_env, 0)
    active, peak, lock = [0], [0], threading.Lock()

    def slow(cmd):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.1)
        with lock:
            active[0] -= 1
        # simulate the winner refreshing the token
        write_expiry(auth_env, int(time.time() * 1000) + 3600_000)
        return types.SimpleNamespace(stdout="OK", stderr="", returncode=0)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(_pi_auth_refresh_under_lock(["pi"], runner=slow)))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
    assert sorted(results) == ["fresh", "fresh", "fresh", "refreshed"]


def test_auth_error_not_model_fallback():
    assert not _model_fallback_allowed(PiAuthError("x", metrics={"failure_kind": "authentication"}))


def test_run_task_raises_auth_error_on_empty_output(tmp_path, monkeypatch, auth_env):
    monkeypatch.setenv("SUBAGENT_PI_AUTH_LOCK", "off")
    monkeypatch.setattr(subagent, "_run_process", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "", "OAuth refresh failed: refresh token has already been used"))
    monkeypatch.setattr(subagent.time, "sleep", lambda s: None)
    monkeypatch.setenv("SUBAGENT_RUNNER", "pi")
    monkeypatch.setenv("SUBAGENT_COMMAND", "pi")
    monkeypatch.setattr(subagent.shutil, "which", lambda name: "/usr/bin/" + name, raising=False)
    with pytest.raises(PiAuthError):
        schema = tmp_path / "s.json"
        schema.write_text('{"type":"object"}')
        subagent.run_json_task(tmp_path, "hello", schema, task_name="writing")
