"""Lightweight per-episode release identity and state tracking."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json
    from episode import page_path
except ImportError:
    from scripts.atomic_io import atomic_write_json
    from scripts.episode import page_path


RELEASE_SCHEMA_VERSION = 1
RELEASE_FILENAME = "release.json"
RELEASE_SUCCESS_STATES = (
    "prepared",
    "uploaded",
    "site_ready",
    "deployed",
    "published",
)
RELEASE_STATES = frozenset((*RELEASE_SUCCESS_STATES, "failed"))


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_release(folder):
    path = Path(folder) / RELEASE_FILENAME
    if not path.exists():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def prepare_release(folder, mp3_path, briefing_path):
    folder = Path(folder)
    mp3_path = Path(mp3_path)
    briefing_path = Path(briefing_path)
    audio_sha256 = _sha256_file(mp3_path)
    briefing_sha256 = _sha256_file(briefing_path)
    slug = page_path(folder)
    release_id = hashlib.sha256(
        f"{slug}\n{audio_sha256}\n{briefing_sha256}".encode("utf-8")
    ).hexdigest()[:16]
    previous = load_release(folder)
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
