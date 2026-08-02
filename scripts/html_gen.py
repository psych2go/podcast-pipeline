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

_scripts = str(Path(__file__).resolve().parent)
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import argparse
import os
import re
from datetime import date, datetime

from config import BASE_DIR, R2_PUBLIC_URL


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

def _build_html(title, sections, word_count=None, date_str=None, mp3_url=None):
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
    wc_str = f"{word_count:,} 字" if word_count else ""
    date_str = date_str or date.today().isoformat()

    # 模板用 .format() 时 CSS/JS 需要 {{ }} 转义，改成 .replace() 后统一转成单括号
    html = HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    for key, val in {
        "title": safe_title,
        "podcast_title": safe_title,
        "date": date_str,
        "word_count": wc_str,
        "toc_items": toc_html,
        "content": content_html,
        "chapter_count": str(total),
        "mp3_url": mp3_url,
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
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%236d2c2c'/%3E%3Ctext x='32' y='43' font-family='Georgia,serif' font-size='32' fill='%23bf9b4a' text-anchor='middle'%3E%E4%B9%A6%3C/text%3E%3C/svg%3E">
<title>{podcast_title} — 讲稿</title>
<style>
/* ══ Reset & Base ══ */
* {{ margin: 0; padding: 0; box-sizing: border-box; }}

html {{
    scroll-behavior: smooth;
    font-size: 16px;
}}

body {{
    font-family:
        "Noto Sans SC", "PingFang SC", "Microsoft YaHei",
        "Helvetica Neue", Arial, sans-serif;
    color: #2b2118;
    background: #fbf9f5;
    line-height: 1.8;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}}

/* 微噪点纹理，打破纯平数字感 */
body::after {{
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 9999;
    opacity: 0.025;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}}

/* 焦点环：键盘可达性（所有可交互元素） */
:focus-visible {{
    outline: 2px solid #6d2c2c;
    outline-offset: 2px;
    border-radius: 4px;
}}

/* ══ Progress Bar ══ */
.progress-bar {{
    position: fixed;
    top: 0;
    left: 0;
    width: 0%;
    height: 2px;
    background: linear-gradient(90deg, #6d2c2c 0%, #bf9b4a 100%);
    z-index: 1000;
    transition: width 0.1s linear;
}}

/* ══ Typography ══ */
h1, h2, h3, h4 {{
    font-family:
        "DM Serif Display", "Noto Serif SC", "Songti SC", Georgia, serif;
    font-weight: 600;
    line-height: 1.4;
    color: #2b2118;
    text-wrap: balance;
}}

p {{
    margin-bottom: 1.4em;
    font-size: 1.05rem;
    letter-spacing: 0.01em;
    color: #3a2f24;
}}

/* ══ Sidebar TOC ══ */
.toc {{
    position: fixed;
    top: 0;
    left: 0;
    width: 280px;
    height: 100vh;
    overflow-y: auto;
    background: #faf5ee;
    border-right: 1px solid #ece3d3;
    padding: 2rem 1.2rem;
    z-index: 100;
    transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}}

.toc-inner {{
    max-width: 220px;
    margin: 0 auto;
}}

.toc-brand {{
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    color: #a8987f;
    margin-bottom: 0.4rem;
    font-weight: 500;
}}

.toc-title {{
    font-size: 1rem;
    font-weight: 700;
    color: #111;
    margin-bottom: 0.2rem;
    line-height: 1.4;
}}

.toc-sub {{
    font-size: 0.75rem;
    color: #999;
    margin-bottom: 1rem;
}}

.toc-divider {{
    width: 1.5rem;
    height: 2px;
    background: #6d2c2c;
    margin: 0.8rem 0 1.2rem;
    border-radius: 1px;
}}

.toc ul {{
    list-style: none;
    padding: 0;
    margin: 0;
}}

.toc li {{
    margin-bottom: 0.15rem;
}}

.toc-link {{
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.35rem 0.6rem;
    font-family:
        "Noto Sans SC", "PingFang SC", "Microsoft YaHei",
        "Helvetica Neue", Arial, sans-serif;
    font-size: 0.82rem;
    color: #6f6152;
    text-decoration: none;
    border-radius: 5px;
    transition: all 0.15s ease;
    line-height: 1.45;
}}

.toc-link:hover {{
    color: #6d2c2c;
    background: rgba(109, 44, 44, 0.06);
}}

.toc-link.active {{
    color: #6d2c2c;
    background: rgba(109, 44, 44, 0.08);
    font-weight: 600;
}}

.toc-num {{
    min-width: 1.5rem;
    font-size: 0.72rem;
    color: #c4b59b;
    font-feature-settings: "tnum";
    text-align: right;
    flex-shrink: 0;
}}

.toc-link.active .toc-num {{
    color: #bf9b4a;
}}

.toc-label {{
    flex: 1;
}}

/* TOC toggle */
.toc-toggle {{
    display: flex;
    position: fixed;
    top: 1rem;
    left: 1rem;
    z-index: 110;
    background: #fbf9f5;
    color: #4a3a2a;
    border: 1px solid #e8dcc8;
    border-radius: 10px;
    width: 44px;
    height: 44px;
    font-size: 1.1rem;
    cursor: pointer;
    box-shadow: 0 1px 6px rgba(109, 44, 44, 0.08);
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    align-items: center;
    justify-content: center;
}}

body:not(.toc-hidden) .toc-toggle {{
    left: calc(280px + 1rem);
}}

.toc-toggle:hover {{
    background: #f5f5f5;
    box-shadow: 0 2px 12px rgba(0,0,0,0.1);
}}

.toc-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.2);
    z-index: 90;
}}

