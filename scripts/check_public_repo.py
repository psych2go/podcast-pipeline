#!/usr/bin/env python3
"""Reject files that must not be present in a public Git tree."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_SITE_FILES = {"site/deploy.sh", "site/wrangler.toml"}
PRIVATE_PREFIXES = (
    "content/",
    "reports/",
    ".runlogs/",
    ".wrangler/",
    ".claude/",
    ".codex/",
    ".agents/",
    ".venv/",
    ".venv-alignment/",
)
PRIVATE_EXACT = {
    ".env",
    ".mcp.json",
    "skills-lock.json",
    "test.txt",
}
PRIVATE_MEDIA_SUFFIXES = {".mp3", ".m4a", ".aac", ".flac", ".wav"}
PRIVATE_ARTIFACT_NAMES = {
    "原始转录.txt",
    "转录_纠错.txt",
    "讲书稿.md",
    "中文完整笔记.md",
    "transcript.raw.json",
    "content_map.json",
    "summary_map.json",
    "ai_review.json",
    "quality_report.json",
    "tts_manifest.json",
    "publish_report.json",
}
PRIVATE_BRANCH_PREFIXES = ("private-", "private/")
LOCAL_PATH_PATTERNS = (
    re.compile("/" + "home" + r"/[^/\s]+/"),
    re.compile("/" + "Users" + r"/[^/\s]+/"),
    re.compile(r"[A-Za-z]:\\" + "Users" + r"\\[^\\\s]+\\"),
)
MAX_TEXT_SCAN_BYTES = 2 * 1024 * 1024


def _git(*args: str) -> bytes:
    return subprocess.check_output(
        ["git", *args],
        cwd=ROOT,
        stderr=subprocess.STDOUT,
    )


def tracked_files() -> list[str]:
    raw = _git("ls-files", "-z")
    return [item.decode("utf-8") for item in raw.split(b"\0") if item]


def current_branch() -> str:
    return _git("branch", "--show-current").decode("utf-8").strip()


def branch_context() -> str:
    branch = current_branch()
    if branch:
        return branch
    return (
        os.environ.get("GITHUB_HEAD_REF", "").strip()
        or os.environ.get("GITHUB_REF_NAME", "").strip()
    )


def is_private_branch(branch: str) -> bool:
    normalized = str(branch or "").removeprefix("refs/heads/")
    if normalized.startswith("origin/"):
        normalized = normalized[len("origin/"):]
    return normalized.startswith(PRIVATE_BRANCH_PREFIXES)


def privacy_reason(path: str) -> str | None:
    if path in PRIVATE_EXACT:
        return "local secret/tool state"
    if path.startswith(PRIVATE_PREFIXES):
        return "private local directory"
    if path.startswith("site/") and path not in ALLOWED_SITE_FILES:
        return "generated site output"
    if Path(path).suffix.lower() in PRIVATE_MEDIA_SUFFIXES:
        return "source/generated media"
    if Path(path).name in PRIVATE_ARTIFACT_NAMES:
        return "podcast content artifact"
    if "/audio/" in f"/{path}":
        return "audio directory"
    return None


def text_privacy_reason(text: str) -> str | None:
    for pattern in LOCAL_PATH_PATTERNS:
        if pattern.search(text):
            return "machine-local absolute path"
    return None


def find_violations(paths: list[str]) -> list[tuple[str, str]]:
    violations = []
    for path in paths:
        reason = privacy_reason(path)
        if reason:
            violations.append((path, reason))
            continue
        file_path = ROOT / path
        try:
            if not file_path.is_file() or file_path.stat().st_size > MAX_TEXT_SCAN_BYTES:
                continue
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        reason = text_privacy_reason(text)
        if reason:
            violations.append((path, reason))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 Git 索引中是否混入不能公开推送的播客内容或本地状态。"
    )
    parser.add_argument(
        "--allow-private-branch",
        action="store_true",
        help="只检查当前索引；允许在 private-* / private/* 本地分支运行。",
    )
    parser.add_argument(
        "--allow-detached-head",
        action="store_true",
        help="允许无法确定分支身份的 detached HEAD；仅用于人工审计。",
    )
    args = parser.parse_args()

    branch = branch_context()
    branch_is_private = is_private_branch(branch)
    violations = find_violations(tracked_files())

    if violations:
        print("[public-check] 发现不能进入公开 Git tree 的文件：")
        for path, reason in violations:
            print(f"  - {path} ({reason})")
        print("请取消跟踪并保留本地文件，例如：git rm --cached <path>")
        return 1

    if not branch and not args.allow_detached_head:
        print(
            "[public-check] 无法确定 detached HEAD 对应的分支；"
            "拒绝把未知历史当作公开分支。"
        )
        return 3

    if branch_is_private and not args.allow_private_branch:
        print(
            f"[public-check] 当前分支 {branch!r} 被标记为私有分支；"
            "即使当前索引干净，历史也可能包含播客内容，拒绝作为公开分支。"
        )
        return 2

    print(f"[public-check] 通过：{len(tracked_files())} 个已跟踪文件均符合公开边界。")
    if branch_is_private:
        print("[public-check] 注意：仅验证当前索引；未证明私有分支历史已经脱敏。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
