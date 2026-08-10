#!/usr/bin/env python3
"""
HTML 生成模块 — 将讲书稿.md 转为精美、自包含的 HTML 阅读页面。

用法（库）:
    from html_gen import md_to_html
    html_path = md_to_html("content/播客名/讲书稿.md")

用法（命令行）:
    python scripts/html_gen.py "content/播客名/讲书稿.md"
    python scripts/html_gen.py "content/播客名/讲书稿.md" -o "output.html"
"""
import sys
from pathlib import Path

try:
    from atomic_io import atomic_write_text
except ImportError:
    from scripts.atomic_io import atomic_write_text

_scripts = str(Path(__file__).resolve().parent)
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import argparse
import hashlib
import os
import re
from datetime import date, datetime

from config import BASE_DIR, R2_PUBLIC_URL
from episode import public_audio_url


# ── Markdown 解析 ─────────────────────────────────────────────────

def parse_sections(md_text):
    """
    将讲稿正文按 ## 章节标题拆分为结构化列表。
    返回 [(section_index, title_or_None, body_text), ...]
    """
    md_text = md_text.strip()
    parts = re.split(r"\n(?=## )", md_text)

    sections = []
    preamble_pending = True
    idx = 0

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith("## "):
            preamble_pending = False
            lines = part.split("\n", 1)
            title = lines[0].replace("## ", "").strip()
            body = lines[1].strip() if len(lines) > 1 else ""
            sections.append((idx, title, body))
            idx += 1
        elif preamble_pending:
            sections.append((-1, None, part))

    return sections


def _escape_html(text):
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def _text_to_html(text):
    """将纯文本段落转换为 HTML <p> 标签。"""
    paras = re.split(r"\n\s*\n", text.strip())
    parts = []
    for p in paras:
        p = p.strip()
        if not p:
            continue
        p = re.sub(r"\s+", " ", p)
        parts.append(f"<p>{_escape_html(p)}</p>")
    return "\n".join(parts)


# ── 构建 HTML ────────────────────────────────────────────────────

def _build_html(
        title, sections, word_count=None, date_str=None, mp3_url=None,
        source_sha256=""):
    """从解析好的 sections 构建完整 HTML 页面。mp3_url 为音频播放器引用（相对/绝对均可）。"""
    # ── TOC ──
    toc_lines = []
    for idx, sec_title, _ in sections:
        if sec_title is None:
            toc_lines.append(
                '<li><a href="#sec-intro" class="toc-link">'
                '<span class="toc-num">·</span>'
                '<span class="toc-label">开场</span></a></li>'
            )
        else:
            anchor = f"sec-{idx}"
            safe = _escape_html(sec_title)
            num = f"{idx + 1}".zfill(2)
            toc_lines.append(
                f'<li><a href="#{anchor}" class="toc-link">'
                f'<span class="toc-num">{num}</span>'
                f'<span class="toc-label">{safe}</span></a></li>'
            )
    toc_html = "\n".join(toc_lines)

    # ── 正文 ──
    content_parts = []
    total = len([s for s in sections if s[1] is not None])

    for idx, sec_title, body in sections:
        if sec_title is None:
            body_html = _text_to_html(body)
            content_parts.append(
                f'<section id="sec-intro" class="chapter chapter-intro">\n'
                f'{body_html}\n'
                f'</section>'
            )
        else:
            anchor = f"sec-{idx}"
            safe_title = _escape_html(sec_title)
            body_html = _text_to_html(body)
            num = f"{idx + 1}".zfill(2)
            content_parts.append(
                f'<section id="{anchor}" class="chapter">\n'
                f'<div class="chapter-decoration">'
                f'<span class="chapter-num">{num}</span>'
                f'<span class="chapter-total">/{total}</span>'
                f'</div>\n'
                f'<div class="chapter-divider"></div>\n'
                f'<h2 class="chapter-title">{safe_title}</h2>\n'
                f'{body_html}\n'
                f'</section>'
            )
    content_html = "\n\n".join(content_parts)

    safe_title = _escape_html(title)
    hero_title = title
    if len(hero_title) > 90 and " — " in hero_title:
        hero_title = hero_title.split(" — ", 1)[0].strip()
    safe_hero_title = _escape_html(hero_title)
    hero_title_class = (
        "title-xlong" if len(hero_title) > 90
        else "title-long" if len(hero_title) > 55
        else ""
    )
    wc_str = f"{word_count:,} 字" if word_count else ""
    date_str = date_str or date.today().isoformat()

    # 模板用 .format() 时 CSS/JS 需要 {{ }} 转义，改成 .replace() 后统一转成单括号
    html = HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    for key, val in {
        "title": safe_title,
        "podcast_title": safe_title,
        "hero_title": safe_hero_title,
        "hero_title_class": hero_title_class,
        "date": date_str,
        "word_count": wc_str,
        "toc_items": toc_html,
        "content": content_html,
        "chapter_count": str(total),
        "mp3_url": mp3_url,
        "source_sha256": source_sha256,
    }.items():
        html = html.replace("{" + key + "}", val)
    return html


# ═══════════════════════════════════════════════════════════════
#  HTML 模板 — 精心设计的阅读体验
#  视觉概念：暖调书卷 × 现代极简
#  配色：奶油纸底 × 深酒红点缀 × 暖金装饰
# ═══════════════════════════════════════════════════════════════

