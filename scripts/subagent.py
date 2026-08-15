"""Subagent runner used by the podcast pipeline.

The pipeline owns orchestration and validation.  Subagents may either return a
structured JSON result or edit a narrowly scoped set of files.  The default
runner is ``codex exec``; callers can replace it with a compatible command via
``SUBAGENT_COMMAND`` without changing pipeline code.
"""
import hashlib
import copy
import json
import os
import signal
import shlex
import shutil
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

try:
    from atomic_io import atomic_write_bytes
    from retry import exponential_delay
except ImportError:
    from scripts.atomic_io import atomic_write_bytes
    from scripts.retry import exponential_delay


class SubagentError(RuntimeError):
    """Raised when a subagent cannot complete a pipeline task."""


_SAFE_CODEX_CONFIG_KEYS = frozenset({
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
})

_SAFE_MODEL_PROVIDER_KEYS = frozenset({
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
})


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
    raw = raw if raw is not None else os.environ.get(
        "SUBAGENT_COMMAND", "codex")
    command = shlex.split(raw)
    if not command:
        raise SubagentError("SUBAGENT_COMMAND 为空")
    executable = shutil.which(command[0])
    if not executable:
        raise SubagentError(
            f"找不到 subagent runner: {command[0]!r}；"
            "请配置 SUBAGENT_COMMAND"
        )
    command[0] = executable
    if Path(executable).name == "codex" and (
            len(command) < 2 or command[1] != "exec"):
        command.insert(1, "exec")
    if any("claude" in Path(part).name.lower() for part in command):
        raise SubagentError(
            "当前流水线禁止将 Claude CLI 作为 subagent runner；"
            "请使用 codex exec 或兼容的 SUBAGENT_COMMAND"
        )
    return command


def _runner_commands():
    commands = [_runner_command()]
    fallback = os.environ.get("SUBAGENT_FALLBACK_COMMAND", "").strip()
    if fallback:
        fallback_command = _runner_command(fallback)
        if fallback_command != commands[0]:
            commands.append(fallback_command)
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
    raise SubagentError(
        f"subagent 返回的不是有效 JSON: {text[-1000:]}"
    )


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
    raise SubagentError(
        f"隔离 Codex 配置包含不支持的值类型: {type(value).__name__}")


def _toml_text(payload):
    """Serialize the small, sanitized Codex config subset we retain."""
    lines = []

    def emit(mapping, path=()):
        scalars = [
            (key, value) for key, value in mapping.items()
            if not isinstance(value, dict)
        ]
        tables = [
            (key, value) for key, value in mapping.items()
            if isinstance(value, dict)
        ]
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
        raise SubagentError(
            f"Codex model_catalog_json 不可安全复制: {source}")
    destination_name = (
        f"{prefix}-{source.name}" if prefix else source.name)
    destination = isolated / destination_name
    shutil.copy2(source, destination)
    destination.chmod(0o600)
    config["model_catalog_json"] = destination_name


def _sanitized_codex_config(source_path, source_home, isolated, *, prefix=""):
    try:
        raw = tomllib.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SubagentError(
            f"无法读取 Codex 配置 {source_path}: {exc}") from exc
    sanitized = {
        key: copy.deepcopy(raw[key])
        for key in _SAFE_CODEX_CONFIG_KEYS
        if key in raw
    }
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
    _copy_model_catalog(
        sanitized, source_home, isolated, prefix=prefix)
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
        sanitized = _sanitized_codex_config(
            config_path, source_home, isolated)
        target = isolated / "config.toml"
        target.write_text(_toml_text(sanitized), encoding="utf-8")
        target.chmod(0o600)
    for profile in _profile_names(command):
        source = source_home / f"{profile}.config.toml"
        if not source.exists():
            raise SubagentError(f"Codex profile 配置不存在: {source}")
        sanitized = _sanitized_codex_config(
            source, source_home, isolated, prefix=profile)
        target = isolated / f"{profile}.config.toml"
        target.write_text(_toml_text(sanitized), encoding="utf-8")
        target.chmod(0o600)


_RUNNER_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM",
    "LANG", "LANGUAGE", "TMPDIR", "TEMP", "TMP", "TZ", "NO_COLOR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR", "GIT_PAGER", "PAGER",
    # Model credentials are necessary when auth.json is not used. Pipeline
    # credentials such as FISH_KEY/HF_TOKEN/Cloudflare tokens are excluded.
    "OPENAI_API_KEY", "CODEX_API_KEY",
})