/* ══ Main Content ══ */
.layout {{
    display: flex;
    min-height: 100vh;
}}

main {{
    flex: 1;
    max-width: 720px;
    margin: 0 auto;
    padding: 0 2rem 5rem;
    transition: margin-left 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}}

body:not(.toc-hidden) main {{
    margin-left: 320px;
}}

/* ══ Hero ══ */
.hero {{
    text-align: left;
    padding: 4rem 0 2.5rem;
    margin-bottom: 0.5rem;
}}

.hero h1 {{
    font-size: 1.8rem;
    font-weight: 500;
    color: #2b2118;
    margin-bottom: 0.6rem;
    letter-spacing: 0.01em;
    line-height: 1.35;
}}

.hero-sub {{
    font-size: 0.82rem;
    color: #9c8c76;
    letter-spacing: 0.03em;
}}

.hero-sub .sep {{
    color: #ddd3c2;
    padding: 0 0.5rem;
}}

.hero-divider {{
    width: 2.5rem;
    height: 2px;
    background: #6d2c2c;
    margin: 1.5rem 0 2rem;
    border-radius: 1px;
}}

/* ══ Chapter Sections ══ */
.chapter {{
    margin-bottom: 3rem;
    padding-top: 1rem;
}}

.chapter-num {{
    display: inline-block;
    font-size: 0.7rem;
    font-weight: 600;
    color: #bf9b4a;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 0.4rem;
}}

.chapter-title {{
    font-size: 1.35rem;
    font-weight: 500;
    margin-bottom: 1.2rem;
    letter-spacing: 0.01em;
    color: #2b2118;
    line-height: 1.4;
}}

.chapter p {{
    margin-bottom: 1.2em;
}}

.chapter p:last-child {{
    margin-bottom: 0;
}}

/* ══ Entry Animation ══ */
@keyframes fadeSlideUp {{
    from {{
        opacity: 0;
        transform: translateY(16px);
    }}
    to {{
        opacity: 1;
        transform: translateY(0);
    }}
}}

.hero {{
    animation: fadeSlideUp 0.5s ease forwards;
}}

.chapter {{
    opacity: 0;
    animation: fadeSlideUp 0.4s ease forwards;
}}

/* ══ Responsive ══ */
@media (max-width: 1100px) {{
    .toc:not(.toc-force-open) {{
        transform: translateX(-100%);
    }}
    .toc.open {{
        transform: translateX(0);
    }}
    .toc-overlay.show {{
        display: block;
    }}
    main {{
        padding: 0 1.5rem 4rem;
        margin-left: 0 !important;
    }}
    .toc-hidden .toc {{
        transform: translateX(-100%);
    }}
}}

@media (min-width: 1101px) {{
    .toc-hidden .toc {{
        transform: translateX(-100%);
    }}
    .toc-overlay {{
        display: none !important;
    }}
}}

@media (max-width: 640px) {{
    html {{
        font-size: 15px;
    }}
    main {{
        padding: 0 1rem 3rem;
    }}
    .hero {{
        padding: 3rem 0 1.5rem;
    }}
    .hero h1 {{
        font-size: 1.35rem;
    }}
    .chapter-title {{
        font-size: 1.15rem;
    }}
    p {{
        font-size: 1rem;
        line-height: 1.75;
    }}
}}

/* ══ Selection ══ */
::selection {{
    background: rgba(109, 44, 44, 0.15);
    color: #2b2118;
}}