HTML_TEMPLATE = """\
\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="{podcast_title} 的中文讲稿，配音频与折叠目录">
<meta name="theme-color" content="#171512">
<meta name="podcast-source-sha256" content="{source_sha256}">
<meta property="og:title" content="{podcast_title} — 中文深度讲稿">
<meta property="og:description" content="完整中文讲稿与中文音频，保留节目观点、数字、推理和分歧。">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%236d2c2c'/%3E%3Ctext x='32' y='43' font-family='Georgia,serif' font-size='32' fill='%23bf9b4a' text-anchor='middle'%3E%E4%B9%A6%3C/text%3E%3C/svg%3E">
<title>{podcast_title} — 讲稿</title>
<style>
:root {
    --ink: #171512;
    --ink-raised: #211e1a;
    --ink-soft: #5b554b;
    --paper: #f1eee6;
    --paper-light: #faf8f2;
    --paper-deep: #e5dfd3;
    --accent: #c45639;
    --accent-light: #e08468;
    --line: rgba(23, 21, 18, 0.15);
    --rail: 300px;
    --serif: "Iowan Old Style", "Noto Serif SC", "Songti SC", Georgia, serif;
    --sans: "Avenir Next", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
    --mono: "SFMono-Regular", "Roboto Mono", Consolas, monospace;
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; font-size: 16px; }
body {
    margin: 0;
    color: var(--ink);
    background:
        radial-gradient(circle at 82% 2%, rgba(196, 86, 57, 0.08), transparent 28rem),
        var(--paper);
    font-family: var(--sans);
    line-height: 1.85;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}
body::after {
    content: "";
    position: fixed;
    inset: 0;
    z-index: 500;
    pointer-events: none;
    opacity: 0.028;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='96' height='96'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}
::selection { color: var(--paper-light); background: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.skip-link {
    position: fixed;
    top: 0.75rem;
    left: 0.75rem;
    z-index: 600;
    transform: translateY(-170%);
    padding: 0.65rem 0.9rem;
    color: var(--paper-light);
    background: var(--ink);
    text-decoration: none;
}
.skip-link:focus { transform: translateY(0); }

.progress-bar {
    position: fixed;
    inset: 0 auto auto 0;
    z-index: 450;
    width: 0;
    height: 3px;
    background: var(--accent);
    transition: width 100ms linear;
}

.toc {
    position: fixed;
    inset: 0 auto 0 0;
    z-index: 100;
    width: var(--rail);
    overflow-y: auto;
    padding: 1.4rem 1.25rem 2rem;
    color: rgba(250, 248, 242, 0.72);
    background:
        radial-gradient(circle at 0 0, rgba(196, 86, 57, 0.2), transparent 18rem),
        var(--ink);
    border-right: 1px solid rgba(255, 255, 255, 0.07);
    transition: transform 300ms cubic-bezier(.2,.75,.25,1);
}
.toc-inner { width: 100%; max-width: 15.5rem; margin-inline: auto; }
.toc-brand { margin-bottom: 3.4rem; }
.back-link {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    padding: 0.65rem 0;
    color: var(--paper-light);
    font-size: 0.8rem;
    font-weight: 650;
    text-decoration: none;
    border-bottom: 1px solid rgba(255,255,255,0.14);
    transition: color 180ms ease, border-color 180ms ease;
}
.back-link:hover { color: var(--accent-light); border-color: var(--accent-light); }
.back-link svg { width: 0.95rem; transition: transform 180ms ease; }
.back-link:hover svg { transform: translateX(-0.2rem); }
.toc-title {
    margin: 0 0 0.35rem;
    color: var(--paper-light);
    font: 520 2.1rem/1 var(--serif);
    letter-spacing: -0.045em;
}
.toc-sub {
    margin: 0;
    color: rgba(250, 248, 242, 0.43);
    font: 600 0.63rem/1.5 var(--mono);
    letter-spacing: 0.1em;
    text-transform: uppercase;
}
.toc-divider { width: 2.2rem; height: 2px; margin: 1.2rem 0 1.25rem; background: var(--accent); }
.toc ul { margin: 0; padding: 0; list-style: none; }
.toc li { margin: 0; }
.toc-link {
    display: grid;
    grid-template-columns: 1.7rem 1fr;
    gap: 0.7rem;
    align-items: start;
    padding: 0.65rem 0;
    color: rgba(250, 248, 242, 0.52);
    font-size: 0.77rem;
    line-height: 1.48;
    text-decoration: none;
    border-bottom: 1px solid rgba(255,255,255,0.075);
    transition: color 180ms ease, transform 180ms ease;
}
.toc-link:hover { color: var(--paper-light); transform: translateX(0.2rem); }
.toc-link.active { color: var(--paper-light); }
.toc-num {
    color: var(--accent-light);
    font: 650 0.63rem/1.75 var(--mono);
    font-variant-numeric: tabular-nums;
}
.toc-label { text-wrap: pretty; }
.toc::-webkit-scrollbar { width: 3px; }
.toc::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); }

.toc-toggle {
    position: fixed;
    top: 1rem;
    left: 1rem;
    z-index: 130;
    width: 2.8rem;
    height: 2.8rem;
    display: grid;
    place-items: center;
    padding: 0;
    color: var(--ink);
    background: rgba(250, 248, 242, 0.88);
    backdrop-filter: blur(14px);
    border: 1px solid var(--line);
    border-radius: 0;
    cursor: pointer;
    box-shadow: 0 0.7rem 2rem rgba(41, 33, 25, 0.1);
    transition: left 300ms cubic-bezier(.2,.75,.25,1), transform 180ms ease, color 180ms ease;
}
body:not(.toc-hidden) .toc-toggle { left: calc(var(--rail) + 1rem); }
.toc-toggle:hover { color: var(--accent); transform: translateY(-2px); }
.toc-toggle:active { transform: translateY(0) scale(0.97); }
.toc-overlay { display: none; position: fixed; inset: 0; z-index: 90; background: rgba(23,21,18,0.48); backdrop-filter: blur(3px); }

.layout { min-height: 100dvh; }
main {
    width: min(calc(100% - 3rem), 54rem);
    margin-inline: auto;
    padding: 0 0 6rem;
    transition: margin 300ms cubic-bezier(.2,.75,.25,1);
}
body:not(.toc-hidden) main {
    margin-left: calc(var(--rail) + max(2.5rem, (100vw - var(--rail) - 54rem) / 2));
    margin-right: auto;
}

.hero { padding: clamp(5.5rem, 11vw, 9rem) 0 2.5rem; }
.hero-kicker {
    margin: 0 0 1.35rem;
    color: var(--accent);
    font: 700 0.68rem/1 var(--mono);
    letter-spacing: 0.14em;
    text-transform: uppercase;
}
.hero h1 {
    max-width: 18ch;
    margin: 0;
    color: var(--ink);
    font: 520 clamp(2.8rem, 6vw, 5.8rem)/0.98 var(--serif);
    letter-spacing: -0.055em;
    text-wrap: balance;
}
.hero-sub {
    display: flex;
    flex-wrap: wrap;
    gap: 0.55rem;
    margin: 2rem 0 0;
    color: var(--ink-soft);
    font: 600 0.7rem/1.5 var(--mono);
    letter-spacing: 0.04em;
    font-variant-numeric: tabular-nums;
}
.hero-sub .sep { color: var(--accent); }
.hero-divider { width: 100%; height: 1px; margin-top: 2rem; background: var(--line); }
.hero-accent { display: none; }

.player {
    position: sticky;
    top: 1rem;
    z-index: 40;
    margin: 0 0 5rem;
    padding: 1rem 1.1rem;
    color: var(--paper-light);
    background: rgba(23, 21, 18, 0.95);
    backdrop-filter: blur(18px);
    border: 1px solid rgba(255,255,255,0.08);
    box-shadow: 0 1.1rem 3.5rem rgba(23, 21, 18, 0.18);
}
.player-main { display: flex; align-items: center; gap: 1rem; }
.player-play {
    flex: 0 0 auto;
    width: 3.4rem;
    height: 3.4rem;
    display: grid;
    place-items: center;
    padding: 0;
    color: var(--paper-light);
    background: var(--accent);
    border: 0;
    border-radius: 50%;
    cursor: pointer;
    transition: transform 200ms cubic-bezier(.2,.75,.25,1), background 180ms ease;
}
.player-play:hover { background: #d46143; transform: scale(1.05); }
.player-play:active { transform: scale(0.96); }
.player-play svg { width: 1.35rem; height: 1.35rem; fill: currentColor; }
.player-play .ic-pause { display: none; }
.player.playing .player-play .ic-pause { display: block; }
.player.playing .player-play .ic-play { display: none; }
.player-info { flex: 1; min-width: 0; }
.player-heading {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.5rem;
    color: rgba(250,248,242,0.52);
    font: 650 0.58rem/1 var(--mono);
    letter-spacing: 0.1em;
    text-transform: uppercase;
}
.player-timeline { display: flex; align-items: center; gap: 0.6rem; }
.player-time {
    min-width: 3.2em;
    color: rgba(250,248,242,0.58);
    font: 600 0.66rem/1 var(--mono);
    text-align: center;
    font-variant-numeric: tabular-nums;
}
.player-seek {
    flex: 1;
    min-width: 0;
    height: 3px;
    appearance: none;
    background: linear-gradient(90deg, var(--accent) 0%, var(--accent) var(--seek, 0%), rgba(255,255,255,0.2) var(--seek, 0%));
    border-radius: 0;
    cursor: pointer;
}
.player-seek::-webkit-slider-thumb { width: 0.8rem; height: 0.8rem; appearance: none; background: var(--paper-light); border: 2px solid var(--accent); border-radius: 50%; }
.player-seek::-moz-range-thumb { width: 0.8rem; height: 0.8rem; background: var(--paper-light); border: 2px solid var(--accent); border-radius: 50%; }
.player-controls { display: flex; align-items: center; gap: 0.55rem; margin-top: 0.65rem; }
.player-btn,
.player-dl {
    min-height: 1.9rem;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.28rem 0.6rem;
    color: rgba(250,248,242,0.7);
    background: transparent;
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 0;
    font: 600 0.65rem/1 var(--sans);
    text-decoration: none;
    cursor: pointer;
    transition: color 180ms ease, border-color 180ms ease, background 180ms ease;
}
.player-btn:hover,
.player-dl:hover { color: var(--paper-light); border-color: var(--accent); background: rgba(196,86,57,0.16); }
.player-btn svg,
.player-dl svg { width: 0.8rem; height: 0.8rem; fill: currentColor; }
.player-speed-menu { position: relative; }
.player-speed-options {
    display: none;
    position: absolute;
    bottom: calc(100% + 0.45rem);
    left: 0;
    min-width: 5.5rem;
    padding: 0.3rem;
    background: var(--ink-raised);
    border: 1px solid rgba(255,255,255,0.15);
    box-shadow: 0 1rem 2rem rgba(0,0,0,0.25);
}
.player-speed-options.open { display: block; }
.player-speed-options button {
    width: 100%;
    padding: 0.45rem 0.55rem;
    color: rgba(250,248,242,0.65);
    background: transparent;
    border: 0;
    font: 600 0.68rem/1 var(--sans);
    text-align: left;
    cursor: pointer;
}
.player-speed-options button:hover,
.player-speed-options button.active { color: var(--paper-light); background: rgba(196,86,57,0.2); }
.player-vol { margin-left: auto; }
.player-vol input { width: 4.5rem; accent-color: var(--accent); }

.chapter { max-width: 44rem; margin: 0 auto 7rem; scroll-margin-top: 7.5rem; }
.chapter-intro {
    max-width: 48rem;
    margin-bottom: 6rem;
    padding: 0 0 0 1.5rem;
    border-left: 3px solid var(--accent);
}
.chapter-intro p {
    margin: 0;
    color: var(--ink);
    font: 500 clamp(1.25rem, 2.2vw, 1.65rem)/1.75 var(--serif);
    letter-spacing: -0.015em;
}
.chapter-decoration { display: flex; align-items: baseline; gap: 0.2rem; margin-bottom: 1rem; }
.chapter-num { color: var(--accent); font: 700 0.75rem/1 var(--mono); letter-spacing: 0.08em; }
.chapter-total { color: #9a9286; font: 600 0.62rem/1 var(--mono); }
.chapter-divider { width: 2.2rem; height: 2px; margin-bottom: 1.5rem; background: var(--ink); }
.chapter-title {
    max-width: 18ch;
    margin: 0 0 2rem;
    color: var(--ink);
    font: 520 clamp(2.1rem, 4.7vw, 4rem)/1.02 var(--serif);
    letter-spacing: -0.045em;
    text-wrap: balance;
}
.chapter p {
    max-width: 40rem;
    margin: 0 0 1.55em;
    color: #37322b;
    font-size: clamp(1.02rem, 1.4vw, 1.09rem);
    line-height: 2;
    letter-spacing: 0.012em;
    text-wrap: pretty;
}
.chapter p:last-child { margin-bottom: 0; }
.chapter.is-visible { animation: revealChapter 620ms cubic-bezier(.2,.75,.25,1) both; }
@keyframes revealChapter { from { opacity: 0; transform: translateY(1.4rem); } to { opacity: 1; transform: translateY(0); } }

.page-footer {
    max-width: 44rem;
    margin: 2rem auto 0;
    padding: 2rem 0 0;
    border-top: 1px solid var(--ink);
}
.page-footer a {
    display: inline-flex;
    gap: 0.45rem;
    align-items: center;
    color: var(--ink);
    font-weight: 650;
    text-decoration: none;
}
.page-footer a:hover { color: var(--accent); }
.page-footer p { margin: 1rem 0 0; color: var(--ink-soft); font-size: 0.73rem; line-height: 1.65; }

@media (max-width: 1100px) {
    .toc:not(.toc-force-open), .toc-hidden .toc { transform: translateX(-100%); }
    .toc.open { transform: translateX(0); }
    .toc-overlay.show { display: block; }
    body:not(.toc-hidden) .toc-toggle { left: calc(var(--rail) + 1rem); }
    main, body:not(.toc-hidden) main { margin-inline: auto; }
}
@media (min-width: 1101px) {
    .toc-hidden .toc { transform: translateX(-100%); }
    .toc-overlay { display: none !important; }
}
@media (max-width: 680px) {
    html { font-size: 15px; }
    main { width: min(calc(100% - 1.5rem), 54rem); }
    .hero { padding-top: 5rem; }
    .hero h1 { font-size: clamp(2.5rem, 12vw, 4rem); }
    .toc-toggle { top: 0.75rem; left: 0.75rem; width: 2.75rem; height: 2.75rem; }
    .player {
        position: fixed;
        top: 0.75rem;
        left: 4.25rem;
        right: 0.75rem;
        width: auto;
        margin: 0;
        z-index: 120;
        padding: 0.7rem 0.75rem;
    }
    body:not(.toc-hidden) .player {
        visibility: hidden;
        pointer-events: none;
    }
    .player-main { gap: 0.6rem; }
    .player-play { width: 2.75rem; height: 2.75rem; }
    .player-heading { display: none; }
    .player-time { min-width: 2.8em; font-size: 0.58rem; }
    .player-vol { display: none; }
    .chapter { margin-bottom: 5rem; scroll-margin-top: 10rem; }
    .chapter-title { font-size: clamp(2rem, 10vw, 3rem); }
    .chapter p { font-size: 1rem; line-height: 1.9; }
    .toc { width: min(86vw, var(--rail)); }
    body:not(.toc-hidden) .toc-toggle { left: 0.75rem; }
}

/* Kami-inspired reading refinement: warm paper, one ink color, restrained type. */
:root {
    --ink: #141413;
    --ink-raised: #252523;
    --ink-soft: #66645e;
    --paper: #f5f4ed;
    --paper-light: #faf9f5;
    --paper-deep: #e8e6dc;
    --accent: #1b365d;
    --accent-light: #2d5a8a;
    --line: #dedcd2;
    --rail: 15.5rem;
    --serif: "Noto Serif SC", "Source Han Serif SC", "Songti SC", STSong, Georgia, serif;
    --sans: var(--serif);
    --mono: "SFMono-Regular", "JetBrains Mono", Consolas, monospace;
}
html { font-size: 16px; }
body {
    color: var(--ink);
    background: var(--paper);
    font-family: var(--serif);
    font-size: 1rem;
    line-height: 1.75;
    letter-spacing: 0;
}
body::after { display: none; }
::selection { color: var(--paper-light); background: var(--accent); }
:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 62%, transparent);
    outline-offset: 3px;
}
.progress-bar { height: 2px; background: var(--accent); }

main,
body:not(.toc-hidden) main {
    width: min(calc(100% - 3rem), 70rem);
    margin-inline: auto;
    padding-bottom: 7rem;
}
.hero,
.player {
    width: min(100%, 48rem);
    margin-left: clamp(0rem, 4vw, 4.5rem);
}
.hero {
    padding: clamp(5.5rem, 9vw, 7.5rem) 0 2rem;
    border-bottom: 1px solid var(--line);
}
.hero-kicker {
    margin-bottom: 1rem;
    color: var(--accent);
    font: 500 0.72rem/1.4 var(--mono);
    letter-spacing: 0;
    text-transform: none;
}
.hero h1 {
    max-width: 26ch;
    color: var(--ink);
    font: 500 clamp(2.5rem, 4.6vw, 3.9rem)/1.14 var(--serif);
    letter-spacing: 0;
    text-wrap: balance;
    overflow-wrap: anywhere;
}
.hero h1.title-long {
    font-size: clamp(2.35rem, 4vw, 3.45rem);
}
.hero h1.title-xlong {
    font-size: clamp(2.2rem, 3.6vw, 3.1rem);
}
.hero-sub {
    gap: 0.65rem;
    margin-top: 1.4rem;
    color: var(--ink-soft);
    font: 500 0.75rem/1.5 var(--mono);
    letter-spacing: 0;
}
.hero-divider { display: none; }

.toc {
    inset: 6.5rem max(1.5rem, calc((100vw - 70rem) / 2)) auto auto;
    width: var(--rail);
    max-height: calc(100dvh - 8rem);
    padding: 0;
    color: var(--ink-soft);
    background: transparent;
    border: 0;
    overflow-y: auto;
}
.toc-inner { max-width: none; }
.toc-brand { margin-bottom: 2rem; }
.back-link {
    padding: 0.55rem 0;
    color: var(--ink-soft);
    font-size: 0.75rem;
    font-weight: 500;
    border-color: var(--line);
}
.back-link:hover { color: var(--accent); border-color: var(--accent); }
.toc-title {
    margin-bottom: 0.15rem;
    color: var(--ink);
    font: 500 1.15rem/1.4 var(--serif);
    letter-spacing: 0;
}
.toc-sub {
    color: #89877f;
    font: 400 0.62rem/1.5 var(--mono);
    letter-spacing: 0;
    text-transform: none;
}
.toc-divider {
    width: 100%;
    height: 1px;
    margin: 0.85rem 0 0.65rem;
    background: var(--line);
}
.toc-link {
    grid-template-columns: 1.5rem 1fr;
    gap: 0.45rem;
    padding: 0.55rem 0;
    color: #77756e;
    font-size: 0.76rem;
    line-height: 1.55;
    border-color: color-mix(in srgb, var(--line) 72%, transparent);
}
.toc-link:hover {
    color: var(--accent);
    transform: none;
}
.toc-link.active {
    color: var(--accent);
    font-weight: 600;
}
.toc-num {
    color: var(--accent);
    font: 500 0.62rem/1.8 var(--mono);
    letter-spacing: 0;
}
.toc-toggle { display: none; }

.player {
    position: sticky;
    top: 0.75rem;
    margin-top: 1.75rem;
    margin-bottom: 5.5rem;
    padding: 0.85rem 0.95rem;
    color: var(--ink);
    background: color-mix(in srgb, var(--paper-light) 94%, transparent);
    border: 1px solid var(--line);
    border-radius: 6px;
    box-shadow: 0 0.6rem 2rem rgba(20, 20, 19, 0.06);
}
.player-play {
    width: 3rem;
    height: 3rem;
    color: var(--paper-light);
    background: var(--accent);
}
.player-play:hover { background: var(--accent-light); }
.player-heading {
    margin-bottom: 0.45rem;
    color: var(--ink-soft);
    font: 500 0.62rem/1 var(--mono);
    letter-spacing: 0;
    text-transform: none;
}
.player-time {
    color: var(--ink-soft);
    font: 500 0.66rem/1 var(--mono);
}
.player-seek {
    background: linear-gradient(
        90deg,
        var(--accent) 0%,
        var(--accent) var(--seek, 0%),
        var(--paper-deep) var(--seek, 0%)
    );
}
.player-seek::-webkit-slider-thumb {
    background: var(--paper-light);
    border-color: var(--accent);
}
.player-seek::-moz-range-thumb {
    background: var(--paper-light);
    border-color: var(--accent);
}
.player-btn,
.player-dl {
    color: var(--ink-soft);
    border-color: var(--line);
    border-radius: 4px;
    font: 500 0.66rem/1 var(--serif);
}
.player-btn:hover,
.player-dl:hover {
    color: var(--accent);
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 6%, transparent);
}
.player-speed-options {
    color: var(--ink);
    background: var(--paper-light);
    border-color: var(--line);
    border-radius: 6px;
    box-shadow: 0 0.8rem 2rem rgba(20, 20, 19, 0.1);
}
.player-speed-options button { color: var(--ink-soft); }
.player-speed-options button:hover,
.player-speed-options button.active {
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 8%, transparent);
}
.player-vol input { accent-color: var(--accent); }

.chapter {
    max-width: 42rem;
    margin: 0 0 6.5rem clamp(0rem, 4vw, 4.5rem);
    scroll-margin-top: 7rem;
}
.chapter-intro {
    max-width: 44rem;
    margin-bottom: 5.5rem;
    padding: 0;
    border: 0;
}
.chapter-intro::before {
    content: "导读";
    display: block;
    width: fit-content;
    margin-bottom: 0.75rem;
    color: var(--accent);
    font: 500 0.72rem/1.4 var(--mono);
}
.chapter-intro p {
    color: var(--ink);
    font: 400 clamp(1.12rem, 1.8vw, 1.28rem)/1.8 var(--serif);
    letter-spacing: 0;
}
.chapter-decoration {
    margin-bottom: 0.55rem;
    padding-bottom: 0.55rem;
    border-bottom: 1px solid var(--line);
}
.chapter-num {
    color: var(--accent);
    font: 500 0.72rem/1 var(--mono);
    letter-spacing: 0;
}
.chapter-total {
    color: #918f87;
    font: 400 0.62rem/1 var(--mono);
}
.chapter-divider { display: none; }
.chapter-title {
    max-width: none;
    margin-bottom: 1.65rem;
    color: var(--ink);
    font: 500 clamp(1.75rem, 3vw, 2.25rem)/1.35 var(--serif);
    letter-spacing: 0;
    text-wrap: balance;
}
.chapter p {
    max-width: 38em;
    margin-bottom: 1.3em;
    color: #32322f;
    font: 400 1.075rem/1.9 var(--serif);
    letter-spacing: 0;
    text-align: justify;
    text-justify: inter-ideograph;
    hanging-punctuation: first last;
}
.chapter.is-visible { animation-duration: 420ms; }

.page-footer {
    max-width: 42rem;
    margin: 0 0 0 clamp(0rem, 4vw, 4.5rem);
    padding-top: 1.5rem;
    border-color: var(--line);
}
.page-footer a { color: var(--accent); font-weight: 500; }
.page-footer p { color: var(--ink-soft); font-size: 0.75rem; }

@media (min-width: 1101px) {
    .toc-hidden .toc { transform: translateX(calc(100% + 2rem)); }
    .toc-overlay { display: none !important; }
}
@media (max-width: 1100px) {
    main,
    body:not(.toc-hidden) main {
        width: min(calc(100% - 2.5rem), 48rem);
        margin-inline: auto;
    }
    .hero,
    .player,
    .chapter,
    .chapter-intro,
    .page-footer {
        width: 100%;
        margin-left: 0;
    }
    .toc-toggle {
        display: grid;
        color: var(--accent);
        background: color-mix(in srgb, var(--paper-light) 94%, transparent);
        border-color: var(--line);
        border-radius: 6px;
        box-shadow: 0 0.5rem 1.5rem rgba(20, 20, 19, 0.08);
    }
    .toc {
        inset: 0 auto 0 0;
        width: min(86vw, 19rem);
        max-height: none;
        padding: 1.25rem 1.35rem 2rem;
        color: var(--ink-soft);
        background: var(--paper-light);
        border-right: 1px solid var(--line);
        transform: translateX(-100%);
    }
    .toc.open { transform: translateX(0); }
    .toc-overlay.show {
        display: block;
        background: rgba(20, 20, 19, 0.28);
        backdrop-filter: blur(2px);
    }
    body:not(.toc-hidden) .toc-toggle { left: calc(min(86vw, 19rem) + 1rem); }
}
@media (max-width: 680px) {
    html { font-size: 15px; }
    main,
    body:not(.toc-hidden) main {
        width: calc(100% - 2rem);
    }
    .hero { padding-top: 5.5rem; }
    .hero h1 {
        max-width: none;
        font-size: clamp(2.15rem, 10vw, 3.15rem);
        line-height: 1.18;
    }
    .hero h1.title-long,
    .hero h1.title-xlong {
        font-size: 1.9rem;
        line-height: 1.28;
    }
    .toc-toggle { top: 0.75rem; left: 0.75rem; }
    .player {
        position: fixed;
        top: 0.75rem;
        left: 4.25rem;
        right: 0.75rem;
        width: auto;
        margin-top: 0;
        padding: 0.65rem 0.7rem;
    }
    .player-play { width: 2.75rem; height: 2.75rem; }
    .chapter { margin-bottom: 4.75rem; }
    .chapter-title {
        font-size: 1.72rem;
        line-height: 1.4;
    }
    .chapter p {
        max-width: none;
        font-size: 1.04rem;
        line-height: 1.88;
        text-align: left;
    }
    body:not(.toc-hidden) .toc-toggle { left: 0.75rem; }
}
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { scroll-behavior: auto !important; animation: none !important; transition: none !important; }
}
</style>
</head>
<body>
<a class="skip-link" href="#main-content">跳到正文</a>

<div class="progress-bar" id="progressBar"></div>

<button class="toc-toggle" id="tocToggle" aria-label="打开目录" aria-controls="toc" aria-expanded="false">
    <svg class="toc-icon-hamburger" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
        <line x1="3" y1="5" x2="17" y2="5"/>
        <line x1="3" y1="10" x2="17" y2="10"/>
        <line x1="3" y1="15" x2="17" y2="15"/>
    </svg>
    <svg class="toc-icon-close" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" style="display:none">
        <line x1="4" y1="4" x2="16" y2="16"/>
        <line x1="16" y1="4" x2="4" y2="16"/>
    </svg>
</button>
<div class="toc-overlay" id="tocOverlay"></div>

<nav class="toc" id="toc" aria-label="章节目录">
    <div class="toc-inner">
        <div class="toc-brand">
            <a class="back-link" href="../">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
                返回节目资料库
            </a>
        </div>
        <h2 class="toc-title">目录</h2>
        <p class="toc-sub">Contents / chapter index</p>
        <div class="toc-divider"></div>
        <ul>
{toc_items}
        </ul>
    </div>
</nav>

<div class="layout">
<main id="main-content">
    <header class="hero">
        <p class="hero-kicker">中文深度讲稿 / AI reviewed</p>
        <h1 class="{hero_title_class}">{hero_title}</h1>
        <p class="hero-sub">
            <span>{date}</span>
            <span class="sep">·</span>
            <span>{word_count}</span>
            <span class="sep">·</span>
            <span>{chapter_count} 章节</span>
        </p>
        <div class="hero-divider"></div>
    </header>

    <section class="player" id="podcastPlayer" aria-label="音频播放器">
        <audio id="podcastAudio" preload="metadata" src="{mp3_url}"></audio>
        <div class="player-main">
            <button class="player-play" id="playerPlay" aria-label="播放" aria-pressed="false">
                <svg class="ic-play" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                <svg class="ic-pause" viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>
            </button>
            <div class="player-info">
                <div class="player-heading">
                    <span>中文音频</span>
                    <span>边听边读</span>
                </div>
                <div class="player-timeline">
                    <span class="player-time" id="playerCur">0:00</span>
                    <input class="player-seek" id="playerSeek" type="range" min="0" max="100" value="0" step="0.1" aria-label="播放进度">
                    <span class="player-time" id="playerDur">0:00</span>
                </div>
                <div class="player-controls">
                    <div class="player-speed-menu">
                        <button class="player-btn player-speed" id="playerSpeedBtn" aria-label="播放速度" aria-expanded="false">1.0x</button>
                        <div class="player-speed-options" id="playerSpeedOptions" role="menu">
                            <button type="button" role="menuitem" data-speed="0.5">0.5x</button>
                            <button type="button" role="menuitem" data-speed="0.75">0.75x</button>
                            <button type="button" role="menuitem" data-speed="1" class="active">1x</button>
                            <button type="button" role="menuitem" data-speed="1.25">1.25x</button>
                            <button type="button" role="menuitem" data-speed="1.5">1.5x</button>
                            <button type="button" role="menuitem" data-speed="1.75">1.75x</button>
                            <button type="button" role="menuitem" data-speed="2">2x</button>
                        </div>
                    </div>
                    <span class="player-btn player-vol" aria-label="音量">
                        <svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"/></svg>
                        <input id="playerVolume" type="range" min="0" max="1" step="0.01" value="1" aria-label="音量">
                    </span>
                    <a class="player-dl" id="playerDl" href="{mp3_url}" download aria-label="下载 mp3">
                        <svg viewBox="0 0 24 24"><path d="M11 3h2v13h-2zM5 17h14v2H5z"/></svg>
                        下载
                    </a>
                </div>
            </div>
        </div>
    </section>

{content}

    <footer class="page-footer">
        <a href="../"><span aria-hidden="true">←</span> 返回全部节目</a>
        <p>播客中的观点不代表本站立场。中文内容用于学习与信息整理，事实与动态数据以原始来源为准。</p>
    </footer>
</main>
</div>

<script>
(function() {{
    'use strict';

    // Chapters reveal when they enter the viewport; content remains visible
    // without IntersectionObserver or when reduced motion is requested.
    var chapters = document.querySelectorAll('.chapter');
    if ('IntersectionObserver' in window &&
            !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {{
        var chapterObserver = new IntersectionObserver(function(entries, observer) {{
            entries.forEach(function(entry) {{
                if (entry.isIntersecting) {{
                    entry.target.classList.add('is-visible');
                    observer.unobserve(entry.target);
                }}
            }});
        }}, {{ rootMargin: '0px 0px -12% 0px', threshold: 0.06 }});
        for (var c = 0; c < chapters.length; c++) {{
            chapterObserver.observe(chapters[c]);
        }}
    }} else {{
        for (var c2 = 0; c2 < chapters.length; c2++) {{
            chapters[c2].classList.add('is-visible');
        }}
    }}

    // state tracked on body
    var toc = document.getElementById('toc');
    var toggle = document.getElementById('tocToggle');
    var overlay = document.getElementById('tocOverlay');
    var isOpen = false;

    function shouldStartOpen() {{
        return window.innerWidth >= 1100;
    }}

    function applyState(open) {{
        isOpen = open;
        document.body.classList.toggle('toc-hidden', !open);
        toc.classList.toggle('open', open);
        if (overlay) overlay.classList.toggle('show', open);

        var hamburger = toggle.querySelector('.toc-icon-hamburger');
        var closeIcon = toggle.querySelector('.toc-icon-close');
        if (hamburger) hamburger.style.display = open ? 'none' : '';
        if (closeIcon) closeIcon.style.display = open ? '' : 'none';
        toggle.setAttribute('aria-label', open ? '关闭目录' : '打开目录');
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');

        if (window.innerWidth < 1100) {{
            document.body.style.overflow = open ? 'hidden' : '';
        }}
    }}

    applyState(shouldStartOpen());

    var progressBar = document.getElementById('progressBar');
    if (progressBar) {{
        window.addEventListener('scroll', function() {{
            var scrollTop = window.scrollY;
            var docHeight = document.documentElement.scrollHeight - window.innerHeight;
            var progress = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;
            progressBar.style.width = progress + '%';
        }});
    }}

    if (toggle) {{
        toggle.addEventListener('click', function(e) {{
            e.stopPropagation();
            applyState(!isOpen);
        }});
    }}

    if (overlay) {{
        overlay.addEventListener('click', function() {{
            applyState(false);
        }});
    }}

    var resizeTimer;
    window.addEventListener('resize', function() {{
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function() {{
            if (window.innerWidth >= 1100) {{
                document.body.style.overflow = '';
            }} else if (isOpen) {{
                document.body.style.overflow = 'hidden';
            }}
        }}, 200);
    }});

    var links = document.querySelectorAll('.toc-link');
    var sections = [];
    for (var i = 0; i < links.length; i++) {{
        var href = links[i].getAttribute('href');
        if (href && href.charAt(0) === '#') {{
            var el = document.getElementById(href.substring(1));
            if (el) {{
                sections.push({{
                    link: links[i],
                    el: el,
                    top: 0,
                    bottom: 0
                }});
            }}
        }}
    }}

    function updateActive() {{
        var scrollY = window.scrollY + 100;
        var current = null;
        for (var i = sections.length - 1; i >= 0; i--) {{
            var s = sections[i];
            s.top = s.el.offsetTop;
            if (scrollY >= s.top) {{
                current = s;
                break;
            }}
        }}
        if (!current && sections.length > 0) {{
            current = sections[0];
        }}
        for (var j = 0; j < sections.length; j++) {{
            sections[j].link.classList.remove('active');
            sections[j].link.removeAttribute('aria-current');
        }}
        if (current) {{
            current.link.classList.add('active');
            current.link.setAttribute('aria-current', 'location');
        }}
    }}

    var ticking = false;
    window.addEventListener('scroll', function() {{
        if (!ticking) {{
            window.requestAnimationFrame(function() {{
                updateActive();
                ticking = false;
            }});
            ticking = true;
        }}
    }});
    updateActive();

    for (var k = 0; k < links.length; k++) {{
        links[k].addEventListener('click', function() {{
            if (window.innerWidth < 1100) {{
                applyState(false);
            }}
        }});
    }}

    document.addEventListener('keydown', function(e) {{
        if (e.key === 'Escape' && isOpen) {{
            applyState(false);
        }}
    }});
}})();
</script>

<script>
(function () {
    'use strict';
    var audio = document.getElementById('podcastAudio');
    if (!audio || !audio.src) return;
    var player = document.getElementById('podcastPlayer');
    var playBtn = document.getElementById('playerPlay');
    var seek = document.getElementById('playerSeek');
    var cur = document.getElementById('playerCur');
    var dur = document.getElementById('playerDur');
    var vol = document.getElementById('playerVolume');
    var speedBtn = document.getElementById('playerSpeedBtn');
    var speedOpts = document.getElementById('playerSpeedOptions');

    function fmt(t) {
        if (!isFinite(t) || t < 0) t = 0;
        var m = Math.floor(t / 60);
        var s = Math.floor(t % 60);
        return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function paintSeek() {
        if (!audio.duration) return;
        var pct = (audio.currentTime / audio.duration) * 100;
        seek.value = audio.currentTime;
        seek.style.setProperty('--seek', pct + '%');
    }

    audio.addEventListener('loadedmetadata', function () {
        dur.textContent = fmt(audio.duration);
        seek.max = audio.duration || 0;
        paintSeek();
    });
    audio.addEventListener('timeupdate', function () {
        cur.textContent = fmt(audio.currentTime);
        paintSeek();
    });
    seek.addEventListener('input', function () {
        audio.currentTime = parseFloat(seek.value);
        paintSeek();
    });

    playBtn.addEventListener('click', function () {
        if (audio.paused) { audio.play().catch(function () {}); }
        else { audio.pause(); }
    });
    audio.addEventListener('play', function () {
        player.classList.add('playing');
        playBtn.setAttribute('aria-label', '暂停');
        playBtn.setAttribute('aria-pressed', 'true');
    });
    audio.addEventListener('pause', function () {
        player.classList.remove('playing');
        playBtn.setAttribute('aria-label', '播放');
        playBtn.setAttribute('aria-pressed', 'false');
    });
    audio.addEventListener('ended', function () {
        player.classList.remove('playing');
        playBtn.setAttribute('aria-label', '播放');
        playBtn.setAttribute('aria-pressed', 'false');
    });

    vol.addEventListener('input', function () { audio.volume = parseFloat(vol.value); });

    speedBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        speedOpts.classList.toggle('open');
        speedBtn.setAttribute(
            'aria-expanded', speedOpts.classList.contains('open') ? 'true' : 'false');
    });
    speedOpts.querySelectorAll('button').forEach(function (b) {
        b.addEventListener('click', function (e) {
            e.stopPropagation();
            audio.playbackRate = parseFloat(b.getAttribute('data-speed'));
            speedBtn.textContent = b.textContent;
            speedOpts.querySelectorAll('button').forEach(function (x) { x.classList.remove('active'); });
            b.classList.add('active');
            speedOpts.classList.remove('open');
            speedBtn.setAttribute('aria-expanded', 'false');
        });
    });
    document.addEventListener('click', function () {
        speedOpts.classList.remove('open');
        speedBtn.setAttribute('aria-expanded', 'false');
    });
})();
</script>
</body>
</html>
"""


