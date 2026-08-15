"""Deterministic rendering of the public podcast index page."""

import html
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from atomic_io import atomic_write_text
except ImportError:
    from scripts.atomic_io import atomic_write_text


STATS_START = "<!-- STATS:START -->"
STATS_END = "<!-- STATS:END -->"
CARDS_START = "<!-- CARDS:START -->"
CARDS_END = "<!-- CARDS:END -->"


def escape_attribute(value):
    return html.escape(str(value or ""), quote=True)


def safe_external_url(value):
    raw = str(value or "").strip()
    if not raw or any(ord(char) < 32 for char in raw):
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return escape_attribute(raw)


def replace_region(text, start_marker, end_marker, inner):
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start == -1 or end == -1:
        raise ValueError(
            f"index.html 缺标记: {start_marker} / {end_marker}")
    end_close = text.find("-->", end)
    if end_close == -1:
        raise ValueError(f"index.html 结束标记不完整: {end_marker}")
    return (
        text[:start] + start_marker + "\n" + inner + "\n"
        + end_marker + text[end_close + 3:]
    )


def _stats_html(entries):
    total_min = sum(entry["duration"] for entry in entries)
    total_words = sum(entry["words"] for entry in entries)
    return (
        f'        <div class="stat"><strong>{len(entries):02d}</strong>'
        f'<span>已收录节目</span></div>\n'
        f'        <div class="stat"><strong>{total_min}</strong>'
        f'<span>分钟中文音频</span></div>\n'
        f'        <div class="stat"><strong>{total_words // 1000}K</strong>'
        f'<span>字深度讲稿</span></div>'
    )


def _cards_html(entries):
    cards = []
    for display_index, entry in enumerate(reversed(entries), start=1):
        href = quote(
            (entry.get("path") or entry["folder"]) + "/content.html",
            safe="/",
        )
        episode_no = len(entries) - display_index + 1
        title = escape_attribute(entry["title"])
        source_name = escape_attribute(entry.get("source_name", ""))
        source_url = safe_external_url(entry.get("source_url", ""))
        cards.append(
            f'    <article class="episode-card" '
            f'data-search="{title.lower()} {source_name.lower()}">\n'
            f'        <a class="episode-card-main" href="{href}" '
            f'aria-label="阅读和收听：{title}">\n'
            f'            <div class="episode-card-top">\n'
            f'                <span class="episode-index">{episode_no:02d}</span>\n'
            f'                <span class="episode-format">中文讲稿 · 音频</span>\n'
            f'            </div>\n'
            f'            <h3 class="episode-title">{title}</h3>\n'
            f'            <div class="episode-meta">\n'
            f'                <span>{entry["duration"]} 分钟</span>\n'
            f'                <span>{entry["words"] // 1000}K 字</span>\n'
            f'            </div>\n'
            f'        </a>\n'
            f'        <div class="episode-card-bottom">\n'
            f'            <a class="episode-open" href="{href}">阅读与收听 '
            f'<span aria-hidden="true">↗</span></a>\n'
            f'            <a href="{source_url}" target="_blank" rel="noopener" '
            f'class="episode-source">{source_name}</a>\n'
            f'        </div>\n'
            f'    </article>'
        )
    return "\n".join(cards)


def render_index(site_dir):
    site_dir = Path(site_dir)
    site_json_path = site_dir / "site.json"
    index_path = site_dir / "index.html"
    if not site_json_path.exists():
        raise FileNotFoundError("找不到 site/site.json，先跑 sync-site")
    if not index_path.exists():
        raise FileNotFoundError(f"找不到 {index_path}")
    entries = json.loads(site_json_path.read_text(encoding="utf-8"))
    if not entries:
        raise ValueError("site.json 为空")

    text = index_path.read_text(encoding="utf-8")
    text = replace_region(
        text, STATS_START, STATS_END, _stats_html(entries))
    text = replace_region(
        text, CARDS_START, CARDS_END, _cards_html(entries))
    atomic_write_text(index_path, text)
    return {"episode_count": len(entries), "index_path": index_path}