/* ══ Scrollbar (Webkit) ══ */
.toc::-webkit-scrollbar {{
    width: 3px;
}}
.toc::-webkit-scrollbar-track {{
    background: transparent;
}}
.toc::-webkit-scrollbar-thumb {{
    background: #ddd;
    border-radius: 2px;
}}

/* ══ Smooth image/block rendering ══ */
img, video, canvas {{
    max-width: 100%;
    height: auto;
}}

/* ══ 音频播放器 ══ */
.player {
    display: flex;
    align-items: center;
    background: #fffdf9;
    border: 1px solid #ece3d3;
    border-radius: 14px;
    padding: 1rem 1.25rem;
    margin: 0 0 2.5rem;
    box-shadow: 0 2px 12px rgba(109, 44, 44, 0.06);
}
.player-main {
    display: flex;
    align-items: center;
    gap: 1.1rem;
    width: 100%;
    flex-wrap: wrap;
}
.player-play {
    flex-shrink: 0;
    width: 54px;
    height: 54px;
    border-radius: 50%;
    border: none;
    background: #6d2c2c;
    color: #f6ead9;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.2s ease, background 0.2s ease;
    box-shadow: 0 2px 10px rgba(109, 44, 44, 0.25);
}
.player-play:hover { background: #822f2f; transform: scale(1.05); }
.player-play:active { transform: scale(0.95); }
.player-play svg { width: 22px; height: 22px; fill: currentColor; }
.player-play .ic-pause { display: none; }
.player.playing .player-play .ic-pause { display: block; }
.player.playing .player-play .ic-play { display: none; }
.player-info { flex: 1; min-width: 220px; }
.player-timeline {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.6rem;
}
.player-time {
    font-size: 0.75rem;
    color: #8a7a6a;
    font-variant-numeric: tabular-nums;
    min-width: 3.2em;
    text-align: center;
    flex-shrink: 0;
}
.player-seek {
    flex: 1;
    -webkit-appearance: none;
    appearance: none;
    height: 4px;
    border-radius: 2px;
    background: linear-gradient(90deg, #6d2c2c 0%, #bf9b4a var(--seek, 0%), #e8dcc8 var(--seek, 0%));
    outline: none;
    cursor: pointer;
}
.player-seek::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: #6d2c2c;
    border: 2px solid #f6ead9;
    box-shadow: 0 1px 4px rgba(109, 44, 44, 0.3);
}
.player-seek::-moz-range-thumb {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: #6d2c2c;
    border: 2px solid #f6ead9;
}
.player-controls {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
}
.player-btn {
    background: none;
    border: 1px solid #e0d6c8;
    border-radius: 6px;
    padding: 0.3rem 0.6rem;
    font-size: 0.75rem;
    color: #6f6152;
    cursor: pointer;
    transition: all 0.15s;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-family: inherit;
}
.player-btn:hover { border-color: #6d2c2c; color: #6d2c2c; background: #fdf6ee; }
.player-btn svg { width: 13px; height: 13px; fill: currentColor; }
.player-speed-menu { position: relative; }
.player-speed-options {
    display: none;
    position: absolute;
    bottom: 100%;
    left: 0;
    background: #fffdf9;
    border: 1px solid #e0d6c8;
    border-radius: 8px;
    padding: 0.25rem;
    box-shadow: 0 6px 20px rgba(109, 44, 44, 0.14);
    z-index: 30;
    min-width: 5.5rem;
    margin-bottom: 0.3rem;
}
.player-speed-options.open { display: block; }
.player-speed-options button {
    display: block;
    width: 100%;
    text-align: left;
    border: none;
    background: none;
    padding: 0.35rem 0.6rem;
    font-size: 0.78rem;
    border-radius: 5px;
    cursor: pointer;
    color: #5a4a3a;
    font-family: inherit;
}
.player-speed-options button:hover { background: #f5ead9; }
.player-speed-options button.active { color: #6d2c2c; font-weight: 600; }
.player-vol {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.35rem 0.5rem;
}
.player-vol input {
    width: 5rem;
    accent-color: #6d2c2c;
    cursor: pointer;
}
.player-dl {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    text-decoration: none;
    border: 1px solid #6d2c2c;
    border-radius: 6px;
    padding: 0.3rem 0.75rem;
    font-size: 0.75rem;
    color: #6d2c2c;
    font-weight: 500;
    transition: all 0.15s;
}
.player-dl:hover { background: #6d2c2c; color: #f6ead9; }
.player-dl svg { width: 13px; height: 13px; fill: currentColor; }

@media (max-width: 640px) {
    .player { padding: 0.9rem 0.9rem; }
    .player-play { width: 46px; height: 46px; }
    .player-play svg { width: 18px; height: 18px; }
}
</style>
</head>
<body>

<div class="progress-bar" id="progressBar"></div>

<button class="toc-toggle" id="tocToggle" aria-label="打开目录">
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

<nav class="toc" id="toc">
    <div class="toc-inner">
        <div class="toc-brand"><a href="../" style="display:flex;align-items:center;gap:6px;padding:6px 10px;margin-bottom:6px;border:1px solid #e0d6c8;border-radius:8px;background:#fdf6ee;text-decoration:none;color:#6d2c2c;font-size:1rem;font-weight:700;line-height:1.4;transition:background .15s" onmouseover="this.style.background='#f5e8d8'" onmouseout="this.style.background='#fdf6ee'">
<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
播客列表
</a></div>
        <h2 class="toc-title">目录</h2>
        <div class="toc-divider"></div>
        <ul>
{toc_items}
        </ul>
    </div>
</nav>

<div class="layout">
<main>
    <header class="hero">
        <div class="hero-accent">
            <span class="hero-accent-line"></span>
            <span class="hero-accent-dot"></span>
            <span class="hero-accent-line"></span>
        </div>
        <h1>{podcast_title}</h1>
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
            <button class="player-play" id="playerPlay" aria-label="播放 / 暂停">
                <svg class="ic-play" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                <svg class="ic-pause" viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>
            </button>
            <div class="player-info">
                <div class="player-timeline">
                    <span class="player-time" id="playerCur">0:00</span>
                    <input class="player-seek" id="playerSeek" type="range" min="0" max="100" value="0" step="0.1" aria-label="播放进度">
                    <span class="player-time" id="playerDur">0:00</span>
                </div>
                <div class="player-controls">
                    <div class="player-speed-menu">
                        <button class="player-btn player-speed" id="playerSpeedBtn" aria-label="播放速度">1.0x</button>
                        <div class="player-speed-options" id="playerSpeedOptions">
                            <button type="button" data-speed="0.5">0.5x</button>
                            <button type="button" data-speed="0.75">0.75x</button>
                            <button type="button" data-speed="1" class="active">1x</button>
                            <button type="button" data-speed="1.25">1.25x</button>
                            <button type="button" data-speed="1.5">1.5x</button>
                            <button type="button" data-speed="1.75">1.75x</button>
                            <button type="button" data-speed="2">2x</button>
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

</main>
</div>

<script>
(function() {{
    'use strict';

    // Dynamic chapter animation delays (supports any number of chapters)
    var chapters = document.querySelectorAll('.chapter');
    for (var c = 0; c < chapters.length; c++) {{
        chapters[c].style.animationDelay = (0.1 + c * 0.05) + 's';
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
        }}
        if (current) {{
            current.link.classList.add('active');
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
    audio.addEventListener('play', function () { player.classList.add('playing'); });
    audio.addEventListener('pause', function () { player.classList.remove('playing'); });
    audio.addEventListener('ended', function () { player.classList.remove('playing'); });

    vol.addEventListener('input', function () { audio.volume = parseFloat(vol.value); });

    speedBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        speedOpts.classList.toggle('open');
    });
    speedOpts.querySelectorAll('button').forEach(function (b) {
        b.addEventListener('click', function (e) {
            e.stopPropagation();
            audio.playbackRate = parseFloat(b.getAttribute('data-speed'));
            speedBtn.textContent = b.textContent;
            speedOpts.querySelectorAll('button').forEach(function (x) { x.classList.remove('active'); });
            b.classList.add('active');
            speedOpts.classList.remove('open');
        });
    });
    document.addEventListener('click', function () { speedOpts.classList.remove('open'); });
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
    md_text = md_path.read_text(encoding="utf-8")

    # 清理可能的 YAML front matter
    md_text = re.sub(r"^---\n.*?\n---\n", "", md_text, flags=re.DOTALL)

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
    folder = md_path.parent.name
    if R2_PUBLIC_URL:
        mp3_url = f"{R2_PUBLIC_URL}/{folder}/{folder}.mp3"
    else:
        mp3_url = f"{folder}.mp3"

    html = _build_html(podcast_title, sections, word_count, date_str, mp3_url)

    # 输出路径
    if not output_path:
        output_path = md_path.with_suffix(".html")
    else:
        output_path = Path(output_path)

    output_path.write_text(html, encoding="utf-8")
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

    args = parser.parse_args()

    input_md = args.input_md or input("输入 MD 文件路径: ")
    if not os.path.exists(input_md):
        print(f"❌ 找不到 {input_md}")
        sys.exit(1)

    result = md_to_html(input_md, args.output, args.title)
    if result:
        print(f"✅ HTML 已生成: {result}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    cli_main()
