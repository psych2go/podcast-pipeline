"""Subagent runner used by the podcast pipeline.

The pipeline owns orchestration and validation.  Subagents may either return a
structured JSON result or edit a narrowly scoped set of files.  The default
runner is Pi in headless JSON mode; Codex remains available as a compatible
fallback via ``SUBAGENT_COMMAND`` without changing pipeline code.

Dual runners (``SUBAGENT_RUNNER=auto|pi|grokbot``):
- ``pi`` — classic ``pi`` + GPT Plus (``openai-codex``).
- ``grokbot`` — Grok Bot / Cursor-agent box path: PTY-wrapped ``pi`` by default
  (headless non-TTY otherwise hangs), or ``SUBAGENT_GROKBOT_BACKEND=host`` file
  bridge for a parent host agent to fill responses.
- ``auto`` — ``grokbot`` when the Grok Bot / box markers are present, else ``pi``.

Model defaults on the pi path:
- Non-transcription tasks default to ``openai-codex/gpt-6-astra`` with
  ``--thinking low`` (override via ``SUBAGENT_PI_MODEL`` / ``SUBAGENT_PI_THINKING``).
- Transcription / correction tasks (``transcript_correction*``) do **not** force
  astra; use ``SUBAGENT_CORRECTION_MODEL`` / ``SUBAGENT_TRANSCRIPTION_MODEL`` /
  ``SUBAGENT_MODEL`` / pi's own default.

Failover env (no secrets here): ``SUBAGENT_COMMAND``, ``SUBAGENT_FALLBACK_COMMAND``,
``SUBAGENT_MODEL`` / provider model chain, and the chat shim's provider base URL /
``MODEL_FALLBACK``. Billing/quota/429/1113-style failures should trigger the next
configured model or command when fallback is set.
"""

import hashlib
import copy
import json
import math
import os
import re
import signal
import shlex
import shutil
import sys
import subprocess
import tempfile
import time
import uuid
import tomllib
from pathlib import Path

from scripts.atomic_io import atomic_write_bytes
from scripts.retry import RetryBudget, exponential_delay, retry_reason


class SubagentError(RuntimeError):
    """Raised when a subagent cannot complete a pipeline task."""

    def __init__(self, message, *, metrics=None):
        super().__init__(message)
        self.failure_metrics = metrics or {}


class PiAuthError(SubagentError):
    """pi login is dead (refresh token consumed / logged out). Never retry."""


AUTH_FAIL_MARKER = "SUBAGENT_AUTH_FAIL"
AUTH_FAIL_EXIT_CODE = 77
_AUTH_PATTERNS = (
    re.compile(r"OAuth refresh failed", re.I),
    re.compile(r"invalid_grant", re.I),
    re.compile(r"refresh token has already been used", re.I),
    re.compile(r"Use /login", re.I),
    re.compile(r"\bnot logged in\b", re.I),
    re.compile(r"(?:HTTP|status|last status:?)[ :]*401\b|\b401 Unauthorized", re.I),
)


def _is_auth_failure(*streams):
    """True when runner output shows a dead login (never quota/empty_output)."""
    text = "\n".join(s or "" for s in streams)
    return any(p.search(text) for p in _AUTH_PATTERNS)


def _pi_auth_path():
    raw = os.environ.get("SUBAGENT_PI_AUTH_FILE", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".pi/agent/auth.json"


def _pi_token_expires_ms(path, provider):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entry = data.get(provider) or {}
        return int(entry.get("expires")) if entry.get("expires") is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _pi_auth_refresh_under_lock(command, *, env=None, now_ms=None, runner=None):
    """Serialize pi token refresh across parallel pipelines.

    Takes an exclusive flock on SUBAGENT_PI_AUTH_LOCK (default
    ~/.pi/agent/.auth.lock). If the provider's access token expires within
    SUBAGENT_PI_REFRESH_MARGIN_S (default 600s), one cheap pi probe runs while
    holding the lock so exactly one process spends the single-use refresh
    token; the others block, then read the fresh token. Disable with
    SUBAGENT_PI_AUTH_LOCK=off. Raises PiAuthError if the probe shows a dead login.
    """
    raw_lock = os.environ.get("SUBAGENT_PI_AUTH_LOCK", "").strip()
    if raw_lock.lower() in {"0", "off", "false", "no"}:
        return "disabled"
    import fcntl

    auth = _pi_auth_path()
    lock = Path(raw_lock).expanduser() if raw_lock else auth.parent / ".auth.lock"
    provider = _pi_provider()
    margin = float(os.environ.get("SUBAGENT_PI_REFRESH_MARGIN_S", "600") or 600)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock, "a")
    except OSError:
        return "no_lock"
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            expires = _pi_token_expires_ms(auth, provider)
            now = now_ms if now_ms is not None else time.time() * 1000
            if expires is not None and expires - now > margin * 1000:
                return "fresh"
            probe = [command[0], "--provider", provider]
            model = os.environ.get("SUBAGENT_PI_MODEL", "").strip()
            if model:
                probe += ["--model", model]
            probe += ["--no-session", "-p", "Reply with exactly: OK"]
            run = runner or (
                lambda c: subprocess.run(
                    c, capture_output=True, text=True, timeout=120, env=env
                )
            )
            try:
                result = run(probe)
            except (OSError, subprocess.TimeoutExpired):
                return "probe_error"
            if _is_auth_failure(result.stdout, result.stderr):
                print(f"[subagent] {AUTH_FAIL_MARKER}: pi login invalid (probe)", flush=True)
                raise PiAuthError("pi 登录失效（OAuth refresh 失败），需要重新 /login")
            return "refreshed"
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _runner_failure(stderr, stdout):
    """Recognize error records only; never echo prompts, URLs or credentials."""
    for line in reversed(((stdout or "") + "\n" + (stderr or "")).splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                error = json.loads(line).get("error")
            except (ValueError, AttributeError):
                continue
            if isinstance(error, dict):
                code = error.get("code")
                if not isinstance(code, str):
                    continue
                if code == "semantic_reject":
                    return "semantic_reject", None
                if code == "model_not_found":
                    return "model_not_found", None
                if code in {"invalid_api_key", "authentication_error"}:
                    return "authentication", None
            continue
        if not line.startswith("ERROR:"):
            continue
        if "ReadTimeout" in line or "ConnectTimeout" in line or "timed out" in line.lower():
            return "timeout", None
        # Billing / quota exhaustion should fail over to the next model/command
        # when SUBAGENT_FALLBACK_COMMAND or model chain is configured.
        if (
            '"code":"1113"' in line
            or "Insufficient balance" in line
            or "余额不足" in line
            or "quota" in line.lower()
        ):
            status_match = re.search(r"HTTP(?: status)?[: ]+\s*(429|\d{3})\b", line)
            status = int(status_match[1]) if status_match else 429
            return "billing", status
        match = re.search(r"(?:last status:|HTTP(?: status)?[: ]+)\s*(401|403|429|5\d\d)\b", line)
        if match:
            status = int(match[1])
            kind = (
                "rate_limit"
                if status == 429
                else ("authentication" if status in {401, 403} else "server_error")
            )
            return kind, status
    return "unknown", None


_SAFE_CODEX_CONFIG_KEYS = frozenset(
    {
        "model",
        "review_model",
        "model_provider",
        "model_context_window",
        "model_auto_compact_token_limit",
        "model_reasoning_effort",
        "model_reasoning_summary",
        "model_verbosity",
        "disable_response_storage",
        "preferred_auth_method",
        "chatgpt_base_url",
        "openai_base_url",
        "forced_chatgpt_workspace_id",
        "model_catalog_json",
        "responses_websockets",
        "request_max_retries",
        "stream_max_retries",
        "stream_idle_timeout_ms",
        "tool_output_token_limit",
        "cli_auth_credentials_store",
    }
)

_SAFE_MODEL_PROVIDER_KEYS = frozenset(
    {
        "name",
        "base_url",
        "env_key",
        "env_key_instructions",
        "experimental_bearer_token",
        "http_headers",
        "env_http_headers",
        "query_params",
        "request_max_retries",
        "requires_openai_auth",
        "stream_idle_timeout_ms",
        "stream_max_retries",
        "wire_api",
    }
)


def prepare_output_schema(schema):
    """Return a Codex-compatible strict structured-output schema.

    Codex structured outputs require every object to reject unknown keys and
    to list every declared property in ``required``.  Callers should not need
    to duplicate those runner-specific constraints in each task schema.
    """
    prepared = copy.deepcopy(schema)

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if node.get("type") == "object" or isinstance(properties, dict):
            node["additionalProperties"] = False
            if isinstance(properties, dict):
                node["required"] = list(properties)
        for value in node.values():
            visit(value)

    visit(prepared)
    return prepared


def _runner_command(raw=None):
    raw = raw if raw is not None else os.environ.get("SUBAGENT_COMMAND", "pi")
    command = shlex.split(raw)
    if not command:
        raise SubagentError("SUBAGENT_COMMAND 为空")
    executable = shutil.which(command[0])
    if not executable:
        raise SubagentError(f"找不到 subagent runner: {command[0]!r}；请配置 SUBAGENT_COMMAND")
    command[0] = executable
    if Path(executable).name == "codex" and (len(command) < 2 or command[1] != "exec"):
        command.insert(1, "exec")
    if any("claude" in Path(part).name.lower() for part in command):
        raise SubagentError(
            "当前流水线禁止将 Claude CLI 作为 subagent runner；"
            "请使用 pi、codex exec 或兼容的 SUBAGENT_COMMAND"
        )
    return command


def _is_runner(command, name):
    return Path(command[0]).name.lower() == name


def _model_chain_env_name(task_name):
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(task_name).upper()).strip("_")
    return f"SUBAGENT_{normalized}_MODEL_CHAIN"


