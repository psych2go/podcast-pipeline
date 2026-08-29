"""Persistent per-episode pipeline run metrics."""
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json, exclusive_file_lock
except ImportError:
    from scripts.atomic_io import atomic_write_json, exclusive_file_lock


RUN_REPORT_SCHEMA_VERSION = 1
MAX_RUN_HISTORY = 100


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _process_is_running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _load_report(path, episode):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if payload.get("schema_version") != RUN_REPORT_SCHEMA_VERSION:
        payload = {}
    payload.setdefault("schema_version", RUN_REPORT_SCHEMA_VERSION)
    payload.setdefault("episode", episode)
    payload.setdefault("runs", [])
    for run in payload["runs"]:
        if (
                run.get("status") == "running"
                and not _process_is_running(run.get("pid"))):
            run["status"] = "failed"
            run["completed_at"] = _utc_now()
            run["error"] = "previous process ended before recording completion"
            for stage in run.get("stages", []):
                if stage.get("status") == "running":
                    stage["status"] = "failed"
                    stage["completed_at"] = run["completed_at"]
                    stage["error"] = run["error"]
    return payload


def _write_report(path, payload):
    path = Path(path)
    atomic_write_json(path, payload)


def _error_text(error):
    return str(error) or type(error).__name__


def _emit_progress(event):
    if os.environ.get("PIPELINE_PROGRESS_EVENTS", "1") == "0":
        return
    print(
        "[进度] " + json.dumps(event, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


class RunStage:
    def __init__(self, report, name, metrics=None):
        self.report = report
        self.payload = {
            "id": uuid.uuid4().hex,
            "name": name,
            "started_at": _utc_now(),
            "status": "running",
            "metrics": dict(metrics or {}),
        }
        self._started = time.monotonic()
        self._closed = False
        self.report._start_stage(self.payload)
        _emit_progress({
            "run_id": self.report.run["id"],
            "stage": name,
            "status": "running",
            "started_at": self.payload["started_at"],
        })

    @property
    def metrics(self):
        return self.payload["metrics"]

    def fail(self, error):
        self._close("failed", _error_text(error))

    def succeed(self):
        self._close("passed", None)

    def _close(self, status, error):
        if self._closed:
            return
        self.payload["completed_at"] = _utc_now()
        self.payload["duration_seconds"] = round(
            time.monotonic() - self._started, 3)
        self.payload["status"] = status
        if error:
            self.payload["error"] = error
        self.report._update_stage(self.payload)
        _emit_progress({
            "run_id": self.report.run["id"],
            "stage": self.payload["name"],
            "status": status,
            "duration_seconds": self.payload["duration_seconds"],
            "completed_at": self.payload["completed_at"],
        })
        self._closed = True


class RunReport:
    """Append one command invocation to ``run_report.json``."""

    def __init__(self, folder, command, metadata=None):
        self.folder = Path(folder)
        self.path = self.folder / "run_report.json"
        self.payload = _load_report(self.path, self.folder.name)
        self.run = {
            "id": uuid.uuid4().hex,
            "pid": os.getpid(),
            "command": command,
            "started_at": _utc_now(),
            "status": "running",
            "metadata": dict(metadata or {}),
            "stages": [],
        }
        self._started = time.monotonic()
        self._finished = False
        self._persist()

    def _persist(self):
        with exclusive_file_lock(f"run-report:{self.path.resolve()}"):
            payload = _load_report(self.path, self.folder.name)
            existing = [
                item for item in payload.get("runs", [])
                if item.get("id") != self.run["id"]
            ]
            payload["runs"] = (existing + [self.run])[-MAX_RUN_HISTORY:]
            payload["episode"] = self.folder.name
            payload["updated_at"] = _utc_now()
            _write_report(self.path, payload)
            self.payload = payload

    def _start_stage(self, stage):
        self.run["stages"].append(stage)
        self._persist()

    def _update_stage(self, stage):
        stage_id = stage.get("id")
        for index, existing in enumerate(self.run["stages"]):
            if existing.get("id") == stage_id:
                self.run["stages"][index] = stage
                break
        else:
            self.run["stages"].append(stage)
        self._persist()

    @contextmanager
    def stage(self, name, metrics=None):
        stage = RunStage(self, name, metrics)
        try:
            yield stage
        except BaseException as exc:
            stage.fail(exc)
            raise
        else:
            stage.succeed()

    def finish(self, passed, error=None, metrics=None):
        if self._finished:
            return
        self.run["completed_at"] = _utc_now()
        self.run["duration_seconds"] = round(
            time.monotonic() - self._started, 3)
        self.run["status"] = "passed" if passed else "failed"
        if error:
            self.run["error"] = _error_text(error)
        if metrics:
            self.run["metrics"] = dict(metrics)
        self._finished = True
        self._persist()
        _emit_progress({
            "run_id": self.run["id"],
            "command": self.run["command"],
            "status": self.run["status"],
            "duration_seconds": self.run["duration_seconds"],
            "completed_at": self.run["completed_at"],
        })
