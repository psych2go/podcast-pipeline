"""Canonical per-episode artifact bindings and dependency invalidation."""

import hashlib
import json
from pathlib import Path
from scripts.atomic_io import atomic_write_json

MANIFEST_FILENAME = "artifact_manifest.json"
ARTIFACTS = {
    "raw_transcript": ["原始转录.txt"],
    "corrected_transcript": ["转录_纠错.txt"],
    "correction_manifest": ["转录_纠错.manifest.json"],
    "content_map": ["content_map.json"],
    "editorial_fact_checks": ["editorial_fact_checks.json", "事实核查.json"],
    "notes": ["中文完整笔记.md"],
    "briefing": ["讲书稿.md"],
    "ai_review": ["ai_review.json"],
    "quality_report": ["quality_report.json"],
    "audio": [],
    "html": [],
    "release": ["release.json"],
}
DOWNSTREAM = {
    "raw_transcript": {
        "corrected_transcript",
        "content_map",
        "editorial_fact_checks",
        "notes",
        "briefing",
        "ai_review",
        "quality_report",
        "release",
    },
    "corrected_transcript": {
        "content_map",
        "editorial_fact_checks",
        "notes",
        "briefing",
        "ai_review",
        "quality_report",
        "release",
    },
    "briefing": {"ai_review", "quality_report", "audio", "html", "release"},
    "ai_review": {"quality_report", "release"},
    "quality_report": {"release"},
}


def _sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def build_artifact_manifest(folder, generator_version="1"):
    folder = Path(folder)
    previous = load_manifest(folder)
    old = previous.get("artifacts", {})
    artifacts = {}
    for name, candidates in ARTIFACTS.items():
        paths = [folder / x for x in candidates if (folder / x).is_file()]
        if name == "audio":
            paths = list(folder.glob("*.mp3"))
        if name == "html":
            paths = list(folder.glob("*- content.html"))
        digest = _sha(paths[0]) if paths else ""
        prior = old.get(name, {})
        artifacts[name] = {
            "file": paths[0].name if paths else "",
            "sha256": digest,
            "generator_version": generator_version,
            "status": "present" if digest else "missing",
            "stale_reason": ""
            if not digest or prior.get("sha256", digest) == digest
            else "upstream input changed",
        }
    changed = {n for n, v in artifacts.items() if v["sha256"] != old.get(n, {}).get("sha256")}
    for upstream in changed:
        for downstream in DOWNSTREAM.get(upstream, ()):
            if artifacts[downstream]["status"] == "present":
                artifacts[downstream]["status"] = "stale"
                artifacts[downstream]["stale_reason"] = f"{upstream} changed"
    return {"schema_version": 1, "artifacts": artifacts}


def load_manifest(folder):
    try:
        return json.loads((Path(folder) / MANIFEST_FILENAME).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}


def write_artifact_manifest(folder, **kwargs):
    data = build_artifact_manifest(folder, **kwargs)
    atomic_write_json(Path(folder) / MANIFEST_FILENAME, data)
    return data


def manifest_sha256(folder):
    p = Path(folder) / MANIFEST_FILENAME
    return _sha(p) if p.exists() else ""