def _model_candidates(task_name, model):
    """Return a bounded, ordered model chain with duplicate entries removed."""
    if model:
        return [model]
    scoped = os.environ.get(_model_chain_env_name(task_name), "").strip()
    raw = scoped or os.environ.get("SUBAGENT_MODEL_CHAIN", "").strip()
    preferred = os.environ.get("SUBAGENT_MODEL", "").strip()
    values = [preferred] if preferred and not scoped else []
    values.extend(item.strip() for item in raw.split(",") if item.strip())
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result or [None]


def _model_fallback_allowed(exc):
    if isinstance(exc, PiAuthError):
        return False
    failure_kind = (getattr(exc, "failure_metrics", None) or {}).get("failure_kind")
    return failure_kind in {
        "model_not_found",
        "authentication",
        "timeout",
        "schema",
        "empty_output",
        "overloaded",
        "rate_limit",
        "billing",
        "network",
        "server_error",
    }


def _truthy_env(name):
    return os.environ.get(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _in_grokbot_environment():
    """Detect Grok Bot / Cursor-agent box environments."""
    if _truthy_env("SUBAGENT_GROKBOT") or _truthy_env("GROK_BOT"):
        return True
    if _truthy_env("CURSOR_AGENT"):
        return True
    if os.environ.get("SAND_BOX_BOOT_ID", "").strip() and Path("/home/box").is_dir():
        return True
    return False


def _runner_kind():
    """Return ``pi`` or ``grokbot`` from SUBAGENT_RUNNER / environment."""
    raw = os.environ.get("SUBAGENT_RUNNER", "auto").strip().lower() or "auto"
    if raw == "auto":
        return "grokbot" if _in_grokbot_environment() else "pi"
    if raw in {"pi", "grokbot"}:
        return raw
    raise SubagentError(f"SUBAGENT_RUNNER 必须是 auto|pi|grokbot 之一，当前值: {raw!r}")


def _grokbot_backend():
    raw = os.environ.get("SUBAGENT_GROKBOT_BACKEND", "pi").strip().lower() or "pi"
    if raw in {"pi", "host"}:
        return raw
    raise SubagentError(f"SUBAGENT_GROKBOT_BACKEND 必须是 pi|host 之一，当前值: {raw!r}")


def _is_transcription_task(task_name):
    """Transcription/correction stages must not force gpt-6-astra."""
    name = str(task_name or "").strip().lower()
    if name == "transcript_correction" or name.startswith("transcript_correction_"):
        return True
    extras = os.environ.get("SUBAGENT_TRANSCRIPTION_TASKS", "")
    for item in extras.split(","):
        token = item.strip().lower()
        if token and (name == token or name.startswith(token + "_")):
            return True
    return False


def _pi_provider():
    return (os.environ.get("SUBAGENT_PI_PROVIDER", "").strip()
            or os.environ.get("PI_PROVIDER", "").strip()
            or "openai-codex")


def _pi_model(model):
    """Apply provider overrides to legacy Codex model strings too."""
    if not model:
        return model
    provider, separator, name = model.partition("/")
    if not separator:
        return f"{_pi_provider()}/{model}"
    if provider == "openai-codex":
        return f"{_pi_provider()}/{name}"
    return model


def _pi_default_model(task_name=None):
    """Resolve pi provider/model; transcription never forces astra."""
    model = (
        os.environ.get("SUBAGENT_PI_MODEL", "").strip() or os.environ.get("PI_MODEL", "").strip()
    )
    if _is_transcription_task(task_name):
        # Prefer dedicated transcription/correction knobs when present.
        transcription = (
            os.environ.get("SUBAGENT_TRANSCRIPTION_MODEL", "").strip()
            or os.environ.get("SUBAGENT_CORRECTION_MODEL", "").strip()
        )
        if transcription:
            return _pi_model(transcription)
        return _pi_model(model) or None
    # Non-transcription pi path: default to gpt-6-astra unless explicitly set.
    if not model:
        model = "gpt-6-astra"
    return _pi_model(model) or None


def _stage_category(task_name):
    name = str(task_name or "").lower()
    if _is_transcription_task(task_name):
        return "transcription"
    if "review" in name:
        return "review"
    if "fact_check" in name:
        return "fact_check"
    if "claim" in name:
        return "claim"
    return "other"


WRITING_THINKING_TASKS = {
    "content_writing",
    "content_writing_contract_repair",
    "review_content_revision",
    "repair_low_confidence_claims",
    "content_depth_repair",
}


def _thinking_strategy(task_name):
    category = _stage_category(task_name)
    scoped = f"SUBAGENT_{category.upper()}_THINKING"
    keys = [scoped] if category in {"review", "claim", "fact_check"} else []
    if str(task_name or "").lower() in WRITING_THINKING_TASKS:
        keys = ["SUBAGENT_PI_THINKING_WRITING", *keys]
    for key in [*keys, "SUBAGENT_PI_THINKING", "SUBAGENT_THINKING"]:
        value = os.environ.get(key, "").strip().lower()
        if value:
            if value not in {"off", "none", "minimal", "low", "medium", "high", "xhigh"}:
                raise SubagentError(f"Invalid thinking level in {key}")
            return value, f"env:{key}"
    return (None, "transcription:none") if category == "transcription" else ("low", "default:low")


def resolve_stage_strategy(task_name, *, model=None, effort=None, enable_search=None):
    """Requested configuration only; prompt effort is not Pi thinking."""
    transcription = _is_transcription_task(task_name)
    candidates = _model_candidates(task_name, None)
    inherited = os.environ.get("SUBAGENT_MODEL", "").strip() or None
    chained = candidates[0] if len(candidates) > 1 else None
    knob = transcription and (
        os.environ.get("SUBAGENT_TRANSCRIPTION_MODEL", "").strip()
        or os.environ.get("SUBAGENT_CORRECTION_MODEL", "").strip()
    )
    resolved = _pi_model(model or chained or knob or inherited or _pi_default_model(task_name))
    source = (
        "explicit"
        if model
        else "inherit"
        if chained
        else "transcription_knob"
        if knob
        else "inherit"
        if inherited
        else "pi_default"
        if resolved
        else "none"
    )
    thinking, thinking_source = _thinking_strategy(task_name)
    return {
        "task_name": task_name,
        "stage_category": _stage_category(task_name),
        "requested_model": model,
        "resolved_model": resolved,
        "model_source": source,
        "prompt_effort": effort,
        "thinking": thinking,
        "thinking_source": thinking_source,
        "transcription_exempt": transcription,
        "enable_search": enable_search,
        "note": "prompt_effort != thinking (Pi --thinking)",
    }


def print_stage_strategy(strategy):
    # Print only enumerated policy fields, never arbitrary model/task/env text.
    summary = {
        key: strategy.get(key)
        for key in (
            "stage_category",
            "model_source",
            "thinking",
            "thinking_source",
            "transcription_exempt",
            "note",
        )
    }
    effort = strategy.get("prompt_effort")
    summary["prompt_effort"] = (
        effort if effort in {None, "low", "medium", "high", "max"} else "custom"
    )
    print("[strategy] " + json.dumps(summary), flush=True)


def _pi_thinking_level(task_name=None):
    return _thinking_strategy(task_name)[0]


def _pty_mode():
    return os.environ.get("SUBAGENT_PI_PTY", "auto").strip().lower() or "auto"


def _needs_pi_pty():
    mode = _pty_mode()
    if mode in {"never", "0", "false", "no", "off"}:
        return False
    if mode in {"always", "1", "true", "yes", "on"}:
        return True
    if mode != "auto":
        raise SubagentError(f"SUBAGENT_PI_PTY 必须是 auto|always|never 之一，当前值: {mode!r}")
    if _runner_kind() == "grokbot":
        return True
    try:
        return not sys.stdout.isatty()
    except Exception:
        return True


def _pty_wrapper_argv():
    local = Path(__file__).resolve().parent / "run_with_pty.py"
    if local.is_file():
        return [sys.executable, str(local)]
    workspace = Path("/workspace/run-pi-pty.py")
    if workspace.is_file():
        return [sys.executable, str(workspace)]
    raise SubagentError("需要 PTY wrapper 但找不到 scripts/run_with_pty.py")


def _wrap_with_pty(cmd):
    return [*_pty_wrapper_argv(), "--", *cmd]


def _execution_record(
    *,
    task_name,
    model,
    timeout,
    enable_search,
    backend,
    command_kind,
    content_ok=False,
    attempts=1,
    retries=0,
    runner_index=0,
    fallback_reason=None,
    effort=None,
):
    """Requested configuration is not evidence of the model actually used.

    Prompt effort prose is independent of Pi's explicit --thinking setting.
    """
    thinking = _pi_thinking_level(task_name) if backend == "pi" else None
    return {
        "requested_runner": os.environ.get("SUBAGENT_RUNNER", "auto"),
        "actual_runner": _runner_kind(),
        "actual_backend": backend,
        "command_kind": command_kind,
        "requested_model": model or None,
        "actual_model": "unknown",
        "thinking": thinking,
        "thinking_requested": thinking,
        "enable_search": bool(enable_search),
        "prompt_effort": effort,
        "thinking_source": (
            _thinking_strategy(task_name)[1] if backend == "pi" else "backend:not_pi"
        ),
        "timeout_s": timeout,
        "attempt": attempts,
        "attempt_count": attempts,
        "retry_count": retries,
        "runner_index": runner_index,
        "fallback_reason": fallback_reason,
        "content_ok": content_ok,
    }


def _host_bridge_dir(folder):
    configured = os.environ.get("SUBAGENT_HOST_BRIDGE_DIR", "").strip()
    root = Path(folder).resolve()
    path = Path(configured).expanduser() if configured else root / ".runlogs" / "host-bridge"
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise SubagentError(
            "host-bridge root must be inside allowed_folder, without symlink escape"
        )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o022:
        raise SubagentError("host-bridge root must be owned and not group/world writable")
    return path.resolve()


def _run_host_bridge(
    folder,
    task,
    *,
    task_name,
    schema_path=None,
    write_files=False,
    enable_search=False,
    model=None,
    timeout=None,
):
    """File-bridge runner for parent Grok Bot / host-agent fill-in.

    Writes request.json and waits for response.json (or error.json). Never
    invents answers; times out fail-closed. Hosts MUST write responses using
    temp + rename, echoing id and input_fingerprint. latest is a hint only.
    Directory checks are defense in depth, not a sandbox against same-UID peers.
    """
    timeout = timeout or int(os.environ.get("SUBAGENT_TIMEOUT", "1800"))
    poll = float(os.environ.get("SUBAGENT_HOST_BRIDGE_POLL", "0.5"))
    record = _execution_record(
        task_name=task_name,
        model=model,
        timeout=timeout,
        enable_search=enable_search,
        backend="host",
        command_kind="host-bridge",
    )

    def fail(kind):
        # Never echo host error bodies: they may contain tasks or credentials.
        return SubagentError(
            f"{task_name} host-bridge: {kind}",
            metrics={**record, "task_name": task_name, "failure_kind": kind},
        )

    try:
        bridge = _host_bridge_dir(folder)
    except (OSError, SubagentError) as exc:
        raise fail("host_bridge_invalid") from exc
    started = time.monotonic()
    created_at = time.time()
    request_id = uuid.uuid4().hex
    request_path = bridge / f"{request_id}.request.json"
    response_path = bridge / f"{request_id}.response.json"
    error_path = bridge / f"{request_id}.error.json"
    schema_payload = None
    if schema_path:
        schema_payload = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    canonical = json.dumps(
        {"task": task, "schema": schema_payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    request = {
        "protocol_version": 1,
        "input_fingerprint": fingerprint,
        "stage": task_name,
        "deadline": created_at + timeout,
        "allowed_folder": str(Path(folder).resolve()),
        "required_capabilities": ["search"] if enable_search else [],
        "id": request_id,
        "task_name": task_name,
        "task": task,
        "folder": str(Path(folder).resolve()),
        "schema": schema_payload,
        "write_files": bool(write_files),
        "enable_search": bool(enable_search),
        "model": model,
        "created_at": created_at,
    }
    atomic_write_bytes(
        request_path,
        json.dumps(request, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    # Also publish a stable "latest" pointer for human/host agents.
    latest = bridge / "latest.request.json"
    atomic_write_bytes(
        latest,
        json.dumps(
            {"id": request_id, "request_path": str(request_path)},
            ensure_ascii=False,
        ).encode("utf-8"),
    )

    def expired():
        return time.time() >= request["deadline"] or time.monotonic() - started >= timeout

    while True:
        if expired():
            raise fail("host_bridge_stale" if response_path.exists() else "timeout")
        if error_path.is_file():
            raise fail("host_bridge_error")
        if response_path.is_file():
            try:
                payload = json.loads(response_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise fail("host_bridge_invalid") from exc
            if expired():
                raise fail("host_bridge_stale")
            if (
                not isinstance(payload, dict)
                or payload.get("id") != request_id
                or payload.get("input_fingerprint") != fingerprint
                or payload.get("protocol_version", 1) != 1
            ):
                raise fail("host_bridge_mismatch")
            if "response" in payload:
                response_text = payload.get("response")
            elif isinstance(payload, dict) and "payload" in payload:
                response_text = json.dumps(payload["payload"], ensure_ascii=False)
            else:
                response_text = ""
            if not isinstance(response_text, str) or not response_text.strip():
                raise fail("empty_output")
            if schema_payload is not None:
                try:
                    schema_failure = _schema_error(_json_from_text(response_text), schema_payload)
                except SubagentError:
                    schema_failure = "invalid_json"
                if schema_failure:
                    raise fail("schema")
            if expired():
                raise fail("host_bridge_stale")
            duration_ms = round((time.monotonic() - started) * 1000)
            return {
                **record,
                "content_ok": True,
                "response": response_text,
                "duration_ms": duration_ms,
                "retry_count": 0,
                "runner_index": 0,
                "command": "host-bridge",
                "model": model or "",
                "task_name": task_name,
            }
        time.sleep(max(0.05, poll))


def _pi_web_extension():
    configured = os.environ.get("SUBAGENT_PI_WEB_EXTENSION", "").strip()
    candidate = (
        Path(configured).expanduser()
        if configured
        else (Path.home() / ".pi/agent/npm/node_modules/pi-web-access/index.ts")
    )
    if candidate.is_file() and not candidate.is_symlink():
        return str(candidate.resolve())
    return None


def _has_option(command, *options):
    return any(
        item in options or any(item.startswith(option + "=") for option in options)
        for item in command
    )


def _pi_command(
    command,
    *,
    task,
    schema_path,
    write_files,
    enable_search,
    model,
    schema_feedback=None,
    thinking=None,
):
    """Translate the pipeline runner contract to Pi's headless JSON mode."""
    cmd = list(command)
    required = [
        ("--no-session",),
        ("--no-extensions",),
        ("--no-mcp",),
        ("--no-skills",),
        ("--no-prompt-templates",),
        ("--no-context-files",),
        ("--no-approve",),
        ("--mode", "json"),
        ("--print",),
    ]
    for options in required:
        if not _has_option(cmd, *options):
            cmd.extend(options)

    no_file_tools = os.environ.get("SUBAGENT_PI_NO_FILE_TOOLS", "").strip() == "1"
    tools = [] if no_file_tools else ["read", "grep", "find", "ls"]
    if write_files and not no_file_tools:
        tools.extend(["edit", "write"])
    if enable_search:
        tools.extend(
            [
                "web_search",
                "fetch_content",
                "get_search_content",
                "source_check",
            ]
        )
        extension = _pi_web_extension()
        if extension and not _has_option(cmd, "--extension", "-e"):
            cmd.extend(["--extension", extension])
    if not _has_option(cmd, "--tools", "-t"):
        if tools:
            cmd.extend(["--tools", ",".join(tools)])
        else:
            cmd.append("--no-tools")

    if model and not _has_option(cmd, "--model"):
        cmd.extend(["--model", _pi_model(model)])
    elif _has_option(cmd, "--model"):
        for index, value in enumerate(cmd):
            if value == "--model" and index + 1 < len(cmd):
                cmd[index + 1] = _pi_model(cmd[index + 1])
            elif value.startswith("--model="):
                cmd[index] = "--model=" + _pi_model(value.split("=", 1)[1])
    provider_override = (os.environ.get("SUBAGENT_PI_PROVIDER", "").strip()
                         or os.environ.get("PI_PROVIDER", "").strip())
    if provider_override:
        for index, value in enumerate(cmd):
            if value == "--provider" and index + 1 < len(cmd):
                if cmd[index + 1] == "openai-codex":
                    cmd[index + 1] = provider_override
            elif value == "--provider=openai-codex":
                cmd[index] = "--provider=" + provider_override
        if not model and not _has_option(cmd, "--model", "--provider"):
            # Transcription may deliberately inherit Pi's model; still select
            # the requested provider instead of inheriting a Codex provider.
            cmd.extend(["--provider", provider_override])
    if thinking and not _has_option(cmd, "--thinking"):
        cmd.extend(["--thinking", thinking])

    prompt = _base_prompt(task)
    if schema_path:
        try:
            schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SubagentError(f"无法读取 Pi output schema: {exc}") from exc
        prompt += (
            "\n必须只输出一个符合以下 JSON Schema 的 JSON 值；"
            "不要输出 Markdown 代码围栏、解释文字或额外字段。\n"
            "JSON Schema:\n" + json.dumps(schema, ensure_ascii=False)
        )
        if schema_feedback:
            prompt += (
                "\n上一次输出未通过结构校验。只修复以下结构问题，"
                "不要改变已满足要求的事实内容：\n" + schema_feedback
            )
    cmd.append(prompt)
    return cmd


def _extract_pi_response(stdout):
    """Extract the final assistant text from Pi's JSON event stream."""
    last_text = ""
    saw_event = False
    for line in (stdout or "").split("\n"):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        saw_event = True
        if event.get("type") == "agent_settled" and event.get("aborted"):
            last_text = ""
        message = None
        if event.get("type") == "message_end":
            message = event.get("message")
        elif event.get("type") == "agent_end":
            messages = event.get("messages") or []
            for candidate in reversed(messages):
                if isinstance(candidate, dict) and candidate.get("role") == "assistant":
                    message = candidate
                    break
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content") or []
        text = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        )
        # Never accept an earlier tool preamble after a failed/empty final turn.
        last_text = (
            text
            if message.get("stopReason")
            not in {"error", "aborted", "toolUse", "pending", "length", "deferred"}
            else ""
        )
    return last_text if saw_event else (stdout or "").strip()


def _schema_error(value, schema, path="$", *, limit=100):
    """Small dependency-free validator for the strict runner JSON contract."""
    if not isinstance(schema, dict):
        return None
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} 不在 enum 中"
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            matches = sum(_schema_error(value, child, path) is None for child in schema[keyword])
            if (
                (keyword == "anyOf" and not matches)
                or (keyword == "oneOf" and matches != 1)
                or (keyword == "allOf" and matches != len(schema[keyword]))
            ):
                return f"{path} 不符合 {keyword}"
    expected = schema.get("type")
    type_ok = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    types = expected if isinstance(expected, list) else [expected]
    if expected is not None and not any(kind in type_ok and type_ok[kind](value) for kind in types):
        return f"{path} 类型应为 {expected}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return f"{path} 数值必须有限"
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} 小于 minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} 大于 maximum"
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return f"{path} 字符串过短"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} 字符串过长"
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return f"{path} 不匹配 pattern"
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return f"{path} 数组项目不足"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} 数组项目过多"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error = _schema_error(item, item_schema, f"{path}[{index}]")
                if error:
                    return error
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                return f"{path}.{key} 缺少必需字段"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                return f"{path} 包含未允许字段"
        for key, child_schema in properties.items():
            if key in value:
                error = _schema_error(value[key], child_schema, f"{path}.{key}")
                if error:
                    return error
    return None