# ── 主函数（库接口）────────────────────────────────────────────────

def md_to_html(md_path, output_path=None, podcast_title=None):
    """
    将讲书稿.md 转为精美 HTML 页面。

    参数:
        md_path: MD 文件路径
        output_path: 输出 HTML 路径（默认同目录下 讲书稿.html）
        podcast_title: 播客标题（默认取文件夹名或 MD 文件名）

    返回:
        生成的 HTML 文件路径，失败返回 None
    """
    md_path = Path(md_path)
    if not md_path.exists():
        print(f"[HTML] 找不到文件: {md_path}", flush=True)
        return None

    # 确定标题
    if not podcast_title:
        podcast_title = md_path.parent.name
        if podcast_title in ("content", "") or not podcast_title:
            podcast_title = md_path.stem

    # 读 MD
    md_bytes = md_path.read_bytes()
    source_sha256 = hashlib.sha256(md_bytes).hexdigest()
    md_text = md_bytes.decode("utf-8")

    # 清理可能的 YAML front matter
    md_text = re.sub(r"^---\n.*?\n---\n", "", md_text, flags=re.DOTALL)
    # 页面 Hero 已展示标题，正文中的 Markdown H1 不再重复渲染。
    md_text = re.sub(r"^# .+\n+", "", md_text, count=1)

    sections = parse_sections(md_text)
    if not sections:
        print(f"[HTML] 文件为空或格式不对: {md_path}", flush=True)
        return None

    # 统计字数（中文 + 英文单词）
    word_count = len(re.findall(r"[\u4e00-\u9fff]", md_text))
    word_count += len(re.findall(r"[a-zA-Z]+", md_text))

    # 日期
    date_str = None
    source_path = md_path.parent / "来源.md"
    if source_path.exists():
        src_text = source_path.read_text(encoding="utf-8")
        m = re.search(r"处理日期：(\S+)", src_text)
        if m:
            date_str = m.group(1)
    if not date_str:
        mtime = os.path.getmtime(md_path)
        date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")

    # 音频播放器：配置了 R2 公开地址则用绝对 URL（支持 Range 流式分片），否则回退相对路径 {播客名}.mp3
    if R2_PUBLIC_URL:
        mp3_url = public_audio_url(md_path.parent, R2_PUBLIC_URL)
    else:
        mp3_url = f"{md_path.parent.name}.mp3"

    html = _build_html(
        podcast_title, sections, word_count, date_str, mp3_url,
        source_sha256=source_sha256)

    # 输出路径
    if not output_path:
        output_path = md_path.with_suffix(".html")
    else:
        output_path = Path(output_path)

    atomic_write_text(output_path, html)
    size_kb = len(html) // 1024
    print(f"[HTML] {word_count:,} 字 → {output_path} ({size_kb}KB)", flush=True)
    return str(output_path)


