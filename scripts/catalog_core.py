"""Podcast catalog ledger, source metadata, and episode statistics."""
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from atomic_io import atomic_write_text
    from episode import display_title as episode_display_title, source_metadata
    from sources import source_label
except ImportError:
    from scripts.atomic_io import atomic_write_text
    from scripts.episode import display_title as episode_display_title, source_metadata
    from scripts.sources import source_label

BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = BASE_DIR / "content"
SITE_DIR = BASE_DIR / "site"
CATALOG = CONTENT_DIR / "播客目录.md"


@dataclass(frozen=True)
class CatalogPaths:
    base_dir: Path
    content_dir: Path
    site_dir: Path
    catalog: Path


def configure_paths(paths):
    """Configure the direct module adapter with one immutable path set."""
    global BASE_DIR, CONTENT_DIR, SITE_DIR, CATALOG
    BASE_DIR = Path(paths.base_dir)
    CONTENT_DIR = Path(paths.content_dir)
    SITE_DIR = Path(paths.site_dir)
    CATALOG = Path(paths.catalog)


MAX_DURATION_MB_PER_MIN = 1.2
_AUDIO_DURATION_CACHE = {}
CATALOG_HEADER = (
    "# 播客处理台账\n\n"
    "| # | 播客 | 转录来源 | 讲稿字数 | 音频时长 |\n"
    "|---|------|---------|--------|---------|\n"
)
def _zh_chars(s):
    return len(re.findall(r"[一-鿿]", s))

def _find_briefing(folder):
    """定位讲稿文件（兼容 讲书稿.md / 简报.md 命名）。"""
    for cand in ("讲书稿.md", f"{folder.name} - 讲书稿.md",
                 "简报.md", f"{folder.name} - 简报.md"):
        p = folder / cand
        if p.exists():
            return p
    hits = sorted(folder.glob("*讲书稿.md")) or sorted(
        folder.glob("*简报.md"))
    return hits[0] if hits else None

def _gen_mp3(folder):
    """该期生成的 MP3（排除 原始音频.mp3）。"""
    for f in sorted(os.listdir(folder)):
        if f.endswith(".mp3") and "原始音频" not in f:
            return folder / f
    return None

def _audio_duration_minutes(mp3):
    """Read duration once per file revision, then reuse the ffprobe result."""
    mp3 = Path(mp3)
    cache_key = None
    try:
        stat = mp3.stat()
        cache_key = (str(mp3.resolve()), stat.st_mtime_ns, stat.st_size)
        cached = _AUDIO_DURATION_CACHE.get(cache_key)
        if cached is not None:
            return cached
    except OSError:
        stat = None

    duration = None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(mp3),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            seconds = float(result.stdout.strip())
            if seconds > 0:
                duration = max(1, round(seconds / 60))
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    if duration is None:
        size = stat.st_size if stat is not None else mp3.stat().st_size
        duration = round(size / (1024 * 1024) / MAX_DURATION_MB_PER_MIN)
    if cache_key is not None:
        _AUDIO_DURATION_CACHE[cache_key] = duration
    return duration

def episode_stats(name):
    """返回 {chars, duration}。讲稿或 mp3 缺失时字段为 0。"""
    folder = CONTENT_DIR / name
    chars = 0
    md = _find_briefing(folder)
    if md:
        chars = _zh_chars(md.read_text(encoding="utf-8"))
    mp3 = _gen_mp3(folder)
    duration = 0
    if mp3:
        duration = _audio_duration_minutes(mp3)
    return {"chars": chars, "duration": duration}

def _read_source(name):
    """从 episode.json 读取来源；旧期自动回退 来源.md。"""
    episode_source = source_metadata(CONTENT_DIR / name)
    if episode_source.get("url"):
        return (
            episode_source["url"],
            episode_source.get("label") or source_label(
                episode_source["url"]),
        )
    src = CONTENT_DIR / name / "来源.md"
    if not src.exists():
        return None, None
    text = src.read_text(encoding="utf-8")
    m = re.search(r"- (?:链接|转录来源)：(\S+)", text)
    if not m:
        return None, None
    url = m.group(1).strip()
    return url, source_label(url)

def _source_cell(name):
    url, label = _read_source(name)
    return f"[{label}]({url})" if url else ""

def _display_title(name):
    try:
        return episode_display_title(CONTENT_DIR / name)
    except Exception:
        return name


def add_to_catalog(name):
    """Validate one episode and rebuild the complete catalog."""
    folder = CONTENT_DIR / name
    if not folder.is_dir():
        raise SystemExit(f"[错误] 找不到播客目录: {folder}")
    if not _find_briefing(folder):
        raise SystemExit(f"[错误] 找不到讲稿: {folder}")
    return rebuild_catalog()

def _load_site_entries():
    path = SITE_DIR / "site.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return entries if isinstance(entries, list) else []

def _episode_dirs():
    """Return publishable episode directories for direct core use."""
    names = []
    if not CONTENT_DIR.exists():
        return names
    for name in sorted(os.listdir(CONTENT_DIR), reverse=True):
        folder = CONTENT_DIR / name
        if not folder.is_dir() or name == ".claude":
            continue
        if _find_briefing(folder):
            names.append(name)
    return names


def _ordered_episode_names():
    available = _episode_dirs()
    available_set = set(available)
    ordered = []
    for entry in _load_site_entries():
        name = entry.get("folder")
        if name in available_set and name not in ordered:
            ordered.append(name)
    ordered.extend(name for name in available if name not in ordered)
    return ordered

def _catalog_text(names):
    rows = []
    for number, name in enumerate(names, start=1):
        stats = episode_stats(name)
        title = _display_title(name).replace("|", r"\|")
        rows.append(
            f"| {number} | {title} | {_source_cell(name)} | "
            f"{stats['chars']//1000}K字 | {stats['duration']}min |"
        )
    return CATALOG_HEADER + "\n".join(rows) + ("\n" if rows else "")

def rebuild_catalog():
    """从 episode/content/site 顺序全量重建播客台账。"""
    names = _ordered_episode_names()
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(CATALOG, _catalog_text(names))
    print(f"[台账] 已全量重建 {len(names)} 期 → {CATALOG.name}")
    return names