def _runner_commands():
    commands = []
    missing = None
    fallback = os.environ.get("SUBAGENT_FALLBACK_COMMAND", "").strip()
    for raw in [None, fallback] if fallback else [None]:
        try:
            command = _runner_command(raw)
        except SubagentError as exc:
            if not str(exc).startswith("找不到 subagent runner:"):
                raise
            missing = exc
            continue
        if command not in commands:
            commands.append(command)
    if not commands:
        raise missing
    return commands


def _base_prompt(task):
    return f"""你是播客流水线中的受限 subagent。
只完成当前任务，不要调用其他 agent，不要修改任务范围之外的文件。
主脚本会对你的输出执行 JSON schema、哈希、证据和质量门校验。
如果输入证据不足，必须明确报告不足，不得凭常识补写事实。

当前任务：
{task}
"""


def _json_from_text(text):
    text = (text or "").strip()
    if not text:
        raise SubagentError("subagent 没有返回内容")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].lstrip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    # Models sometimes prepend or append prose around the JSON payload
    # (e.g. "以下是结果：{...}"). Extract the first balanced JSON value
    # instead of failing the whole attempt on an unparseable surface form.
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return value
    raise SubagentError(f"subagent 返回的不是有效 JSON: {text[-1000:]}")


def _terminate_process_tree(process, grace_seconds=2):
    """Terminate a subprocess and every descendant in its process group."""
    if process.poll() is not None:
        return
    try:
        group = os.getpgid(process.pid)
        os.killpg(group, signal.SIGTERM)
        process.wait(timeout=grace_seconds)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _run_process(cmd, *, cwd, env, timeout):
    """Run one runner attempt with process-group timeout cleanup."""
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=stdout or exc.output,
            stderr=stderr or exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(
        cmd,
        process.returncode,
        stdout,
        stderr,
    )


