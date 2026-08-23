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
        f'<span>期完整节目</span></div>\n'
        f'        <div class="stat"><strong>{total_min}</strong>'
        f'<span>分钟中文音频</span></div>\n'
        f'        <div class="stat"><strong>{total_words // 1000}K</strong>'
        f'<span>字可检索讲稿</span></div>'
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
        featured = ' data-featured="true"' if display_index == 1 else ""
        source = (
            f'<a href="{source_url}" target="_blank" rel="noopener" '
            f'class="episode-source">来源 · {source_name}</a>'
            if source_url else
            f'<span class="episode-source">来源 · {source_name or "未标注"}</span>'
        )
        cards.append(
            f'    <article class="episode-card"{featured} '
            f'data-search="{title.lower()} {source_name.lower()}">\n'
            f'        <div class="episode-index" aria-hidden="true">'
            f'{episode_no:02d}</div>\n'
            f'        <a class="episode-card-main" href="{href}" '
            f'aria-label="阅读和收听：{title}">\n'
            f'            <p class="episode-format">中文讲稿 · 中文音频</p>\n'
            f'            <h3 class="episode-title">{title}</h3>\n'
            f'            <div class="episode-meta">\n'
            f'                <span>{entry["duration"]} 分钟</span>\n'
            f'                <span>{entry["words"] // 1000}K 字</span>\n'
            f'            </div>\n'
            f'        </a>\n'
            f'        <div class="episode-card-bottom">\n'
            f'            {source}\n'
            f'            <a class="episode-open" href="{href}" '
            f'aria-label="打开 {title}"><span aria-hidden="true">↗</span></a>\n'
            f'        </div>\n'
            f'    </article>'
        )
    return "\n".join(cards)


INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="精选英文播客的完整中文讲稿、证据审查与中文音频。">
<meta name="theme-color" content="#eef1ef">
<meta property="og:title" content="声稿 — 英文播客中文档案">
<meta property="og:description" content="完整观点、数字和推理，整理成可读可听的中文档案。">
<meta property="og:image" content="assets/podcast-studio.webp">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='8' fill='%2318201e'/%3E%3Cpath d='M18 17h28v6H25v7h18v6H25v8h21v6H18z' fill='%2395b8aa'/%3E%3C/svg%3E">
<title>声稿 — 英文播客中文档案</title>
<style>
:root {
    --ink: #18201e;
    --ink-soft: #56625e;
    --paper: #eef1ef;
    --surface: #f9faf9;
    --surface-muted: #e2e8e5;
    --accent: #3f6f61;
    --accent-strong: #285346;
    --line: #cbd4d0;
    --serif: "Iowan Old Style", "Noto Serif SC", "Source Han Serif SC", "Songti SC", Georgia, serif;
    --sans: "Avenir Next", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
    --mono: "SFMono-Regular", "JetBrains Mono", Consolas, monospace;
    --container: 82rem;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
    margin: 0;
    color: var(--ink);
    background: var(--paper);
    font-family: var(--sans);
    line-height: 1.65;
    letter-spacing: 0;
    -webkit-font-smoothing: antialiased;
}
body::after {
    content: "";
    position: fixed;
    inset: 0;
    z-index: 30;
    pointer-events: none;
    opacity: 0.025;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='110' height='110'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.72' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}
::selection { color: white; background: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
.skip-link {
    position: fixed;
    top: 0.75rem;
    left: 0.75rem;
    z-index: 50;
    transform: translateY(-170%);
    padding: 0.7rem 1rem;
    color: white;
    background: var(--ink);
    text-decoration: none;
    transition: transform 180ms ease;
}
.skip-link:focus { transform: translateY(0); }
.site-nav,
.hero,
.library,
.site-footer {
    width: min(calc(100% - 2.5rem), var(--container));
    margin-inline: auto;
}
.site-nav {
    min-height: 4.75rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--line);
}
.brand {
    display: inline-flex;
    align-items: center;
    gap: 0.7rem;
    color: inherit;
    text-decoration: none;
    font-weight: 650;
}
.brand-mark {
    width: 2rem;
    height: 2rem;
    display: grid;
    place-items: center;
    color: var(--surface);
    background: var(--ink);
    border-radius: 4px;
    font-family: var(--serif);
    font-size: 1.15rem;
}
.nav-note {
    margin: 0;
    color: var(--ink-soft);
    font: 500 0.7rem/1.4 var(--mono);
    font-variant-numeric: tabular-nums;
}
.hero {
    display: grid;
    grid-template-columns: minmax(0, 1.25fr) minmax(18rem, 0.55fr) minmax(15rem, 0.48fr);
    gap: clamp(2rem, 5vw, 5rem);
    align-items: end;
    min-height: 34rem;
    padding: 4.5rem 0 5.25rem;
}
.hero-copy { align-self: center; max-width: 50rem; }
.eyebrow,
.library-kicker,
.episode-format {
    margin: 0;
    color: var(--accent-strong);
    font: 600 0.69rem/1.5 var(--mono);
    text-transform: uppercase;
    letter-spacing: 0.08em;
}
.hero h1 {
    max-width: 13ch;
    margin: 1.2rem 0 0;
    font-family: var(--serif);
    font-size: 4.7rem;
    font-weight: 520;
    line-height: 0.98;
    letter-spacing: 0;
    text-wrap: balance;
}
.hero-intro {
    max-width: 37rem;
    margin: 1.8rem 0 0;
    color: var(--ink-soft);
    font-size: 1.05rem;
    line-height: 1.85;
    text-wrap: pretty;
}
.hero-media {
    position: relative;
    align-self: stretch;
    min-height: 27rem;
    margin: 0;
    overflow: hidden;
    border-radius: 6px;
    background: var(--ink);
}
.hero-media img {
    width: 100%;
    height: 100%;
    display: block;
    object-fit: cover;
    filter: grayscale(0.72) saturate(0.62) contrast(1.04);
    opacity: 0.78;
}
.hero-media::after {
    content: "声音 / 文字 / 证据";
    position: absolute;
    inset: auto 1rem 1rem;
    padding-top: 0.8rem;
    color: white;
    border-top: 1px solid rgba(255,255,255,0.48);
    font: 500 0.65rem/1.5 var(--mono);
    letter-spacing: 0.08em;
}
.stats {
    align-self: stretch;
    display: grid;
    align-content: end;
    border-top: 1px solid var(--ink);
}
.stat {
    display: grid;
    grid-template-columns: 5.4rem 1fr;
    gap: 0.8rem;
    align-items: baseline;
    padding: 1.1rem 0;
    border-bottom: 1px solid var(--line);
}
.stat strong {
    font: 500 2rem/1 var(--serif);
    font-variant-numeric: tabular-nums;
}
.stat span { color: var(--ink-soft); font-size: 0.77rem; }
.library { padding: 1.8rem 0 7rem; border-top: 1px solid var(--ink); }
.library-head {
    display: grid;
    grid-template-columns: 1fr minmax(17rem, 26rem);
    gap: 2rem;
    align-items: end;
    margin-bottom: 2.5rem;
}
.library-kicker { margin-bottom: 0.6rem; }
.library h2 {
    margin: 0;
    font: 520 3.1rem/1 var(--serif);
    letter-spacing: 0;
}
.library-summary {
    margin: 0.8rem 0 0;
    color: var(--ink-soft);
    font-size: 0.8rem;
    font-variant-numeric: tabular-nums;
}
.search-wrap {
    position: relative;
    display: block;
    border-bottom: 1px solid var(--ink);
}
.search-wrap svg {
    position: absolute;
    left: 0;
    top: 50%;
    width: 1rem;
    transform: translateY(-50%);
    color: var(--ink-soft);
}
.search-input {
    width: 100%;
    padding: 0.85rem 2rem 0.85rem 1.65rem;
    border: 0;
    color: var(--ink);
    background: transparent;
    font: 500 0.9rem/1.2 var(--sans);
}
.search-input:focus-visible { outline: none; }
.search-wrap:focus-within {
    border-color: var(--accent-strong);
    box-shadow: 0 1px 0 var(--accent-strong);
}
.search-input::placeholder { color: #7c8883; }
.search-clear {
    position: absolute;
    right: 0;
    top: 50%;
    width: 1.8rem;
    height: 1.8rem;
    transform: translateY(-50%);
    border: 0;
    color: var(--ink-soft);
    background: transparent;
    cursor: pointer;
    opacity: 0;
    pointer-events: none;
    transition: color 180ms ease, opacity 180ms ease, transform 180ms ease;
}
.search-clear:hover { color: var(--accent-strong); }
.search-clear:active { transform: translateY(-50%) scale(0.94); }
.search-wrap.has-value .search-clear { opacity: 1; pointer-events: auto; }
.episode-list { border-top: 1px solid var(--line); }
.episode-card {
    display: grid;
    grid-template-columns: 4.25rem minmax(0, 1fr) minmax(10rem, 14rem);
    gap: 1.6rem;
    align-items: stretch;
    min-height: 12rem;
    border-bottom: 1px solid var(--line);
    transition: background 220ms ease, transform 220ms ease;
}
.episode-card:hover { background: var(--surface); }
.episode-card:active { transform: translateY(1px); }
.episode-card[data-featured="true"] {
    min-height: 18rem;
    background: color-mix(in srgb, var(--accent) 10%, var(--surface));
}
.episode-index {
    padding-top: 1.6rem;
    color: var(--accent-strong);
    font: 600 0.78rem/1 var(--mono);
    font-variant-numeric: tabular-nums;
}
.episode-card-main {
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 1.55rem 0 1.8rem;
    color: inherit;
    text-decoration: none;
}
.episode-format { margin-bottom: 1rem; opacity: 0.82; }
.episode-title {
    max-width: 31ch;
    margin: 0;
    font: 520 2rem/1.15 var(--serif);
    letter-spacing: 0;
    text-wrap: balance;
    overflow-wrap: anywhere;
}
.episode-card[data-featured="true"] .episode-title {
    max-width: 25ch;
    font-size: 3rem;
}
.episode-meta {
    display: flex;
    gap: 1.25rem;
    margin-top: 1.3rem;
    color: var(--ink-soft);
    font: 500 0.71rem/1.4 var(--mono);
    font-variant-numeric: tabular-nums;
}
.episode-card-bottom {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1rem;
    padding: 1.55rem 0 1.8rem;
}
.episode-source {
    color: var(--ink-soft);
    font-size: 0.72rem;
    text-decoration: none;
}
a.episode-source:hover { color: var(--accent-strong); text-decoration: underline; text-underline-offset: 0.25rem; }
.episode-open {
    width: 2.6rem;
    height: 2.6rem;
    display: grid;
    place-items: center;
    flex: 0 0 auto;
    color: white;
    background: var(--ink);
    border-radius: 4px;
    text-decoration: none;
    transition: background 180ms ease, transform 180ms ease;
}
.episode-open:hover { background: var(--accent-strong); transform: translate(2px, -2px); }
.episode-open:active { transform: scale(0.96); }
.episode-card[hidden] { display: none; }
.empty-state {
    display: none;
    padding: 5rem 0;
    color: var(--ink-soft);
    border-bottom: 1px solid var(--line);
}
.empty-state.show { display: block; }
.empty-state strong { display: block; color: var(--ink); font: 520 1.5rem/1.3 var(--serif); }
.empty-state span { display: block; margin-top: 0.5rem; }
.site-footer {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 2rem;
    align-items: end;
    padding: 2.5rem 0 3.25rem;
    border-top: 1px solid var(--ink);
}
.footer-title { margin: 0 0 0.35rem; font: 520 1.45rem/1.2 var(--serif); }
.site-footer p { max-width: 45rem; margin: 0; color: var(--ink-soft); font-size: 0.76rem; }
.footer-mark { color: var(--accent); font: 500 2.8rem/1 var(--serif); }
@media (max-width: 980px) {
    .hero { grid-template-columns: minmax(0, 1fr) 16rem; min-height: 30rem; }
    .hero h1 { font-size: 3.7rem; }
    .hero-media { min-height: 22rem; }
    .stats { grid-column: 1 / -1; grid-template-columns: repeat(3, 1fr); }
    .stat { grid-template-columns: 1fr; }
    .episode-card { grid-template-columns: 3rem minmax(0, 1fr) 11rem; }
}
@media (max-width: 700px) {
    .site-nav, .hero, .library, .site-footer { width: calc(100% - 1.5rem); }
    .nav-note { display: none; }
    .hero {
        grid-template-columns: 1fr;
        gap: 1.3rem;
        min-height: 0;
        padding: 2.6rem 0 2.8rem;
    }
    .hero h1 { max-width: 12ch; font-size: 2.75rem; }
    .hero-intro {
        margin-top: 1rem;
        font-size: 0.92rem;
        line-height: 1.65;
    }
    .hero-media {
        min-height: 9rem;
        max-height: 9rem;
    }
    .hero-media img { object-position: center 43%; }
    .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .stat {
        grid-template-columns: 1fr;
        gap: 0.3rem;
        padding: 0.8rem 0.35rem 0.8rem 0;
    }
    .stat strong { font-size: 1.55rem; }
    .stat span { font-size: 0.65rem; }
    .library-head { grid-template-columns: 1fr; }
    .library h2 { font-size: 2.45rem; }
    .episode-card,
    .episode-card[data-featured="true"] {
        grid-template-columns: 2.4rem minmax(0, 1fr);
        min-height: 0;
        gap: 0.75rem;
    }
    .episode-card-bottom { grid-column: 2; padding-top: 0; }
    .episode-title,
    .episode-card[data-featured="true"] .episode-title { font-size: 1.72rem; }
    .episode-card-main { padding-bottom: 0.8rem; }
    .site-footer { grid-template-columns: 1fr auto; }
}
@media (max-width: 390px) {
    .hero h1 { font-size: 2.45rem; }
    .episode-card { grid-template-columns: 2rem minmax(0, 1fr); }
    .episode-title,
    .episode-card[data-featured="true"] .episode-title { font-size: 1.5rem; }
    .episode-card-bottom { align-items: center; }
}
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
}
</style>
</head>
<body>
<a class="skip-link" href="#library">跳到节目列表</a>
<header class="site-nav">
    <a class="brand" href="./" aria-label="声稿首页">
        <span class="brand-mark">声</span>
        <span>声稿</span>
    </a>
    <p class="nav-note">English podcasts / Chinese archive</p>
</header>
<main>
    <section class="hero" aria-labelledby="page-title">
        <div class="hero-copy">
            <p class="eyebrow">完整观点 · 中文讲稿 · 证据审查</p>
            <h1 id="page-title">把一场长谈，整理成可回看的声音档案。</h1>
            <p class="hero-intro">保留人物立场、数字、推理和分歧。每一期都可以阅读、收听，并回到原始转录核对。</p>
        </div>
        <figure class="hero-media">
            <img src="assets/podcast-studio.webp" width="1200" height="1800" alt="录音室里的麦克风、耳机与防喷罩">
        </figure>
        <div class="stats" aria-label="站点统计">
<!-- STATS:START -->
<!-- STATS:END -->
        </div>
    </section>
    <section class="library" id="library" aria-labelledby="library-title">
        <div class="library-head">
            <div>
                <p class="library-kicker">Archive / 持续更新</p>
                <h2 id="library-title">节目档案</h2>
                <p class="library-summary"><span id="visibleCount">0</span> 期节目正在显示</p>
            </div>
            <label class="search-wrap" for="episodeSearch">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.2 4.2"/></svg>
                <input class="search-input" id="episodeSearch" type="search" autocomplete="off" placeholder="搜索节目或来源" aria-label="搜索节目">
                <button class="search-clear" id="searchClear" type="button" aria-label="清空搜索">×</button>
            </label>
        </div>
        <div class="episode-list" id="episodeGrid">
<!-- CARDS:START -->
<!-- CARDS:END -->
            <div class="empty-state" id="emptyState" role="status">
                <strong>没有匹配的节目</strong>
                <span>换一个标题、人物或来源关键词。</span>
            </div>
        </div>
    </section>
</main>
<footer class="site-footer">
    <div>
        <p class="footer-title">声稿</p>
        <p>中文内容用于学习和信息整理。观点属于原节目参与者，事实与动态数据以原始来源为准。</p>
    </div>
    <span class="footer-mark" aria-hidden="true">声</span>
</footer>
<script>
(function() {
    'use strict';
    var input = document.getElementById('episodeSearch');
    var clear = document.getElementById('searchClear');
    var wrap = document.querySelector('.search-wrap');
    var cards = Array.prototype.slice.call(document.querySelectorAll('.episode-card'));
    var empty = document.getElementById('emptyState');
    var count = document.getElementById('visibleCount');
    function applyFilter() {
        var query = input.value.trim().toLocaleLowerCase();
        var visible = 0;
        cards.forEach(function(card) {
            var show = !query || (card.dataset.search || '').indexOf(query) !== -1;
            card.hidden = !show;
            if (show) visible += 1;
        });
        wrap.classList.toggle('has-value', Boolean(query));
        empty.classList.toggle('show', visible === 0);
        count.textContent = String(visible);
    }
    input.addEventListener('input', applyFilter);
    clear.addEventListener('click', function() {
        input.value = '';
        input.focus();
        applyFilter();
    });
    applyFilter();
})();
</script>
</body>
</html>
"""


def render_index(site_dir):
    site_dir = Path(site_dir)
    site_json_path = site_dir / "site.json"
    index_path = site_dir / "index.html"
    if not site_json_path.exists():
        raise FileNotFoundError("找不到 site/site.json，先跑 sync-site")
    entries = json.loads(site_json_path.read_text(encoding="utf-8"))
    if not entries:
        raise ValueError("site.json 为空")
    text = replace_region(
        INDEX_TEMPLATE, STATS_START, STATS_END, _stats_html(entries))
    text = replace_region(
        text, CARDS_START, CARDS_END, _cards_html(entries))
    atomic_write_text(index_path, text)
    return {"episode_count": len(entries), "index_path": index_path}
