#!/usr/bin/env python3
"""
播客台账（content/播客目录.md）与站点清单（site/site.json）维护脚本。

把 CLAUDE.md 第 6、7 步的手贴 python 一行命令脚本化：

用法：
  python scripts/catalog.py stats "<播客名>"            # 打印字数/时长，不写入
  python scripts/catalog.py add "<播客名>"               # 追加/更新一行到 播客目录.md
  python scripts/catalog.py sync-site [--only "<播客名>"]  # 同步 content.html + 重建 site.json

说明：
  - 字数 = 讲书稿.md 的中文字数；时长 = 生成的 mp3 大小按 1.2MB/min 估算
  - 转录来源（台账链接 + site.json 的 source_name/source_url）从 来源.md 读取；
    找不到时台账该列为空、site.json 沿用已有值
  - sync-site 保留 site.json 里已存在的 title / source 字段与顺序，
    不会覆盖手工精修过的展示标题
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from atomic_io import atomic_write_bytes, atomic_write_json, atomic_write_text
from config import (
    PAGES_BASE_URL,
    PAGES_PROJECT,
    R2_BUCKET,
    R2_PUBLIC_URL,
    validate_for_stage,
)
from episode import (
    audio_key as episode_audio_key,
    display_title as episode_display_title,
    legacy_page_path as episode_legacy_page_path,
    page_path as episode_page_path,
    public_audio_url,
    quality_metadata,
    source_metadata,
)
from publish import (
    PUBLISH_REPORT_SCHEMA_VERSION,
    verify_publish,
    write_publish_report,
)
from release import (
    active_audio_key,
    load_release,
    update_release_state,
)
from run_report import RUN_REPORT_SCHEMA_VERSION, RunReport
from sources import source_host, source_label

BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = BASE_DIR / "content"
SITE_DIR = BASE_DIR / "site"
CATALOG = CONTENT_DIR / "播客目录.md"

MAX_DURATION_MB_PER_MIN = 1.2  # 经验值：生成的 mp3 约 1.2MB/分钟

CATALOG_HEADER = (
    "# 播客处理台账\n"
    "\n"
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
    """优先用 ffprobe 读取真实时长；不可用时再回退文件大小估算。"""
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
                return max(1, round(seconds / 60))
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return round(mp3.stat().st_size / (1024 * 1024) / MAX_DURATION_MB_PER_MIN)


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


# ── 台账 ──────────────────────────────────────────────────────────

def _display_title(name):
    try:
        return episode_display_title(CONTENT_DIR / name)
    except Exception:
        return name


def add_to_catalog(name):
    """兼容旧命令；验证目标单集后全量重建台账。"""
    folder = CONTENT_DIR / name
    if not folder.is_dir():
        sys.exit(f"[错误] 找不到播客目录: {folder}")
    if not _find_briefing(folder):
        sys.exit(f"[错误] 找不到讲稿: {folder}")
    rebuild_catalog()


def _load_site_entries():
    path = SITE_DIR / "site.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return entries if isinstance(entries, list) else []


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


def catalog_consistency_errors():
    """验证台账和 site.json 都与当前内容统计一致。"""
    names = _ordered_episode_names()
    errors = []
    try:
        actual_catalog = CATALOG.read_text(encoding="utf-8")
    except OSError:
        actual_catalog = ""
    expected_catalog = _catalog_text(names)
    if actual_catalog != expected_catalog:
        errors.append("播客目录.md 与当前单集统计不一致，需运行 catalog.py rebuild")

    entries = _load_site_entries()
    entry_names = [entry.get("folder") for entry in entries]
    if entry_names != names:
        errors.append("site.json 顺序或单集集合与播客台账不一致")
        return errors
    for name, entry in zip(names, entries):
        expected = _build_entry(name, entry)
        for field in (
                "title", "path", "slug", "duration", "words",
                "source_name", "source_url", "quality_mode"):
            if entry.get(field) != expected.get(field):
                errors.append(
                    f"{name}: site.json.{field}="
                    f"{entry.get(field)!r}，当前应为 {expected.get(field)!r}"
                )
    return errors


# ── 站点清单 ──────────────────────────────────────────────────────

def _episode_dirs():
    names = []
    for d in sorted(os.listdir(CONTENT_DIR), reverse=True):
        folder = CONTENT_DIR / d
        if not folder.is_dir() or d == ".claude":
            continue
        if not _find_briefing(folder):
            continue
        names.append(d)
    return names


def _build_entry(name, prev):
    folder = CONTENT_DIR / name
    stats = episode_stats(name)
    url, label = _read_source(name)
    path = episode_page_path(folder)
    return {
        "title": _display_title(name),
        "folder": name,
        "path": path,
        "slug": path,
        "duration": stats["duration"],
        "words": stats["chars"],
        "source_name": label or prev.get("source_name") or "",
        "source_url": url or prev.get("source_url") or "",
        "quality_mode": quality_metadata(folder).get(
            "mode",
            "strict" if (folder / "content_map.json").exists() else "legacy",
        ),
    }


def _site_readiness_errors(names, existing):
    errors = []
    try:
        from quality_report import build_quality_report
    except ImportError:
        from scripts.quality_report import build_quality_report

    for name in names:
        folder = CONTENT_DIR / name
        mp3 = _gen_mp3(folder)
        html = folder / f"{name} - content.html"
        if not mp3:
            errors.append(f"{name}: 缺少最终 MP3")
        if not html.exists():
            errors.append(f"{name}: 缺少 content.html")

        content_map = folder / "content_map.json"
        if content_map.exists():
            report = build_quality_report(folder, strict=True)
            if not report.get("passed", False):
                detail = "; ".join(report.get("errors", [])[:3])
                errors.append(f"{name}: 严格质量门未通过: {detail}")
        elif name not in existing:
            errors.append(
                f"{name}: 新期缺少 content_map.json，不能进入站点")
    return errors


def sync_site(only=None):
    """重建 site.json（全部期），并按需拷贝 content.html 到 site/{name}/。

    保留已有 site.json 里手工精修的 title / source 字段与出现顺序，
    只更新字数/时长，新期追加在末尾。
    --only 只限制 content.html 的拷贝范围，site.json 始终重建全部期。
    """
    existing = {}
    site_json_path = SITE_DIR / "site.json"
    if site_json_path.exists():
        try:
            for e in json.loads(site_json_path.read_text(encoding="utf-8")):
                existing[e.get("folder")] = e
        except Exception:
            existing = {}

    names = _ordered_episode_names()
    if only and only not in names:
        sys.exit(f"[错误] 找不到播客目录（或没有讲稿）: {only}")
    readiness_errors = _site_readiness_errors(names, existing)
    if readiness_errors:
        for error in readiness_errors:
            print(f"[站点][阻断] {error}")
        sys.exit("[站点][阻断] 存在未就绪单集，未修改 site/")

    # 拷贝 content.html（--only 只影响这一步）；音频由 R2 公开 URL 提供，不放进 site/
    for name in names:
        if only and name != only:
            continue
        folder = CONTENT_DIR / name
        html = folder / f"{name} - content.html"
        if html.exists():
            paths = {
                episode_page_path(folder),
                episode_legacy_page_path(folder),
            } - {""}
            for public_path in paths:
                destination = SITE_DIR / public_path
                destination.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(
                    destination / "content.html", html.read_bytes())

    # 重建 site.json（始终覆盖全部期）
    eps = [_build_entry(name, existing.get(name, {})) for name in names]

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(site_json_path, eps)
    copied = len(names) if only is None else (1 if only in names else 0)
    print(f"[站点] {len(eps)} 期 → {site_json_path.name}（content.html 已同步 {copied} 期）")


# ── 首页生成 ──────────────────────────────────────────────────────

_HTML_ESCAPE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def _esc(s):
    return s.translate(_HTML_ESCAPE)


def _replace_between(text, start_marker, end_marker, inner):
    s = text.find(start_marker)
    e = text.find(end_marker)
    if s == -1 or e == -1:
        sys.exit(f"[错误] index.html 缺标记: {start_marker} / {end_marker}")
    e_end = text.find("-->", e) + 3
    return text[:s] + start_marker + "\n" + inner + "\n" + end_marker + text[e_end:]


def gen_index():
    """从 site.json 重建首页 index.html 的统计和卡片列表（按标记替换）。"""
    site_json_path = SITE_DIR / "site.json"
    index_path = SITE_DIR / "index.html"
    if not site_json_path.exists():
        sys.exit("[错误] 找不到 site/site.json，先跑 sync-site")
    if not index_path.exists():
        sys.exit(f"[错误] 找不到 {index_path}")
    eps = json.loads(site_json_path.read_text(encoding="utf-8"))
    if not eps:
        sys.exit("[错误] site.json 为空")

    # 统计
    total_min = sum(e["duration"] for e in eps)
    total_words = sum(e["words"] for e in eps)
    stats = (
        f'        <div class="stat"><strong>{len(eps):02d}</strong>'
        f'<span>已收录节目</span></div>\n'
        f'        <div class="stat"><strong>{total_min}</strong>'
        f'<span>分钟中文音频</span></div>\n'
        f'        <div class="stat"><strong>{total_words // 1000}K</strong>'
        f'<span>字深度讲稿</span></div>'
    )

    # 卡片：site.json 保留台账顺序，首页倒序展示，让最新一期位于首屏。
    cards = []
    for display_index, e in enumerate(reversed(eps), start=1):
        href = quote(
            (e.get("path") or e["folder"]) + "/content.html",
            safe="/",
        )
        episode_no = len(eps) - display_index + 1
        title = _esc(e["title"])
        source_name = _esc(e.get("source_name", ""))
        source_url = _esc(e.get("source_url", ""))
        cards.append(
            f'    <article class="episode-card" data-search="{title.lower()} {source_name.lower()}">\n'
            f'        <a class="episode-card-main" href="{href}" '
            f'aria-label="阅读和收听：{title}">\n'
            f'            <div class="episode-card-top">\n'
            f'                <span class="episode-index">{episode_no:02d}</span>\n'
            f'                <span class="episode-format">中文讲稿 · 音频</span>\n'
            f'            </div>\n'
            f'            <h3 class="episode-title">{title}</h3>\n'
            f'            <div class="episode-meta">\n'
            f'                <span>{e["duration"]} 分钟</span>\n'
            f'                <span>{e["words"] // 1000}K 字</span>\n'
            f'            </div>\n'
            f'        </a>\n'
            f'        <div class="episode-card-bottom">\n'
            f'            <a class="episode-open" href="{href}">阅读与收听 '
            f'<span aria-hidden="true">↗</span></a>\n'
            f'            <a href="{source_url}" target="_blank" rel="noopener" '
            f'class="episode-source">{source_name}</a>\n'
            f'        </div>\n'
            f'    </article>')
    cards_html = "\n".join(cards)

    text = index_path.read_text(encoding="utf-8")
    text = _replace_between(text, "<!-- STATS:START -->", "<!-- STATS:END -->", stats)
    text = _replace_between(text, "<!-- CARDS:START -->", "<!-- CARDS:END -->", cards_html)
    atomic_write_text(index_path, text)
    print(f"[首页] {len(eps)} 期卡片 + 统计已更新 → {index_path}")


# ── 来源回填 ──────────────────────────────────────────────────────

def backfill_sources():
    """为缺少 来源.md 的期，从 site.json 回填（标题 + 来源链接）。"""
    site_json_path = SITE_DIR / "site.json"
    if not site_json_path.exists():
        sys.exit("[错误] 找不到 site/site.json")
    entries = json.loads(site_json_path.read_text(encoding="utf-8"))
    written = skipped = 0
    for e in entries:
        name = e["folder"]
        url = e.get("source_url")
        folder = CONTENT_DIR / name
        if not folder.is_dir():
            continue
        src_path = folder / "来源.md"
        if src_path.exists():
            skipped += 1
            continue
        if not url:
            print(f"[来源] 跳过 {name}（无 source_url）")
            skipped += 1
            continue
        md = _find_briefing(folder)
        if md:
            import time as _time
            d = _time.strftime("%Y-%m-%d", _time.localtime(os.path.getmtime(md)))
        else:
            d = "未知"
        atomic_write_text(
            src_path,
            f"# 来源信息\n\n## 原始播客\n- 标题：{name}\n\n## 转录来源\n"
            f"- 链接：{url}\n- 转录来源：{url}\n\n## 处理信息\n"
            f"- 处理日期：{d}\n",
        )
        written += 1
        print(f"[来源] 已回填 {name}")
    print(f"[来源] 回填 {written} 期，跳过 {skipped} 期")


# ── 跨单集健康汇总 ─────────────────────────────────────────────────

def _load_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_since(value):
    match = re.fullmatch(r"(\d+)([dhw])", value.strip().lower())
    if not match:
        raise ValueError("--since 必须使用 24h、7d 或 2w 形式")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("--since 必须大于 0")
    unit = match.group(2)
    return {
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
    }[unit]


def _metric_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(
        value, bool) else 0


def _error_key(value):
    return " ".join(str(value or "").split())[:180]


def _md_cell(value):
    return str(value).replace("|", r"\|").replace("\n", " ")


def build_health_report(content_dir=None, since="7d", now=None):
    """Aggregate valid strict-mode run reports without mutating episode state."""
    content_dir = Path(content_dir or CONTENT_DIR)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - _parse_since(since)
    stage_stats = defaultdict(
        lambda: {"runs": 0, "failures": 0, "durations": []})
    error_counts = Counter()
    source_failures = Counter()
    total_runs = 0
    retry_count = 0
    tls_downgrades = 0
    reported_cost = 0.0
    token_usage = Counter()
    unpublished = []

    for report_path in sorted(content_dir.glob("*/run_report.json")):
        folder = report_path.parent
        episode = _load_json_file(folder / "episode.json")
        if episode.get("quality", {}).get("mode") == "legacy":
            continue
        payload = _load_json_file(report_path)
        if payload.get("schema_version") != RUN_REPORT_SCHEMA_VERSION:
            continue
        eligible_runs = []
        for run in payload.get("runs", []):
            metadata = run.get("metadata", {})
            if (
                    run.get("status") not in {"passed", "failed"}
                    or metadata.get("skip_health")
                    or metadata.get("legacy")):
                continue
            started_at = _parse_timestamp(
                run.get("started_at") or run.get("completed_at"))
            if started_at is None or started_at < cutoff:
                continue
            eligible_runs.append(run)
            total_runs += 1
            source = metadata.get("source", "")
            if run.get("status") == "failed" and source:
                source_failures[source_host(source) or "unknown"] += 1

            stages = run.get("stages") or [{
                "name": run.get("command", "unknown"),
                "status": run.get("status"),
                "duration_seconds": run.get("duration_seconds"),
                "error": run.get("error"),
                "metrics": run.get("metrics", {}),
            }]
            failed_stage_seen = False
            for stage in stages:
                name = stage.get("name") or "unknown"
                stats = stage_stats[name]
                stats["runs"] += 1
                if stage.get("status") == "failed":
                    stats["failures"] += 1
                    failed_stage_seen = True
                    error = _error_key(stage.get("error"))
                    if error:
                        error_counts[error] += 1
                duration = stage.get("duration_seconds")
                if isinstance(duration, (int, float)):
                    stats["durations"].append(duration)
                metrics = stage.get("metrics", {})
                retry_count += int(_metric_number(
                    metrics.get("retry_count")))
                tls_downgrades += int(bool(
                    metrics.get("tls_downgrade")))
                reported_cost += _metric_number(
                    metrics.get("reported_cost_usd"))
                usage = metrics.get("usage", {})
                if isinstance(usage, dict):
                    for key in (
                            "input_tokens", "output_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens"):
                        token_usage[key] += int(_metric_number(
                            usage.get(key)))
            if run.get("status") == "failed" and not failed_stage_seen:
                error = _error_key(run.get("error"))
                if error:
                    error_counts[error] += 1

        if not eligible_runs:
            continue
        quality = _load_json_file(folder / "quality_report.json")
        published = _load_json_file(folder / "publish_report.json")
        if quality and not quality.get("passed") and not published.get("passed"):
            failed_runs = [
                run for run in eligible_runs
                if run.get("status") == "failed"
            ]
            latest_failure = max(
                failed_runs or eligible_runs,
                key=lambda run: _parse_timestamp(
                    run.get("started_at") or run.get("completed_at"))
                or cutoff,
            )
            failed_stages = [
                stage for stage in latest_failure.get("stages", [])
                if stage.get("status") == "failed"
            ]
            last_stage = (
                failed_stages[-1].get("name")
                if failed_stages
                else latest_failure.get("command", "unknown")
            )
            reason = (
                failed_stages[-1].get("error")
                if failed_stages else latest_failure.get("error")
            ) or next(iter(quality.get("errors", [])), "质量门未通过")
            unpublished.append((folder.name, last_stage, _error_key(reason)))

    lines = [
        "# Pipeline Health",
        "",
        f"- 时间窗口：{cutoff.isoformat()} 至 {now.isoformat()}",
        f"- 有效运行：{total_runs}",
        f"- 重试总数：{retry_count}",
        f"- TLS 降级次数：{tls_downgrades}",
        f"- AI 已报告成本：${reported_cost:.4f}",
        (
            "- Token：input "
            f"{token_usage['input_tokens']} / output "
            f"{token_usage['output_tokens']} / cache read "
            f"{token_usage['cache_read_input_tokens']}"
        ),
        "",
        "## 阶段健康度",
        "",
        "| 阶段 | 运行数 | 失败数 | 失败率 | 平均耗时 |",
        "|---|---:|---:|---:|---:|",
    ]
    if stage_stats:
        for name, stats in sorted(stage_stats.items()):
            average = (
                sum(stats["durations"]) / len(stats["durations"])
                if stats["durations"] else 0
            )
            failure_rate = stats["failures"] / stats["runs"] * 100
            lines.append(
                f"| {_md_cell(name)} | {stats['runs']} | "
                f"{stats['failures']} | {failure_rate:.1f}% | "
                f"{average:.1f}s |"
            )
    else:
        lines.append("| 无数据 | 0 | 0 | 0.0% | 0.0s |")

    lines.extend([
        "",
        "## 高频错误",
        "",
        "| 次数 | 错误 |",
        "|---:|---|",
    ])
    if error_counts:
        for error, count in error_counts.most_common(10):
            lines.append(f"| {count} | {_md_cell(error)} |")
    else:
        lines.append("| 0 | 无 |")

    lines.extend([
        "",
        "## 失败来源",
        "",
        "| 次数 | 域名 |",
        "|---:|---|",
    ])
    if source_failures:
        for host, count in source_failures.most_common(10):
            lines.append(f"| {count} | {_md_cell(host)} |")
    else:
        lines.append("| 0 | 无 |")

    lines.extend([
        "",
        "## 未发布且质量门未通过",
        "",
        "| 单集 | 最后阶段 | 原因 |",
        "|---|---|---|",
    ])
    if unpublished:
        for name, stage, reason in sorted(unpublished):
            lines.append(
                f"| {_md_cell(name)} | {_md_cell(stage)} | "
                f"{_md_cell(reason)} |"
            )
    else:
        lines.append("| 无 | - | - |")
    return "\n".join(lines) + "\n"


def health(since="7d", output=None):
    text = build_health_report(since=since)
    if output:
        atomic_write_text(output, text)
        print(f"[健康报告] 已写入 {output}")
    print(text, end="")
    return text


# ── 一键收尾 ──────────────────────────────────────────────────────

def _is_wrangler_command(cmd):
    return any(Path(str(part)).name == "wrangler" for part in cmd)


def _wrangler_environment():
    env = os.environ.copy()
    for key in (
            "CLOUDFLARE_API_TOKEN",
            "CLOUDFLARE_API_KEY",
            "CLOUDFLARE_EMAIL"):
        env.pop(key, None)
    return env


def _run_with_output(cmd, cwd, dry_run=False):
    if dry_run:
        print("  [dry-run] " + " ".join(cmd))
        return True, "dry-run"
    try:
        run_cwd = SITE_DIR if _is_wrangler_command(cmd) else cwd
        run_env = (
            _wrangler_environment()
            if _is_wrangler_command(cmd) else None
        )
        r = subprocess.run(
            cmd,
            cwd=run_cwd,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        out = "\n".join(
            value for value in ((r.stdout or "").strip(), (r.stderr or "").strip())
            if value
        )
        if out:
            print(out[-2000:])
        if r.returncode != 0:
            print(f"[错误] 命令失败 (exit {r.returncode})，已跳过")
            return False, out
        return True, out
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[错误] {type(exc).__name__}: {exc}，已跳过")
        return False, str(exc)


def _run(cmd, cwd, dry_run=False):
    return _run_with_output(cmd, cwd, dry_run=dry_run)[0]


def _run_wrangler(cmd, dry_run=False):
    """Run Wrangler away from the repository .env using OAuth by default."""
    if not _is_wrangler_command(cmd):
        raise ValueError("_run_wrangler 只接受 Wrangler 命令")
    return _run_with_output(cmd, SITE_DIR, dry_run=dry_run)


def _verify_publish_with_retry(*args, attempts=4, delay=3):
    """Retry only transient Pages propagation failures after a deployment."""
    page_error_prefixes = (
        "首页状态异常",
        "首页未找到",
        "单期页面状态异常",
        "单期页面未找到",
        "单期页面缺少",
    )
    report = None
    for attempt in range(1, attempts + 1):
        report = verify_publish(*args)
        if report.get("passed", False):
            return report
        errors = report.get("errors", [])
        page_only = errors and all(
            error.startswith(page_error_prefixes) for error in errors)
        if not page_only or attempt == attempts:
            return report
        print(
            f"[发布验证] Pages 可能仍在传播，"
            f"{delay}s 后重试 ({attempt}/{attempts})...")
        time.sleep(delay)
    return report


class _PublishFailure(RuntimeError):
    def __init__(self, stage, error, report=None):
        self.stage = stage
        self.error = str(error) or type(error).__name__
        self.report = report
        super().__init__(self.error)


def _release_report(folder, audio_key=None):
    release = load_release(folder)
    if not release:
        return {
            "release_id": "",
            "audio_key": audio_key or "",
            "state": "untracked",
            "last_successful_state": "",
            "previous_release_id": "",
        }
    return {
        "release_id": release.get("release_id", ""),
        "audio_key": release.get("audio_key") or audio_key or "",
        "state": release.get("state", ""),
        "last_successful_state": release.get("last_successful_state", ""),
        "previous_release_id": release.get("previous_release_id", ""),
    }


def _write_publish_failure(
        name,
        failed_stage,
        error,
        *,
        report=None,
        run_id="",
        audio_key=None,
):
    folder = CONTENT_DIR / name
    error_text = str(error) or type(error).__name__
    payload = dict(report or {})
    existing_errors = payload.get("errors")
    if isinstance(existing_errors, list):
        errors = list(existing_errors)
    elif existing_errors:
        errors = [str(existing_errors)]
    else:
        errors = []
    if error_text not in errors:
        errors.append(error_text)
    if not audio_key:
        try:
            audio_key = active_audio_key(
                folder, episode_audio_key(folder))
        except Exception:
            audio_key = ""
    payload.update({
        "schema_version": PUBLISH_REPORT_SCHEMA_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "errors": errors,
        "failed_stage": failed_stage,
        "error": error_text,
        "release": _release_report(folder, audio_key),
    })
    if run_id:
        payload["run_id"] = run_id
    write_publish_report(folder / "publish_report.json", payload)
    return payload


def _candidate_catalog_errors(name):
    """Validate the site/catalog candidate without writing generated files."""
    entries = _load_site_entries()
    existing = {
        entry.get("folder"): entry
        for entry in entries
        if entry.get("folder")
    }
    names = _ordered_episode_names()
    errors = []
    if name not in names:
        errors.append(f"{name}: 候选目录中找不到目标单集")
        return errors

    errors.extend(_site_readiness_errors(names, existing))
    candidate_entries = [
        _build_entry(episode_name, existing.get(episode_name, {}))
        for episode_name in names
    ]
    if [entry.get("folder") for entry in candidate_entries] != names:
        errors.append("候选 site.json 顺序或单集集合不一致")

    paths = {}
    for entry in candidate_entries:
        path = entry.get("path")
        if not path:
            errors.append(f"{entry.get('folder')}: 候选页面路径为空")
            continue
        if path in paths:
            errors.append(
                f"候选页面路径冲突: {paths[path]} 与 "
                f"{entry.get('folder')} 均使用 {path}"
            )
        paths[path] = entry.get("folder")

    try:
        _catalog_text(names)
    except Exception as exc:
        errors.append(f"候选播客目录生成失败: {exc}")
    return errors


def _publish_preflight(name):
    """发布前验证内容质量、产物存在性、新鲜度和音频可解码性。"""
    folder = CONTENT_DIR / name
    if not folder.is_dir():
        print(f"[发布前检查][阻断] 找不到播客目录: {folder}")
        return False

    from quality_report import build_quality_report
    report = build_quality_report(folder)
    atomic_write_json(folder / "quality_report.json", report)
    if not report.get("passed", False):
        for error in report.get("errors", []):
            print(f"[发布前检查][阻断] {error}")
        return False

    briefing = _find_briefing(folder)
    mp3 = _gen_mp3(folder)
    html = folder / f"{name} - content.html"
    if not briefing or not mp3 or not html.exists():
        print("[发布前检查][阻断] 缺少讲稿、最终 MP3 或 content.html")
        return False

    release = load_release(folder)
    if release:
        audio_sha256 = hashlib.sha256(mp3.read_bytes()).hexdigest()
        briefing_sha256 = hashlib.sha256(briefing.read_bytes()).hexdigest()
        if release.get("audio_sha256") != audio_sha256:
            print("[发布前检查][阻断] release.json 音频哈希已过期")
            return False
        if release.get("briefing_sha256") != briefing_sha256:
            print("[发布前检查][阻断] release.json 讲稿哈希已过期")
            return False

    briefing_sha256 = hashlib.sha256(briefing.read_bytes()).hexdigest()
    html_text = html.read_text(encoding="utf-8")
    html_hash_match = re.search(
        r'<meta name="podcast-source-sha256" content="([0-9a-f]{64})">',
        html_text,
    )
    if not html_hash_match or html_hash_match.group(1) != briefing_sha256:
        print("[发布前检查][阻断] content.html 未绑定当前讲稿哈希，必须重新生成")
        return False
    if release and release.get("audio_key") not in html.read_text(
            encoding="utf-8"):
        print("[发布前检查][阻断] content.html 未绑定当前 release 音频 key")
        return False

    from tts import validate_tts_manifest
    tts_errors = validate_tts_manifest(folder, briefing.name, name)
    if tts_errors:
        for error in tts_errors:
            print(f"[发布前检查][阻断] {error}")
        return False

    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,duration",
                "-of", "json", str(mp3),
            ],
            capture_output=True, text=True, timeout=30,
        )
        payload = json.loads(probe.stdout or "{}")
        streams = payload.get("streams", [])
        if probe.returncode != 0 or not streams or streams[0].get("codec_name") != "mp3":
            print("[发布前检查][阻断] 最终音频无法通过 ffprobe 解码")
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"[发布前检查][阻断] 音频检查失败: {exc}")
        return False

    print("[发布前检查] 质量、哈希、产物新鲜度和音频格式均通过")
    return True


def _finish_impl(name, dry_run, run_report):
    print(f"=== 一键收尾: {name} ===")
    folder = CONTENT_DIR / name
    with run_report.stage("config_preflight"):
        validate_for_stage("publish", dry_run=dry_run)
    with run_report.stage("publish_preflight") as stage:
        preflight_ok = _publish_preflight(name)
        stage.metrics["passed"] = preflight_ok
        if not preflight_ok:
            raise _PublishFailure(
                "publish_preflight", "publish preflight failed")

    release = load_release(folder)
    if release and not dry_run:
        release = update_release_state(folder, "prepared")
    mp3 = _gen_mp3(folder)
    key = active_audio_key(
        folder,
        episode_audio_key(folder),
    )

    if dry_run:
        with run_report.stage("dry_run_catalog") as stage:
            candidate_errors = _candidate_catalog_errors(name)
            stage.metrics["error_count"] = len(candidate_errors)
            stage.metrics["candidate_episode_count"] = len(
                _ordered_episode_names())
            if candidate_errors:
                for error in candidate_errors:
                    print(f"[dry-run][阻断] {error}")
                raise _PublishFailure(
                    "dry_run_catalog", "; ".join(candidate_errors))
        with run_report.stage("upload_r2", {
                "dry_run": True,
                "size_bytes": mp3.stat().st_size,
                "release_id": release.get("release_id") if release else "",
        }) as stage:
            print(f"[R2] 上传 {mp3.name} 到 R2 ...")
            _run(
                ["npx", "wrangler", "r2", "object", "put",
                 f"{R2_BUCKET}/{key}",
                 "--file", str(mp3), "--content-type", "audio/mpeg",
                 "--remote"],
                cwd=BASE_DIR, dry_run=True,
            )
            stage.metrics["object_key"] = key
        with run_report.stage("deploy_pages", {
                "dry_run": True,
                "project": PAGES_PROJECT,
        }):
            _run_with_output(
                ["npx", "wrangler", "pages", "deploy", ".",
                 "--project-name", PAGES_PROJECT, "--branch", "main"],
                cwd=SITE_DIR, dry_run=True,
            )
        print("[完成] dry-run：未修改 site、台账、首页或 release 状态")
        return True

    with run_report.stage("upload_r2", {
            "dry_run": False,
            "size_bytes": mp3.stat().st_size,
            "release_id": release.get("release_id") if release else "",
    }) as stage:
        print(f"[R2] 上传 {mp3.name} 到 R2 ...")
        uploaded = _run(
            ["npx", "wrangler", "r2", "object", "put",
             f"{R2_BUCKET}/{key}",
             "--file", str(mp3), "--content-type", "audio/mpeg",
             "--remote"],
            cwd=BASE_DIR, dry_run=False,
        )
        stage.metrics["object_key"] = key
        if not uploaded:
            print("[发布] R2 上传失败，停止 Pages 部署")
            raise _PublishFailure("upload_r2", "R2 upload failed")
    if release:
        release = update_release_state(folder, "uploaded")

    with run_report.stage("sync_site") as stage:
        try:
            sync_site(only=name)
        except SystemExit as exc:
            raise _PublishFailure("sync_site", exc) from exc
        stage.metrics["episode_count"] = len(_load_site_entries())

    with run_report.stage("rebuild_catalog") as stage:
        try:
            names = rebuild_catalog()
        except SystemExit as exc:
            raise _PublishFailure("rebuild_catalog", exc) from exc
        stage.metrics["episode_count"] = len(names)

    with run_report.stage("generate_index"):
        try:
            gen_index()
        except SystemExit as exc:
            raise _PublishFailure("generate_index", exc) from exc

    with run_report.stage("catalog_consistency") as stage:
        consistency_errors = catalog_consistency_errors()
        stage.metrics["error_count"] = len(consistency_errors)
        if consistency_errors:
            for error in consistency_errors:
                print(f"[一致性][阻断] {error}")
            raise _PublishFailure(
                "catalog_consistency", "; ".join(consistency_errors))
    if release:
        release = update_release_state(folder, "site_ready")

    with run_report.stage("deploy_pages", {
            "dry_run": False,
            "project": PAGES_PROJECT,
    }) as stage:
        print("[部署] Pages 部署 ...")
        deployed, deploy_output = _run_with_output(
            ["npx", "wrangler", "pages", "deploy", ".",
             "--project-name", PAGES_PROJECT, "--branch", "main"],
            cwd=SITE_DIR, dry_run=False,
        )
        if not deployed:
            print("[发布] Pages 部署失败")
            raise _PublishFailure(
                "deploy_pages", "Pages deployment failed")
    if release:
        release = update_release_state(folder, "deployed")

    if not R2_PUBLIC_URL:
        print("[发布][阻断] R2_PUBLIC_URL 未配置，无法验证线上音频")
        raise _PublishFailure(
            "verify_publish", "R2_PUBLIC_URL 未配置")

    display_title = _display_title(name)
    public_path = quote(
        episode_page_path(folder), safe="")
    episode_url = f"{PAGES_BASE_URL}/{public_path}/content.html"
    audio_url = public_audio_url(folder, R2_PUBLIC_URL)
    with run_report.stage("verify_publish") as stage:
        report = _verify_publish_with_retry(
            PAGES_BASE_URL + "/",
            episode_url,
            audio_url,
            display_title,
            mp3,
        )
        stage.metrics["passed"] = bool(report.get("passed"))
        stage.metrics["error_count"] = len(report.get("errors", []))
        stage.metrics["statuses"] = {
            check: values.get("status")
            for check, values in report.get("checks", {}).items()
        }
        if not report.get("passed", False):
            raise _PublishFailure(
                "verify_publish",
                "; ".join(report.get("errors", []))
                or "remote verification failed",
                report=report,
            )
    deployment_match = re.search(
        r"https://[A-Za-z0-9-]+\." + re.escape(PAGES_PROJECT) + r"\.pages\.dev",
        deploy_output,
    )
    if deployment_match:
        report["deployment_url"] = deployment_match.group(0)
    report["stable_url"] = PAGES_BASE_URL
    if release:
        report["release"] = {
            **_release_report(folder, key),
            "state": "published",
            "last_successful_state": "published",
        }
    report["run_id"] = run_report.run["id"]
    report_path = folder / "publish_report.json"
    write_publish_report(report_path, report)
    if release:
        update_release_state(folder, "published")
    print(f"[发布验证] Pages、R2 和 Range 均通过 → {report_path.name}")
    print("[完成] 台账、site、首页、R2、Pages 已处理")
    return True


def finish(name, dry_run=False):
    """一键收尾并将阶段耗时、失败和远端状态写入 run_report.json。"""
    folder = CONTENT_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    run_report = RunReport(folder, "catalog.finish", {
        "dry_run": dry_run,
        "pages_project": PAGES_PROJECT,
        "r2_bucket": R2_BUCKET,
    })
    try:
        ok = _finish_impl(name, dry_run, run_report)
    except _PublishFailure as exc:
        if not dry_run:
            if load_release(folder):
                update_release_state(folder, "failed", error=exc.error)
            _write_publish_failure(
                name,
                exc.stage,
                exc.error,
                report=exc.report,
                run_id=run_report.run["id"],
            )
        run_report.finish(False, exc.error)
        return False
    except BaseException as exc:
        if not dry_run:
            if load_release(folder):
                update_release_state(folder, "failed", error=exc)
            failed_stages = [
                stage for stage in run_report.run.get("stages", [])
                if stage.get("status") == "failed"
            ]
            failed_stage = (
                failed_stages[-1].get("name")
                if failed_stages else "publish"
            )
            _write_publish_failure(
                name,
                failed_stage,
                exc,
                run_id=run_report.run["id"],
            )
        run_report.finish(False, exc)
        raise
    run_report.finish(ok, None if ok else "publish transaction failed")
    return ok


def _batch_publish_item(name):
    folder = CONTENT_DIR / name
    mp3 = _gen_mp3(folder)
    release = load_release(folder)
    return {
        "name": name,
        "folder": folder,
        "mp3": mp3,
        "release": release,
        "key": active_audio_key(folder, episode_audio_key(folder)),
    }


def finish_batch(names, dry_run=False):
    """Publish multiple prepared episodes with one site deployment."""
    names = list(dict.fromkeys(str(name) for name in names if str(name)))
    if not names:
        raise ValueError("finish-batch 至少需要一个单集名称")

    validate_for_stage("publish", dry_run=dry_run)
    for name in names:
        if not _publish_preflight(name):
            if not dry_run:
                folder = CONTENT_DIR / name
                if load_release(folder):
                    update_release_state(
                        folder, "failed", error="publish preflight failed")
                _write_publish_failure(
                    name, "publish_preflight", "publish preflight failed")
            return False

    candidate_errors = _candidate_catalog_errors(names[0])
    if candidate_errors:
        for error in candidate_errors:
            print(f"[finish-batch][阻断] {error}")
        if not dry_run:
            for name in names:
                folder = CONTENT_DIR / name
                if load_release(folder):
                    update_release_state(
                        folder, "failed", error="; ".join(candidate_errors))
                _write_publish_failure(
                    name,
                    "catalog_candidate",
                    "; ".join(candidate_errors),
                )
        return False

    items = [_batch_publish_item(name) for name in names]
    if any(item["mp3"] is None for item in items):
        missing = [
            item["name"] for item in items if item["mp3"] is None]
        raise RuntimeError(f"批量发布缺少最终 MP3: {missing}")

    if dry_run:
        for item in items:
            print(
                f"[R2] 上传 {item['mp3'].name} 到 "
                f"{R2_BUCKET}/{item['key']} ...")
            _run(
                [
                    "npx", "wrangler", "r2", "object", "put",
                    f"{R2_BUCKET}/{item['key']}",
                    "--file", str(item["mp3"]),
                    "--content-type", "audio/mpeg", "--remote",
                ],
                cwd=BASE_DIR,
                dry_run=True,
            )
        _run_with_output(
            [
                "npx", "wrangler", "pages", "deploy", ".",
                "--project-name", PAGES_PROJECT, "--branch", "main",
            ],
            cwd=SITE_DIR,
            dry_run=True,
        )
        print(
            f"[完成] finish-batch dry-run：{len(items)} 期，"
            "未修改 site、台账、首页或 release 状态")
        return True

    for item in items:
        if item["release"]:
            item["release"] = update_release_state(
                item["folder"], "prepared")

    for item in items:
        print(f"[R2] 上传 {item['mp3'].name} 到 R2 ...")
        uploaded = _run(
            [
                "npx", "wrangler", "r2", "object", "put",
                f"{R2_BUCKET}/{item['key']}",
                "--file", str(item["mp3"]),
                "--content-type", "audio/mpeg", "--remote",
            ],
            cwd=BASE_DIR,
            dry_run=False,
        )
        if not uploaded:
            error = "R2 upload failed"
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "upload_r2", error,
                audio_key=item["key"])
            return False
        if item["release"]:
            item["release"] = update_release_state(
                item["folder"], "uploaded")

    try:
        sync_site()
        rebuild_catalog()
        gen_index()
    except SystemExit as exc:
        error = str(exc) or "site generation failed"
        for item in items:
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "sync_site", error,
                audio_key=item["key"])
        return False

    consistency_errors = catalog_consistency_errors()
    if consistency_errors:
        error = "; ".join(consistency_errors)
        for detail in consistency_errors:
            print(f"[一致性][阻断] {detail}")
        for item in items:
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "catalog_consistency", error,
                audio_key=item["key"])
        return False

    for item in items:
        if item["release"]:
            item["release"] = update_release_state(
                item["folder"], "site_ready")

    print(f"[部署] Pages 批量部署 {len(items)} 期 ...")
    deployed, deploy_output = _run_with_output(
        [
            "npx", "wrangler", "pages", "deploy", ".",
            "--project-name", PAGES_PROJECT, "--branch", "main",
        ],
        cwd=SITE_DIR,
        dry_run=False,
    )
    if not deployed:
        error = "Pages deployment failed"
        for item in items:
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "deploy_pages", error,
                audio_key=item["key"])
        return False

    for item in items:
        if item["release"]:
            item["release"] = update_release_state(
                item["folder"], "deployed")

    if not R2_PUBLIC_URL:
        error = "R2_PUBLIC_URL 未配置"
        for item in items:
            if item["release"]:
                update_release_state(item["folder"], "failed", error=error)
            _write_publish_failure(
                item["name"], "verify_publish", error,
                audio_key=item["key"])
        return False

    deployment_match = re.search(
        r"https://[A-Za-z0-9-]+\." + re.escape(PAGES_PROJECT)
        + r"\.pages\.dev",
        deploy_output,
    )
    all_passed = True
    for item in items:
        display_title = _display_title(item["name"])
        public_path = quote(
            episode_page_path(item["folder"]), safe="")
        episode_url = f"{PAGES_BASE_URL}/{public_path}/content.html"
        audio_url = public_audio_url(item["folder"], R2_PUBLIC_URL)
        report = _verify_publish_with_retry(
            PAGES_BASE_URL + "/",
            episode_url,
            audio_url,
            display_title,
            item["mp3"],
        )
        report["stable_url"] = PAGES_BASE_URL
        report["batch_size"] = len(items)
        if deployment_match:
            report["deployment_url"] = deployment_match.group(0)
        if report.get("passed", False):
            if item["release"]:
                report["release"] = {
                    **_release_report(item["folder"], item["key"]),
                    "state": "published",
                    "last_successful_state": "published",
                }
                update_release_state(item["folder"], "published")
            write_publish_report(
                item["folder"] / "publish_report.json", report)
            print(
                f"[发布验证] {item['name']}: Pages、R2 和 Range 均通过")
            continue

        all_passed = False
        error = (
            "; ".join(report.get("errors", []))
            or "remote verification failed"
        )
        if item["release"]:
            update_release_state(item["folder"], "failed", error=error)
        _write_publish_failure(
            item["name"],
            "verify_publish",
            error,
            report=report,
            audio_key=item["key"],
        )
    if all_passed:
        print(
            f"[完成] {len(items)} 期音频上传、一次 Pages 部署和逐期验收已完成")
    return all_passed


# ── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="播客台账与站点清单维护（CLAUDE.md 第 6/7 步脚本化）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stats = sub.add_parser("stats", help="打印某期字数/时长，不写入")
    p_stats.add_argument("name")

    p_add = sub.add_parser("add", help="追加/更新某期到 播客目录.md")
    p_add.add_argument("name")

    sub.add_parser("rebuild", help="从当前内容和 site 顺序全量重建播客目录")
    sub.add_parser("check", help="校验播客目录、site.json 与当前内容统计一致")

    p_site = sub.add_parser("sync-site", help="同步 content.html + 重建 site.json")
    p_site.add_argument("--only", default=None, help="只同步某期")

    p_index = sub.add_parser("gen-index", help="从 site.json 重建首页 index.html")
    p_index.add_argument("--name", default=None, help="仅重新生成某期卡片（占位，全部重建）")

    p_backfill = sub.add_parser("backfill-sources", help="为缺 来源.md 的期回填来源")

    p_health = sub.add_parser("health", help="汇总近期跨单集运行健康度")
    p_health.add_argument(
        "--since", default="7d", help="时间窗口，如 24h、7d、2w")
    p_health.add_argument(
        "--output", default=None, help="可选 Markdown 输出路径")

    p_finish = sub.add_parser("finish", help="一键收尾：台账+site+首页+R2+部署")
    p_finish.add_argument("name")
    p_finish.add_argument("--dry-run", action="store_true", help="只打印命令不执行（wrangler 部分）")
    p_finish_batch = sub.add_parser(
        "finish-batch",
        help="批量收尾：上传多期音频，只部署一次 Pages",
    )
    p_finish_batch.add_argument("names", nargs="+")
    p_finish_batch.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印命令，不修改 site 或 release 状态",
    )

    args = parser.parse_args()

    if args.cmd == "stats":
        s = episode_stats(args.name)
        print(f"{args.name}: {s['chars']//1000}K字, {s['duration']}min")
    elif args.cmd == "add":
        add_to_catalog(args.name)
    elif args.cmd == "rebuild":
        rebuild_catalog()
    elif args.cmd == "check":
        errors = catalog_consistency_errors()
        for error in errors:
            print(f"[一致性][错误] {error}")
        if not errors:
            print("[一致性] 播客目录、site.json 与当前内容统计一致")
        return 1 if errors else 0
    elif args.cmd == "sync-site":
        sync_site(args.only)
    elif args.cmd == "gen-index":
        gen_index()
    elif args.cmd == "backfill-sources":
        backfill_sources()
    elif args.cmd == "health":
        try:
            health(args.since, args.output)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.cmd == "finish":
        return 0 if finish(args.name, dry_run=args.dry_run) else 1
    elif args.cmd == "finish-batch":
        return 0 if finish_batch(
            args.names, dry_run=args.dry_run) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