# ── CLI 入口 ──────────────────────────────────────────────────────

def cli_main():
    parser = argparse.ArgumentParser(
        description="将讲书稿.md 转为精美 HTML 阅读页面"
    )
    parser.add_argument("input_md", nargs="?",
                        help="讲书稿.md 文件路径")
    parser.add_argument("-o", "--output", default=None,
                        help="输出 HTML 路径（默认同目录下 讲书稿.html）")
    parser.add_argument("--title", default=None,
                        help="播客标题（默认取文件夹名）")
    parser.add_argument(
        "--allow-unchecked",
        action="store_true",
        help="显式绕过统一质量门；绕过行为会写入 run_report.json",
    )

    args = parser.parse_args()

    input_md = args.input_md or input("输入 MD 文件路径: ")
    if not os.path.exists(input_md):
        print(f"❌ 找不到 {input_md}")
        sys.exit(1)

    from preflight import quality_gate
    from run_report import RunReport
    folder = Path(input_md).parent
    report = RunReport(folder, "html.cli", {
        "entry_point": "html.cli",
        "allow_unchecked": args.allow_unchecked,
    })
    try:
        if args.allow_unchecked:
            with report.stage("quality_gate_bypass") as stage:
                stage.metrics.update({
                    "entry_point": "html.cli",
                    "allow_unchecked": True,
                    "reason": "explicit CLI flag",
                })
        else:
            with report.stage("quality_gate") as stage:
                passed = quality_gate(folder, run_report=report)
                stage.metrics["passed"] = passed
                if not passed:
                    stage.fail("quality gate failed")
                    report.finish(False, "quality gate failed")
                    sys.exit(1)
        result = md_to_html(input_md, args.output, args.title)
        report.finish(bool(result), None if result else "HTML generation failed")
    except BaseException as exc:
        if not report._finished:
            report.finish(False, exc)
        raise
    if result:
        print(f"✅ HTML 已生成: {result}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    cli_main()
