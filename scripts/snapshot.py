"""Shared canonical snapshot used by review, quality and publishing."""
import hashlib, json
from pathlib import Path
FILES=("转录_纠错.txt","content_map.json","事实核查.json","editorial_fact_checks.json","中文完整笔记.md","讲书稿.md","ai_review.json","quality_report.json","artifact_manifest.json")
def canonical_snapshot(folder):
    folder=Path(folder); items={}
    for name in FILES:
        p=folder/name
        if p.is_file(): items[name]=hashlib.sha256(p.read_bytes()).hexdigest()
    return items
def snapshot_sha256(folder):
    return hashlib.sha256(json.dumps(canonical_snapshot(folder),sort_keys=True,separators=(',',':')).encode()).hexdigest()
def assert_snapshot(folder, expected):
    actual=canonical_snapshot(folder)
    if actual != expected: raise ValueError("artifact snapshot changed")
    return True