def _configured_provider_env_keys(home, command):
    keys = set()
    paths = [Path(home) / "config.toml"]
    paths.extend(
        Path(home) / f"{profile}.config.toml"
        for profile in _profile_names(command)
    )
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
        key for key in source_env
        if key.startswith("LC_") or key.startswith("SUBAGENT_")
    )
    return {key: value for key, value in source_env.items() if key in allowed}


def _runner_environment(tmp, command):
    """Isolate runner config and expose only runtime/provider environment."""
    source_env = os.environ.copy()
    is_codex = Path(command[0]).name == "codex"
    source = Path(
        source_env.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    configured = source_env.get("SUBAGENT_CODEX_HOME", "").strip()
    provider_home = (
        Path(configured).expanduser().resolve() if configured else source
    )
    provider_keys = (
        _configured_provider_env_keys(provider_home, command)
        if is_codex else set()
    )
    env = _filtered_runner_environment(
        source_env, provider_keys=provider_keys)
    if not is_codex:
        return env

    inherit = source_env.get(
        "SUBAGENT_INHERIT_CODEX_HOME", "").strip().lower() in {
            "1", "true", "yes", "on",
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
):
    folder = Path(folder).resolve()
    commands = _runner_commands()
    timeout = timeout or int(os.environ.get("SUBAGENT_TIMEOUT", "1800"))
    max_retries = int(os.environ.get("SUBAGENT_MAX_RETRIES", "2"))
    output_path = None
    try:
        with tempfile.TemporaryDirectory(
                prefix=f"podcast-subagent-{task_name}-") as tmp:
            if schema_path:
                schema_path = Path(schema_path).resolve()
            else:
                schema_path = None
            output_path = Path(tmp) / "last_message.txt"
            last_detail = ""
            total_duration_ms = 0
            total_retries = 0
            for runner_index, command in enumerate(commands):
                runner_env = _runner_environment(tmp, command)
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
                    if (
                            Path(command[0]).name == "codex"
                            and len(cmd) > 1
                            and cmd[1] == "exec"):
                        cmd.insert(1, "--search")
                    else:
                        cmd.append("--search")
                if model:
                    cmd.extend(["--model", model])
                if schema_path:
                    cmd.extend(["--output-schema", str(schema_path)])
                cmd.append(_base_prompt(task))

                for attempt in range(max_retries + 1):
                    output_path.unlink(missing_ok=True)
                    started = time.monotonic()
                    try:
                        result = _run_process(
                            cmd,
                            cwd=folder,
                            env=runner_env,
                            timeout=timeout,
                        )
                    except subprocess.TimeoutExpired:
                        total_duration_ms += round(
                            (time.monotonic() - started) * 1000)
                        last_detail = f"timeout after {timeout}s"
                    else:
                        total_duration_ms += round(
                            (time.monotonic() - started) * 1000)
                        if result.returncode == 0:
                            response = (
                                output_path.read_text(encoding="utf-8")
                                if output_path.exists()
                                else result.stdout
                            )
                            return {
                                "response": response,
                                "duration_ms": total_duration_ms,
                                "retry_count": total_retries,
                                "runner_index": runner_index,
                                "command": " ".join(command),
                                "model": model or "",
                                "task_name": task_name,
                            }
                        last_detail = (
                            result.stderr or result.stdout or ""
                        ).strip()[-1500:]
                    if attempt < max_retries:
                        total_retries += 1
                        wait = exponential_delay(attempt + 1, 2.0)
                        print(
                            f"[subagent] {task_name} 失败，{wait:g}s 后重试 "
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
                f"{task_name} subagent 失败，所有 runner 均不可用: "
                f"{last_detail}"
            )
    except subprocess.TimeoutExpired as exc:
        raise SubagentError(
            f"{task_name} subagent 超时 ({timeout}s)"
        ) from exc
    except OSError as exc:
        raise SubagentError(f"{task_name} subagent 启动失败: {exc}") from exc


def run_json_task(
        folder,
        task,
        schema_path,
        *,
        task_name,
        enable_search=False,
        model=None,
        timeout=None,
):
    """Run a read-only subagent and parse its structured JSON response."""
    temporary_schema = None
    schema_disabled = os.environ.get(
        "SUBAGENT_DISABLE_OUTPUT_SCHEMA", "").strip().lower() in {
            "1", "true", "yes", "on"
        }
    if schema_disabled:
        runner_schema = None
    else:
        if isinstance(schema_path, dict):
            raw_schema = schema_path
        else:
            raw_schema = json.loads(
                Path(schema_path).read_text(encoding="utf-8"))
        temporary_schema = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False)
        json.dump(
            prepare_output_schema(raw_schema),
            temporary_schema,
            ensure_ascii=False,
        )
        temporary_schema.close()
        runner_schema = temporary_schema.name
    try:
        result = _run(
            folder,
            task,
            task_name=task_name,
            schema_path=runner_schema,
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
            raise SubagentError(
                f"{label} 不在单集目录内: {value}"
            ) from exc
        if relative == Path("."):
            raise SubagentError(f"{label} 不能是单集目录本身")

        current = folder
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise SubagentError(
                    f"{label} 不允许经过符号链接: {relative}"
                )

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
        if (
                relative.as_posix() not in excluded
                and relative.as_posix() in task):
            mentioned.append(relative)
    return mentioned


def _copy_into_staging(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _validate_staging_files(staging, expected_files):
    expected = {path.as_posix() for path in expected_files}
    unexpected = []
    for path in staging.rglob("*"):
        relative = path.relative_to(staging).as_posix()
        if path.is_symlink():
            unexpected.append(relative)
        elif path.is_file():
            if relative not in expected:
                unexpected.append(relative)
        elif not path.is_dir():
            unexpected.append(relative)
    if unexpected:
        raise SubagentError(
            "subagent 创建了未允许的文件: "
            + ", ".join(sorted(unexpected))
        )


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
        raise SubagentError(
            "input_files 与 allowed_files 不能重叠: "
            + ", ".join(sorted(overlap))
        )
    if not required_set <= allowed_set:
        invalid = sorted(required_set - allowed_set)
        raise SubagentError(
            "required_files 必须属于 allowed_files: "
            + ", ".join(invalid)
        )

    input_snapshots = {}
    for relative in inputs:
        source = folder / relative
        if not source.exists():
            raise SubagentError(f"input_files 不存在: {relative}")
        input_snapshots[relative.as_posix()] = _file_snapshot(
            source, "input_files")
    output_snapshots = {
        relative.as_posix(): _file_snapshot(
            folder / relative, "allowed_files")
        for relative in allowed
    }

    input_list = "\n".join(
        f"- {path.as_posix()}（只读）" for path in inputs
    ) or "- 无"
    output_list = "\n".join(
        f"- {path.as_posix()}" for path in allowed
    )
    scoped_task = f"""{task}

当前工作目录是隔离的临时 staging，不是真实单集目录。
只允许读取以下输入文件，禁止修改或删除：
{input_list}

只允许创建、修改或按任务要求删除以下输出文件：
{output_list}

完成后检查这些文件确实存在且内容可被下游脚本解析。
不要创建、修改或删除列表之外的任何文件。
"""
    with tempfile.TemporaryDirectory(
            prefix=f"podcast-subagent-staging-{task_name}-") as tmp:
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
            _assert_snapshot(
                folder / relative, expected, "真实 input_files")

        staged_outputs = {}
        missing_outputs = []
        for relative in allowed:
            staged_output = staging / relative
            if staged_output.exists():
                staged_outputs[relative.as_posix()] = _regular_file_bytes(
                    staged_output, "allowed_files")
            else:
                missing_outputs.append(relative)

        missing_required = sorted(
            relative.as_posix()
            for relative in missing_outputs
            if relative.as_posix() in required_set
        )
        if missing_required:
            raise SubagentError(
                "subagent 缺少必需输出: " + ", ".join(missing_required)
            )

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
            if (
                    output_snapshots[key]["exists"]
                    and output_snapshots[key]["sha256"] == staged_sha256):
                continue
            atomic_write_bytes(folder / relative, data)
            committed.append(key)

        if remove_missing_outputs:
            for relative in missing_outputs:
                target = folder / relative
                if target.exists():
                    target.unlink()
                    removed.append(relative.as_posix())

    result["allowed_files"] = [
        path.as_posix() for path in allowed
    ]
    result["input_files"] = [
        path.as_posix() for path in inputs
    ]
    result["committed_files"] = committed
    result["removed_files"] = removed
    return result