def _toml_key(value):
    value = str(value)
    if value and all(char.isalnum() or char in "_-" for char in value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise SubagentError(f"隔离 Codex 配置包含不支持的值类型: {type(value).__name__}")


def _toml_text(payload):
    """Serialize the small, sanitized Codex config subset we retain."""
    lines = []

    def emit(mapping, path=()):
        scalars = [(key, value) for key, value in mapping.items() if not isinstance(value, dict)]
        tables = [(key, value) for key, value in mapping.items() if isinstance(value, dict)]
        if path:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append("[" + ".".join(_toml_key(item) for item in path) + "]")
        for key, value in scalars:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in tables:
            emit(value, (*path, key))

    emit(payload)
    return "\n".join(lines).strip() + "\n"


def _copy_model_catalog(config, source_home, isolated, *, prefix=""):
    catalog = config.get("model_catalog_json")
    if not isinstance(catalog, str) or not catalog.strip():
        return
    source = Path(catalog).expanduser()
    if not source.is_absolute():
        source = source_home / source
    if not source.is_file() or source.is_symlink():
        raise SubagentError(f"Codex model_catalog_json 不可安全复制: {source}")
    destination_name = f"{prefix}-{source.name}" if prefix else source.name
    destination = isolated / destination_name
    shutil.copy2(source, destination)
    destination.chmod(0o600)
    config["model_catalog_json"] = destination_name


def _sanitized_codex_config(source_path, source_home, isolated, *, prefix=""):
    try:
        raw = tomllib.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SubagentError(f"无法读取 Codex 配置 {source_path}: {exc}") from exc
    sanitized = {key: copy.deepcopy(raw[key]) for key in _SAFE_CODEX_CONFIG_KEYS if key in raw}
    providers = raw.get("model_providers")
    if isinstance(providers, dict):
        sanitized_providers = {}
        for provider_id, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            sanitized_provider = {
                key: copy.deepcopy(provider[key])
                for key in _SAFE_MODEL_PROVIDER_KEYS
                if key in provider
            }
            if sanitized_provider:
                sanitized_providers[str(provider_id)] = sanitized_provider
        if sanitized_providers:
            sanitized["model_providers"] = sanitized_providers
    # Hooks are explicitly disabled even when the user config enables them.
    # MCP servers, hook state, projects, notices, skills and UI preferences are
    # omitted rather than copied into the isolated runner home.
    sanitized["features"] = {
        "hooks": False,
        "plugins": False,
        "remote_plugin": False,
        "workspace_dependencies": False,
    }
    _copy_model_catalog(sanitized, source_home, isolated, prefix=prefix)
    return sanitized


def _profile_names(command):
    names = []
    index = 0
    while index < len(command):
        part = command[index]
        if part in {"-p", "--profile"} and index + 1 < len(command):
            names.append(command[index + 1])
            index += 2
            continue
        if part.startswith("--profile="):
            names.append(part.split("=", 1)[1])
        index += 1
    return [name for name in names if name]


def _copy_sanitized_codex_config(source_home, isolated, command):
    config_path = source_home / "config.toml"
    if config_path.exists():
        sanitized = _sanitized_codex_config(config_path, source_home, isolated)
        target = isolated / "config.toml"
        target.write_text(_toml_text(sanitized), encoding="utf-8")
        target.chmod(0o600)
    for profile in _profile_names(command):
        source = source_home / f"{profile}.config.toml"
        if not source.exists():
            raise SubagentError(f"Codex profile 配置不存在: {source}")
        sanitized = _sanitized_codex_config(source, source_home, isolated, prefix=profile)
        target = isolated / f"{profile}.config.toml"
        target.write_text(_toml_text(sanitized), encoding="utf-8")
        target.chmod(0o600)


_RUNNER_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "COLORTERM",
        "LANG",
        "LANGUAGE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "TZ",
        "NO_COLOR",
        # Pi model selection is passed explicitly too, but keeping these values
        # available preserves provider/model diagnostics for compatible runners.
        "PI_PROVIDER",
        "PI_MODEL",
        "PI_REASONING_LEVEL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "GIT_PAGER",
        "PAGER",
        # Model credentials are necessary when auth.json is not used. Pipeline
        # credentials such as FISH_KEY/HF_TOKEN/Cloudflare tokens are excluded.
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
    }
)

