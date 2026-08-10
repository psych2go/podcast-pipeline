"""Persistent per-episode pipeline run metrics."""
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


RUN_REPORT_SCHEMA_VERSION = 1
MAX_RUN_HISTORY = 100


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


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
        if run.get("status") == "running":
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def _error_text(error):
    return str(error) or type(error).__name__


class RunStage:
    def __init__(self, report, name, metrics=None):
        self.report = report
        self.payload = {
            "name": name,
            "started_at": _utc_now(),
            "status": "running",
            "metrics": dict(metrics or {}),
        }
        self._started = time.monotonic()
        self._closed = False

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
        self.report._append_stage(self.payload)
        self._closed = True


class RunReport:
    """Append one command invocation to ``run_report.json``."""

    def __init__(self, folder, command, metadata=None):
        self.folder = Path(folder)
        self.path = self.folder / "run_report.json"
        self.payload = _load_report(self.path, self.folder.name)
        self.run = {
            "id": uuid.uuid4().hex,
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
        payload = dict(self.payload)
        existing = [
            item for item in payload.get("runs", [])
            if item.get("id") != self.run["id"]
        ]
        payload["runs"] = (existing + [self.run])[-MAX_RUN_HISTORY:]
        payload["episode"] = self.folder.name
        payload["updated_at"] = _utc_now()
        _write_report(self.path, payload)
        self.payload = payload

    def _append_stage(self, stage):
        self.run["stages"].append(stage)
        self._persist()

    @contextmanager
    def stage(self, name, metrics=None):
        stage = RunStage(self, name, metrics)
        try:
            yield stage
        except Exception as exc:
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
