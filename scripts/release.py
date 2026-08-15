"""Lightweight per-episode release identity, provenance, and state tracking."""
import argparse
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

try:
    from hashing import sha256_file
except ImportError:
    from scripts.hashing import sha256_file

try:
    from atomic_io import atomic_write_json
    from episode import page_path
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts.episode import page_path


RELEASE_SCHEMA_VERSION = 2
PIPELINE_VERSION = 8
RELEASE_FILENAME = "release.json"
RELEASE_SUCCESS_STATES = (
    "prepared",
    "uploaded",
    "site_ready",
    "deployed",
    "published",
)
RELEASE_STATES = frozenset((*RELEASE_SUCCESS_STATES, "failed"))


def load_release(folder):
    path = Path(folder) / RELEASE_FILENAME
    if not path.exists():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _git_output(folder, *args):
    result = subprocess.run(
        ["git", "-C", str(folder), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _git_provenance(folder):
    """Fingerprint pipeline-code changes without embedding a giant diff."""
    root_raw = _git_output(folder, "rev-parse", "--show-toplevel")
    if root_raw is None:
        return {
            "git_commit": "",
            "git_dirty": False,
            "git_diff_sha256": "",
        }
    root = Path(root_raw.decode("utf-8", errors="replace").strip())
    commit_raw = _git_output(root, "rev-parse", "HEAD") or b""
    status_raw = _git_output(root, "status", "--porcelain=v1", "-z") or b""
    # Content/site media may be very large and are already hash-bound by the
    # release manifest. This fingerprint is intentionally scoped to pipeline
    # code/config/docs so provenance remains cheap and repeatable.
    diff_raw = _git_output(
        root,
        "diff",
        "--binary",
        "HEAD",
        "--",
        "scripts",
        "tests",
        ".github",
        "docs",
        "benchmarks",
        "CLAUDE.md",
        "AGENTS.md",
        "README.md",
        "README.en.md",
        ".env.example",
        ".gitignore",
        "requirements.txt",
        "requirements-alignment.txt",
        "requirements-asr.txt",
        "requirements-asr-gpu.txt",
        "requirements-benchmark.txt",
        "requirements-browser.txt",
        "requirements-diarization.txt",
        "requirements-tts.txt",
    ) or b""
    untracked_raw = _git_output(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        "scripts",
        "tests",
        ".github",
        "docs",
        "benchmarks",
        "CLAUDE.md",
        "AGENTS.md",
        "README.md",
        "README.en.md",
        ".env.example",
        ".gitignore",
        "requirements.txt",
        "requirements-alignment.txt",
        "requirements-asr.txt",
        "requirements-asr-gpu.txt",
        "requirements-benchmark.txt",
        "requirements-browser.txt",
        "requirements-diarization.txt",
        "requirements-tts.txt",
    ) or b""
    digest = hashlib.sha256()
    digest.update(diff_raw)
    for raw_name in sorted(filter(None, untracked_raw.split(b"\0"))):
        digest.update(b"\0untracked\0" + raw_name + b"\0")
        path = root / raw_name.decode("utf-8", errors="surrogateescape")
        if path.is_file():
            digest.update(sha256_file(path).encode("ascii"))
    return {
        "git_commit": commit_raw.decode("ascii", errors="ignore").strip(),
        "git_dirty": bool(status_raw),
        "git_diff_sha256": digest.hexdigest(),
    }
def prepare_release(folder, mp3_path, briefing_path, *, require_clean=False):
    folder = Path(folder)
    mp3_path = Path(mp3_path)
    briefing_path = Path(briefing_path)
    audio_sha256 = sha256_file(mp3_path)
    briefing_sha256 = sha256_file(briefing_path)
    slug = page_path(folder)
    release_id = hashlib.sha256(
        f"{slug}\n{audio_sha256}\n{briefing_sha256}".encode("utf-8")
    ).hexdigest()[:16]
    previous = load_release(folder)
    provenance = _git_provenance(folder)
    if require_clean and provenance["git_dirty"]:
        raise RuntimeError("Git 工作区不干净，--require-clean 阻断 release 准备")
    payload = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "release_id": release_id,
        "audio_sha256": audio_sha256,
        "briefing_sha256": briefing_sha256,
        "audio_key": f"{slug}/audio/{audio_sha256}.mp3",
        "state": "prepared",
        "last_successful_state": "prepared",
        "previous_release_id": (
            previous.get("release_id", "")
            if previous.get("release_id") != release_id
            else previous.get("previous_release_id", "")
        ),
        "error": "",
        **provenance,
        "pipeline_version": PIPELINE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(folder / RELEASE_FILENAME, payload)
    return payload


def update_release_state(folder, state, *, error="", **extra):
    folder = Path(folder)
    payload = load_release(folder)
    if not payload:
        return {}
    if state not in RELEASE_STATES:
        raise ValueError(f"unknown release state: {state}")
    previous_state = payload.get("state", "")
    if state == "failed":
        last_successful = payload.get("last_successful_state", "")
        if (
                previous_state in RELEASE_SUCCESS_STATES
                and previous_state != "failed"):
            last_successful = previous_state
        payload["last_successful_state"] = (
            last_successful
            if last_successful in RELEASE_SUCCESS_STATES
            else ""
        )
    else:
        payload["last_successful_state"] = state
    payload["state"] = state
    payload["error"] = str(error) if error else ""
    payload.update(extra)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(folder / RELEASE_FILENAME, payload)
    return payload


def active_audio_key(folder, fallback):
    release = load_release(folder)
    if release.get("audio_key"):
        return release["audio_key"]
    return fallback


def main():
    parser = argparse.ArgumentParser(description="准备带 Git provenance 的 release.json")
    parser.add_argument("folder")
    parser.add_argument("mp3")
    parser.add_argument("briefing")
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="Git 工作区存在任何未提交变化时阻断",
    )
    args = parser.parse_args()
    payload = prepare_release(
        args.folder,
        args.mp3,
        args.briefing,
        require_clean=args.require_clean,
    )
    import json
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
