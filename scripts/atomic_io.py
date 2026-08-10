"""Atomic file writes and process-level file locks."""
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

import fcntl


def _target_mode(path):
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return 0o644


def _fsync_path(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


@contextmanager
def atomic_output_path(path):
    """Yield a same-directory staging path and replace the target on success."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=f".tmp{path.suffix}",
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        yield tmp_path
        _fsync_path(tmp_path)
        os.chmod(tmp_path, _target_mode(path))
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_bytes(path, data):
    with atomic_output_path(path) as tmp_path:
        tmp_path.write_bytes(data)


def atomic_write_text(path, text, encoding="utf-8"):
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path, payload):
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _lock_path(key):
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    root = Path(tempfile.gettempdir()) / "podcast-pipeline-locks"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.lock"


@contextmanager
def exclusive_file_lock(key, blocking=True):
    """Hold a cross-process exclusive lock identified by an arbitrary key."""
    path = _lock_path(key)
    with path.open("a+", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise RuntimeError(f"资源正在被另一个进程处理: {key}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