# Never expose pipeline secrets to the runner, even when they appear in
# SUBAGENT_ENV_ALLOWLIST or a custom model provider env_key.
_PIPELINE_SECRET_ENV_DENYLIST = frozenset(
    {
        "FISH_KEY",
        "HF_TOKEN",
        "HUGGINGFACE_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)


def _configured_provider_env_keys(home, command):
    keys = set()
    paths = [Path(home) / "config.toml"]
    paths.extend(Path(home) / f"{profile}.config.toml" for profile in _profile_names(command))
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        providers = config.get("model_providers")
        if not isinstance(providers, dict):
            continue
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            env_key = provider.get("env_key")
            if isinstance(env_key, str) and env_key.strip():
                keys.add(env_key.strip())
            headers = provider.get("env_http_headers")
            if isinstance(headers, dict):
                keys.update(
                    str(value).strip()
                    for value in headers.values()
                    if isinstance(value, str) and value.strip()
                )
    return keys


def _filtered_runner_environment(source_env, *, provider_keys=()):
    explicit = {
        item.strip()
        for item in source_env.get("SUBAGENT_ENV_ALLOWLIST", "").split(",")
        if item.strip()
    }
    allowed = set(_RUNNER_ENV_KEYS) | set(provider_keys) | explicit
    allowed.update(
        key for key in source_env if key.startswith("LC_") or key.startswith("SUBAGENT_")
    )
    allowed -= _PIPELINE_SECRET_ENV_DENYLIST
    return {
        key: value
        for key, value in source_env.items()
        if key in allowed and key not in _PIPELINE_SECRET_ENV_DENYLIST
    }


def _runner_environment(tmp, command):
    """Isolate runner config and expose only runtime/provider environment."""
    source_env = os.environ.copy()
    is_codex = Path(command[0]).name == "codex"
    source = Path(source_env.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    configured = source_env.get("SUBAGENT_CODEX_HOME", "").strip()
    provider_home = Path(configured).expanduser().resolve() if configured else source
    provider_keys = _configured_provider_env_keys(provider_home, command) if is_codex else set()
    env = _filtered_runner_environment(source_env, provider_keys=provider_keys)
    if _is_runner(command, "pi"):
        # Give each Pi attempt a disposable config directory.  Copy only
        # authentication/model catalogs and web-search routing; never inherit
        # the parent session, history, extensions, trust state, or hooks.
        source_pi = Path(
            source_env.get(
                "PI_CODING_AGENT_DIR",
                str(Path.home() / ".pi/agent"),
            )
        ).expanduser()
        isolated = Path(tmp) / "pi-agent"
        isolated.mkdir(parents=True, exist_ok=True)
        for name in ("auth.json", "models.json", "models-store.json", "web-search.json"):
            source = source_pi / name
            if source.is_file() and not source.is_symlink():
                destination = isolated / name
                shutil.copy2(source, destination)
                destination.chmod(0o600)
        env["PI_CODING_AGENT_DIR"] = str(isolated)
        env.pop("PI_SESSION_FILE", None)
        env.pop("PI_SESSION_ID", None)
        return env
    if not is_codex:
        return env

    inherit = source_env.get("SUBAGENT_INHERIT_CODEX_HOME", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if inherit:
        env["CODEX_HOME"] = str(source)
        return env
    if configured:
        isolated = Path(configured).expanduser().resolve()
        isolated.mkdir(parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(isolated)
        return env

    isolated = Path(tmp) / "codex-home"
    isolated.mkdir(parents=True, exist_ok=True)
    auth = source / "auth.json"
    if auth.is_file() and not auth.is_symlink():
        shutil.copy2(auth, isolated / "auth.json")
        (isolated / "auth.json").chmod(0o600)
    _copy_sanitized_codex_config(source, isolated, command)
    env["CODEX_HOME"] = str(isolated)
    return env


def _workspace_snapshot(folder):
    """Capture a write staging tree so every runner attempt starts clean."""
    folder = Path(folder)
    snapshot = {}
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise SubagentError(f"staging 不允许符号链接: {path.relative_to(folder)}")
        if path.is_file():
            snapshot[path.relative_to(folder)] = path.read_bytes()
        elif not path.is_dir():
            raise SubagentError(f"staging 包含非普通文件: {path.relative_to(folder)}")
    return snapshot


def _restore_workspace(folder, snapshot):
    folder = Path(folder)
    for path in sorted(folder.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()
            except OSError:
                pass
        else:
            path.unlink(missing_ok=True)
    for relative, data in snapshot.items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, data)


def _run(
    folder,
    task,
    *,
    task_name,
    schema_path=None,
    write_files=False,
    enable_search=False,
    model=None,
    timeout=None,
    _model_candidates_override=None,
    effort=None,
    _budget=None,
    _prior_failure=None,
):
    folder = Path(folder).resolve()
    candidates = (
        _model_candidates_override
        if _model_candidates_override is not None
        else _model_candidates(task_name, model)
    )
    if _budget is None:
        print_stage_strategy(
            resolve_stage_strategy(
                task_name, model=model, effort=effort, enable_search=enable_search
            )
        )
        retries = max(0, int(os.environ.get("SUBAGENT_MAX_RETRIES", "2")))
        _budget = RetryBudget(
            int(
                os.environ.get(
                    "SUBAGENT_STAGE_CALL_BUDGET", str((retries + 1) * max(1, len(candidates)) * 2)
                )
            ),
            scope=task_name,
        )
    if len(candidates) > 1:
        last_error = None
        attempted = []
        prior_attempts = 0
        fallback_reason = None
        for candidate in candidates:
            attempted.append(candidate or "runner-default")
            try:
                result = _run(
                    folder,
                    task,
                    task_name=task_name,
                    schema_path=schema_path,
                    write_files=write_files,
                    enable_search=enable_search,
                    model=candidate,
                    timeout=timeout,
                    _model_candidates_override=[candidate],
                    effort=effort,
                    _budget=_budget,
                    _prior_failure=fallback_reason,
                )
                result["attempt_count"] += prior_attempts
                result["attempt"] = result["attempt_count"]
                result["retry_count"] = result["attempt_count"] - 1
                result["fallback_reason"] = fallback_reason or result.get("fallback_reason")
                result["attempted_models"] = attempted
                return result
            except SubagentError as exc:
                metrics = exc.failure_metrics
                count = metrics.get("attempt_count", 1)
                metrics["attempt_count"] = prior_attempts + count
                metrics["attempt"] = metrics["attempt_count"]
                metrics["retry_count"] = max(0, metrics["attempt_count"] - 1)
                metrics["fallback_reason"] = fallback_reason or metrics.get("fallback_reason")
                prior_attempts += count
                fallback_reason = metrics.get("failure_kind")
                last_error = exc
                if not _model_fallback_allowed(exc):
                    raise
                print(
                    f"[subagent] {task_name} 模型 {candidate or 'runner-default'}"
                    f" 不可用，切换候选模型",
                    flush=True,
                )
        if last_error is not None:
            metrics = dict(getattr(last_error, "failure_metrics", {}) or {})
            metrics["attempted_models"] = attempted
            raise SubagentError(
                f"{task_name} 所有候选模型均不可用: {', '.join(attempted)}",
                metrics=metrics,
            ) from last_error
    # Per-task model arguments (e.g. SUBAGENT_REVIEW_MODEL) take precedence;
    # SUBAGENT_MODEL fills the stages that do not select a model themselves.
    # Transcription may also use SUBAGENT_TRANSCRIPTION_MODEL without forcing
    # the non-transcription gpt-6-astra default.
    if not model and _is_transcription_task(task_name):
        model = (
            os.environ.get("SUBAGENT_TRANSCRIPTION_MODEL", "").strip()
            or os.environ.get("SUBAGENT_CORRECTION_MODEL", "").strip()
            or None
        )
    model = model or os.environ.get("SUBAGENT_MODEL", "").strip() or None
    runner_kind = _runner_kind()
    timeout = timeout or int(os.environ.get("SUBAGENT_TIMEOUT", "1800"))
    max_retries = int(os.environ.get("SUBAGENT_MAX_RETRIES", "2"))

    # Grok Bot host-bridge backend: parent agent fills response files.
    if runner_kind == "grokbot" and _grokbot_backend() == "host":
        host_model = model or _pi_default_model(task_name) or ""
        if not _budget.consume(
            "initial_call" if not _budget.events else retry_reason(_prior_failure)
        ):
            raise SubagentError(
                "stage call budget exhausted",
                metrics={
                    **_budget.metrics(),
                    "failure_kind": "budget_exhausted",
                    "content_ok": False,
                },
            )
        try:
            result = _run_host_bridge(
                folder,
                task,
                task_name=task_name,
                schema_path=schema_path,
                write_files=write_files,
                enable_search=enable_search,
                model=host_model or None,
                timeout=timeout,
            )
        except SubagentError as exc:
            exc.failure_metrics.update(_budget.metrics())
            exc.failure_metrics["prompt_effort"] = effort
            raise
        if schema_path:
            try:
                payload = _json_from_text(result["response"])
                schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
                schema_failure = _schema_error(payload, schema)
            except (OSError, json.JSONDecodeError, SubagentError):
                schema_failure = "invalid_json"
            if schema_failure:
                raise SubagentError(
                    f"{task_name} host-bridge schema 校验失败: {schema_failure}",
                    metrics={
                        **_execution_record(
                            task_name=task_name,
                            model=host_model,
                            timeout=timeout,
                            enable_search=enable_search,
                            backend="host",
                            command_kind="host-bridge",
                            effort=effort,
                        ),
                        **_budget.metrics(),
                        "task_name": task_name,
                        "model": host_model,
                        "failure_kind": "schema",
                        "runner_index": 0,
                        "attempt_count": 1,
                        "retry_count": 0,
                        "duration_ms": result.get("duration_ms", 0),
                    },
                )
        return {
            **_execution_record(
                task_name=task_name,
                model=host_model,
                timeout=timeout,
                enable_search=enable_search,
                backend="host",
                command_kind="host-bridge",
                content_ok=True,
                effort=effort,
            ),
            **result,
            **_budget.metrics(),
            "prompt_effort": effort,
        }

    commands = _runner_commands()
    output_path = None
    workspace_snapshot = _workspace_snapshot(folder) if write_files else None
    attempts = 0
    total_duration_ms = 0
    total_retries = 0
    runner_index = 0
    runner_model = model or ""
    failure_kind, http_status = _prior_failure or "unknown", None
    thinking_level = _pi_thinking_level(task_name)
    backend, command_kind = "unknown", "unknown"
    fallback_reason = None

    def execution(content_ok=False):
        return {
            **_execution_record(
                task_name=task_name,
                model=runner_model,
                timeout=timeout,
                enable_search=enable_search,
                backend=backend,
                command_kind=command_kind,
                content_ok=content_ok,
                attempts=attempts,
                retries=total_retries,
                runner_index=runner_index,
                fallback_reason=fallback_reason,
                effort=effort,
            ),
            **_budget.metrics(),
        }

    def failure_metrics():
        return {
            **execution(),
            "task_name": task_name,
            "model": runner_model,
            "attempt_count": attempts,
            "retry_count": total_retries,
            "duration_ms": total_duration_ms,
            "runner_index": runner_index,
            "failure_kind": failure_kind,
            "http_status": http_status,
            "runner_kind": runner_kind,
        }

    try:
        with tempfile.TemporaryDirectory(prefix=f"podcast-subagent-{task_name}-") as tmp:
            if schema_path:
                schema_path = Path(schema_path).resolve()
            else:
                schema_path = None
            output_path = Path(tmp) / "last_message.txt"
            last_detail = ""
            for runner_index, command in enumerate(commands):
                if runner_index:
                    fallback_reason = failure_kind
                backend = "pi" if _is_runner(command, "pi") else "codex"
                command_kind = backend
                runner_env = _runner_environment(tmp, command)
                is_pi = _is_runner(command, "pi")
                runner_model = (
                    _pi_model(model or _pi_default_model(task_name)) if is_pi else model
                ) or ""
                if is_pi:
                    cmd = _pi_command(
                        command,
                        task=task,
                        schema_path=schema_path,
                        write_files=write_files,
                        enable_search=enable_search,
                        model=runner_model or None,
                        thinking=thinking_level,
                    )
                    if _needs_pi_pty():
                        cmd = _wrap_with_pty(cmd)
                        command_kind = "pi+pty"
                else:
                    cmd = [
                        *command,
                        "--ephemeral",
                        "--skip-git-repo-check",
                        "-C",
                        str(folder),
                        "-s",
                        "workspace-write" if write_files else "read-only",
                        "-o",
                        str(output_path),
                    ]
                    if write_files:
                        cmd.extend(["--add-dir", str(folder)])
                    if enable_search:
                        if Path(command[0]).name == "codex" and len(cmd) > 1 and cmd[1] == "exec":
                            cmd.insert(1, "--search")
                        else:
                            cmd.append("--search")
                    if model:
                        cmd.extend(["--model", model])
                    if schema_path and os.environ.get(
                        "SUBAGENT_DISABLE_OUTPUT_SCHEMA", ""
                    ).strip().lower() not in {"1", "true", "yes", "on"}:
                        cmd.extend(["--output-schema", str(schema_path)])
                    cmd.append(_base_prompt(task))

                schema_feedback = None
                for attempt in range(max_retries + 1):
                    if is_pi and schema_feedback:
                        cmd = _pi_command(
                            command,
                            task=task,
                            schema_path=schema_path,
                            write_files=write_files,
                            enable_search=enable_search,
                            model=runner_model or None,
                            schema_feedback=schema_feedback,
                            thinking=thinking_level,
                        )
                        if _needs_pi_pty():
                            cmd = _wrap_with_pty(cmd)
                    if workspace_snapshot is not None:
                        _restore_workspace(folder, workspace_snapshot)
                    output_path.unlink(missing_ok=True)
                    reason = "initial_call" if not _budget.events else retry_reason(failure_kind)
                    if not _budget.consume(reason):
                        failure_kind = "budget_exhausted"
                        raise SubagentError(
                            f"{task_name} stage call budget exhausted", metrics=failure_metrics()
                        )
                    if is_pi:
                        _pi_auth_refresh_under_lock(command, env=runner_env)
                    started = time.monotonic()
                    attempts += 1
                    try:
                        result = _run_process(
                            cmd,
                            cwd=folder,
                            env=runner_env,
                            timeout=timeout,
                        )
                    except OSError as exc:
                        total_duration_ms += round((time.monotonic() - started) * 1000)
                        failure_kind, http_status = "launch_error", None
                        last_detail = f"launch_error: {type(exc).__name__}"
                        break
                    except subprocess.TimeoutExpired:
                        total_duration_ms += round((time.monotonic() - started) * 1000)
                        failure_kind, http_status = "timeout", None
                        last_detail = f"timeout after {timeout}s"
                    else:
                        total_duration_ms += round((time.monotonic() - started) * 1000)
                        if is_pi and _is_auth_failure(result.stdout, result.stderr) and (
                            result.returncode != 0
                            or not _extract_pi_response(result.stdout).strip()
                        ):
                            failure_kind, http_status = "authentication", 401
                            print(
                                f"[subagent] {AUTH_FAIL_MARKER}: {task_name} pi login invalid",
                                flush=True,
                            )
                            raise PiAuthError(
                                f"{task_name} pi 登录失效（OAuth refresh 失败），需要重新 /login",
                                metrics=failure_metrics(),
                            )
                        if result.returncode == 0:
                            response = (
                                _extract_pi_response(result.stdout)
                                if is_pi
                                else (
                                    output_path.read_text(encoding="utf-8")
                                    if output_path.exists()
                                    else result.stdout
                                )
                            )
                            if not response.strip():
                                failure_kind, http_status = "empty_output", None
                                last_detail = "empty_output; exit=0"
                            elif schema_path:
                                try:
                                    payload = _json_from_text(response)
                                    schema = json.loads(
                                        Path(schema_path).read_text(encoding="utf-8")
                                    )
                                    schema_failure = _schema_error(payload, schema)
                                except (OSError, json.JSONDecodeError, SubagentError):
                                    schema_failure = "invalid_json"
                                if schema_failure:
                                    failure_kind, http_status = "schema", None
                                    # Pi runs with --no-session: the retry cannot see
                                    # its previous answer unless we hand it back, which
                                    # otherwise yields all-zero placeholder reviews.
                                    previous = response.strip()
                                    if len(previous) > 60000:
                                        previous = previous[:60000] + "\n…（已截断）"
                                    schema_feedback = (
                                        schema_failure
                                        + "\n\n上一次的完整输出如下（在其基础上仅补齐/修正结构，"
                                        "保留其中已有的评价与事实结论）：\n" + previous
                                    )
                                    last_detail = f"schema; exit=0; {schema_failure[:160]}"
                                else:
                                    return {
                                        **execution(content_ok=True),
                                        "response": response,
                                        "duration_ms": total_duration_ms,
                                        "retry_count": total_retries,
                                        "runner_index": runner_index,
                                        "command": " ".join(command),
                                        "model": runner_model,
                                        "task_name": task_name,
                                    }
                            else:
                                return {
                                    **execution(content_ok=True),
                                    "response": response,
                                    "duration_ms": total_duration_ms,
                                    "retry_count": total_retries,
                                    "runner_index": runner_index,
                                    "command": " ".join(command),
                                    "model": runner_model,
                                    "task_name": task_name,
                                }
                        else:
                            failure_kind, http_status = _runner_failure(
                                result.stderr, result.stdout
                            )
                            if failure_kind == "semantic_reject":
                                _budget.consume("semantic_reject")
                                raise SubagentError(
                                    f"{task_name} semantic rejection (no transport fallback)",
                                    metrics=failure_metrics(),
                                )
                            last_detail = f"{failure_kind}; exit={result.returncode}"
                            if http_status:
                                last_detail += f"; HTTP {http_status}"
                            # Surface a short stderr/stdout tail for diagnosis
                            # (never dump prompts/secrets; shim ERROR lines only).
                            diag = []
                            for stream in (result.stderr or "", result.stdout or ""):
                                for line in stream.splitlines():
                                    s = line.strip()
                                    if s.startswith("ERROR:") or s.startswith("codex_chat_shim:"):
                                        diag.append(s[:240])
                            if not diag:
                                combined = (result.stderr or "") + (result.stdout or "")
                                tail = combined[-400:].strip()
                                if tail:
                                    diag.append("tail:" + tail.replace("\n", " ")[:240])
                            if diag:
                                print(
                                    f"[subagent] {task_name} runner diag: " + " | ".join(diag[-3:]),
                                    flush=True,
                                )
                        if result.returncode != 0 and failure_kind in {
                            "model_not_found",
                            "authentication",
                        }:
                            break
                    if attempt < max_retries:
                        total_retries += 1
                        wait = exponential_delay(attempt + 1, 2.0)
                        print(
                            f"[subagent] {task_name} 失败"
                            f"（{last_detail[:200]}），{wait:g}s 后重试 "
                            f"{attempt + 1}/{max_retries}",
                            flush=True,
                        )
                        time.sleep(wait)
                if runner_index + 1 < len(commands):
                    print(
                        f"[subagent] {task_name} 主 runner 不可用，"
                        f"切换备用 runner {runner_index + 1}",
                        flush=True,
                    )
            raise SubagentError(
                f"{task_name} subagent 失败，所有 runner 均不可用: {last_detail}",
                metrics=failure_metrics(),
            )
    except subprocess.TimeoutExpired as exc:
        failure_kind, http_status = "timeout", None
        raise SubagentError(
            f"{task_name} subagent 超时 ({timeout}s)",
            metrics=failure_metrics(),
        ) from exc
    except OSError as exc:
        failure_kind, http_status = "launch_error", None
        raise SubagentError(
            f"{task_name} subagent 启动失败: {type(exc).__name__}", metrics=failure_metrics()
        ) from exc


def run_json_task(
    folder,
    task,
    schema_path,
    *,
    task_name,
    enable_search=False,
    model=None,
    timeout=None,
    effort=None,
):
    """Run a read-only subagent and parse its structured JSON response."""
    temporary_schema = None
    schema_disabled = os.environ.get("SUBAGENT_DISABLE_OUTPUT_SCHEMA", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if schema_disabled:
        # Without runner-side schema negotiation the model never sees the
        # contract, so it guesses an envelope shape. Embed the schema in the
        # prompt instead; this only carries the contract, never content.
        if schema_path is not None:
            raw_schema = (
                schema_path
                if isinstance(schema_path, dict)
                else json.loads(Path(schema_path).read_text(encoding="utf-8"))
            )
            task = (
                task + "\n输出必须严格符合以下 JSON Schema：顶层字段全部必填，"
                "不要使用其他信封或嵌套包装结构。\n" + json.dumps(raw_schema, ensure_ascii=False)
            )
    # Keep local Pi validation/retry even when provider schema negotiation is
    # disabled. Codex's --output-schema flag is independently suppressed above.
    if isinstance(schema_path, dict):
        raw_schema = schema_path
    else:
        raw_schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    temporary_schema = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    )
    json.dump(prepare_output_schema(raw_schema), temporary_schema, ensure_ascii=False)
    temporary_schema.close()
    runner_schema = temporary_schema.name
    try:
        result = _run(
            folder,
            task,
            task_name=task_name,
            schema_path=runner_schema,
            effort=effort,
            write_files=False,
            enable_search=enable_search,
            model=model,
            timeout=timeout,
        )
    finally:
        if temporary_schema is not None:
            Path(temporary_schema.name).unlink(missing_ok=True)
    payload = _json_from_text(result["response"])
    result["payload"] = payload
    return result


def _scoped_paths(folder, paths, label):
    """Normalize caller paths without allowing symlink or parent traversal."""
    normalized = []
    seen = set()
    for value in paths or []:
        raw = Path(value)
        candidate = raw if raw.is_absolute() else folder / raw
        candidate = Path(os.path.abspath(candidate))
        try:
            relative = candidate.relative_to(folder)
        except ValueError as exc:
            raise SubagentError(f"{label} 不在单集目录内: {value}") from exc
        if relative == Path("."):
            raise SubagentError(f"{label} 不能是单集目录本身")

        current = folder
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise SubagentError(f"{label} 不允许经过符号链接: {relative}")

        key = relative.as_posix()
        if key not in seen:
            normalized.append(relative)
            seen.add(key)
    return normalized


def _regular_file_bytes(path, label):
    if path.is_symlink():
        raise SubagentError(f"{label} 不允许是符号链接: {path.name}")
    if not path.is_file():
        raise SubagentError(f"{label} 不是普通文件: {path.name}")
    return path.read_bytes()


def _file_snapshot(path, label):
    if not path.exists():
        return {"exists": False, "sha256": ""}
    data = _regular_file_bytes(path, label)
    return {
        "exists": True,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _assert_snapshot(path, snapshot, label):
    current = _file_snapshot(path, label)
    if current != snapshot:
        raise SubagentError(f"{label} 在 subagent 运行期间发生变化: {path.name}")


def _infer_mentioned_inputs(folder, task, excluded):
    """Copy only existing files explicitly named by legacy task prompts."""
    mentioned = []
    excluded = {path.as_posix() for path in excluded}
    for path in folder.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(folder)
        if relative.as_posix() not in excluded and relative.as_posix() in task:
            mentioned.append(relative)
    return mentioned


def _copy_into_staging(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


_HOST_BRIDGE_REL = ".runlogs/host-bridge"


def _validate_staging_files(staging, expected_files):
    expected = {path.as_posix() for path in expected_files}
    unexpected = []
    for path in staging.rglob("*"):
        relative = path.relative_to(staging).as_posix()
        if (
            relative == ".runlogs"
            or relative == _HOST_BRIDGE_REL
            or relative.startswith(_HOST_BRIDGE_REL + "/")
        ):
            # Host-bridge request/response files are runner plumbing, not outputs.
            continue
        if path.is_symlink():
            unexpected.append(relative)
        elif path.is_file():
            if relative not in expected:
                unexpected.append(relative)
        elif not path.is_dir():
            unexpected.append(relative)
    if unexpected:
        raise SubagentError("subagent 创建了未允许的文件: " + ", ".join(sorted(unexpected)))


def run_edit_task(
    folder,
    task,
    *,
    task_name,
    allowed_files,
    input_files=None,
    required_files=None,
    remove_missing_outputs=False,
    model=None,
    timeout=None,
):
    """Run an edit subagent in staging and commit only validated outputs."""
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise SubagentError(f"单集目录不存在: {folder}")

    allowed = _scoped_paths(folder, allowed_files, "allowed_files")
    if not allowed:
        raise SubagentError("allowed_files 不能为空")

    if input_files is None:
        inputs = _infer_mentioned_inputs(folder, task, allowed)
    else:
        inputs = _scoped_paths(folder, input_files, "input_files")
    required = _scoped_paths(folder, required_files, "required_files")

    allowed_set = {path.as_posix() for path in allowed}
    input_set = {path.as_posix() for path in inputs}
    required_set = {path.as_posix() for path in required}
    overlap = allowed_set & input_set
    if overlap:
        raise SubagentError("input_files 与 allowed_files 不能重叠: " + ", ".join(sorted(overlap)))
    if not required_set <= allowed_set:
        invalid = sorted(required_set - allowed_set)
        raise SubagentError("required_files 必须属于 allowed_files: " + ", ".join(invalid))

    input_snapshots = {}
    for relative in inputs:
        source = folder / relative
        if not source.exists():
            raise SubagentError(f"input_files 不存在: {relative}")
        input_snapshots[relative.as_posix()] = _file_snapshot(source, "input_files")
    output_snapshots = {
        relative.as_posix(): _file_snapshot(folder / relative, "allowed_files")
        for relative in allowed
    }

    input_list = "\n".join(f"- {path.as_posix()}（只读）" for path in inputs) or "- 无"
    output_list = "\n".join(f"- {path.as_posix()}" for path in allowed)
    scoped_task = f"""{task}

当前工作目录是隔离的临时 staging，不是真实单集目录。
只允许读取以下输入文件，禁止修改或删除：
{input_list}

只允许创建、修改或按任务要求删除以下输出文件：
{output_list}

完成后检查这些文件确实存在且内容可被下游脚本解析。
不要创建、修改或删除列表之外的任何文件。
"""
    with tempfile.TemporaryDirectory(prefix=f"podcast-subagent-staging-{task_name}-") as tmp:
        staging = Path(tmp) / "workspace"
        staging.mkdir()

        for relative in inputs:
            _copy_into_staging(folder / relative, staging / relative)
        for relative in allowed:
            source = folder / relative
            if source.exists() and not remove_missing_outputs:
                _copy_into_staging(source, staging / relative)

        result = _run(
            staging,
            scoped_task,
            task_name=task_name,
            write_files=True,
            model=model,
            timeout=timeout,
        )

        _validate_staging_files(staging, [*inputs, *allowed])
        for relative in inputs:
            staged_input = staging / relative
            expected = input_snapshots[relative.as_posix()]
            _assert_snapshot(staged_input, expected, "input_files")
            _assert_snapshot(folder / relative, expected, "真实 input_files")

        staged_outputs = {}
        missing_outputs = []
        for relative in allowed:
            staged_output = staging / relative
            if staged_output.exists():
                staged_outputs[relative.as_posix()] = _regular_file_bytes(
                    staged_output, "allowed_files"
                )
            else:
                missing_outputs.append(relative)

        missing_required = sorted(
            relative.as_posix()
            for relative in missing_outputs
            if relative.as_posix() in required_set
        )
        if missing_required:
            raise SubagentError("subagent 缺少必需输出: " + ", ".join(missing_required))

        for relative in allowed:
            _assert_snapshot(
                folder / relative,
                output_snapshots[relative.as_posix()],
                "真实 allowed_files",
            )

        committed = []
        removed = []
        for relative in allowed:
            key = relative.as_posix()
            if key not in staged_outputs:
                continue
            data = staged_outputs[key]
            staged_sha256 = hashlib.sha256(data).hexdigest()
            if output_snapshots[key]["exists"] and output_snapshots[key]["sha256"] == staged_sha256:
                continue
            atomic_write_bytes(folder / relative, data)
            committed.append(key)

        if remove_missing_outputs:
            for relative in missing_outputs:
                target = folder / relative
                if target.exists():
                    target.unlink()
                    removed.append(relative.as_posix())

    result["allowed_files"] = [path.as_posix() for path in allowed]
    result["input_files"] = [path.as_posix() for path in inputs]
    result["committed_files"] = committed
    result["removed_files"] = removed
    return result
