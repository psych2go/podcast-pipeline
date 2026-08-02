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
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

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

# 来源域名 → 台账/站点显示名
_SOURCE_LABELS = {
    "podcasts.happyscribe.com": "HappyScribe",
    "happyscribe.com": "HappyScribe",
    "nav.al": "nav.al",
    "singjupost.com": "SingjuPost",
}


def _zh_chars(s):
    return len(re.findall(r"[一-鿿]", s))


def _find_briefing(folder):
    """定位讲稿文件（兼容 讲书稿.md / 简报.md 命名）。"""
    for cand in (f"{folder.name} - 讲书稿.md", "讲书稿.md",
                 f"{folder.name} - 简报.md", "简报.md"):
        p = folder / cand
        if p.exists():
            return p
    hits = list(folder.glob("*讲书稿.md")) or list(folder.glob("*简报.md"))
    return hits[0] if hits else None


def _gen_mp3(folder):
    """该期生成的 MP3（排除 原始音频.mp3）。"""
    for f in sorted(os.listdir(folder)):
        if f.endswith(".mp3") and "原始音频" not in f:
            return folder / f
    return None


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
        duration = round(mp3.stat().st_size / (1024 * 1024) / MAX_DURATION_MB_PER_MIN)
    return {"chars": chars, "duration": duration}


def _source_label(url):
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
    return _SOURCE_LABELS.get(host, host)


def _read_source(name):
    """从 来源.md 读取 (url, label)。找不到返回 (None, None)。"""
    src = CONTENT_DIR / name / "来源.md"
    if not src.exists():
        return None, None
    text = src.read_text(encoding="utf-8")
    m = re.search(r"- (?:链接|转录来源)：(\S+)", text)
    if not m:
        return None, None
    url = m.group(1).strip()
    return url, _source_label(url)


def _source_cell(name):
    url, label = _read_source(name)
    return f"[{label}]({url})" if url else ""


# ── 台账 ──────────────────────────────────────────────────────────

def _display_title(name):
    """从 site.json 查该期精修标题（带标点）；查不到回退文件夹名。

    台账「播客」列、首页卡片、每期页 hero 都用它，保证全站标题一致。
    """
    site_json_path = SITE_DIR / "site.json"
    try:
        for e in json.loads(site_json_path.read_text(encoding="utf-8")):
            if e.get("folder") == name:
                return e.get("title") or name
    except Exception:
        pass
    return name


def add_to_catalog(name):
    folder = CONTENT_DIR / name
    if not folder.is_dir():
        sys.exit(f"[错误] 找不到播客目录: {folder}")
    if not _find_briefing(folder):
        sys.exit(f"[错误] 找不到讲稿: {folder}")

    stats = episode_stats(name)
    title = _display_title(name)
    url, _ = _read_source(name)
    source_cell = _source_cell(name)
    body = f"{title} | {source_cell} | {stats['chars']//1000}K字 | {stats['duration']}min |"

    text = CATALOG.read_text(encoding="utf-8") if CATALOG.exists() else ""
    if not text.strip():
        text = CATALOG_HEADER

    # 定位已有行：优先按转录来源 URL（唯一），其次按文件夹名（兼容旧行）
    m = None
    if url:
        m = re.search(r"^.*" + re.escape(url) + r".*$", text, re.M)
    if not m:
        m = re.search(r"^(\|\s*\d+\s*\|\s*)" + re.escape(name) + r"\s*\|.*$", text, re.M)

    if m:
        line = m.group(0)
        # 保留行号；来源单元格若新值缺失则沿用旧值
        fields = [f.strip() for f in line.strip().strip("|").split("|")]
        existing_source = fields[2] if len(fields) >= 3 else ""
        source_cell = source_cell or existing_source
        body = f"{title} | {source_cell} | {stats['chars']//1000}K字 | {stats['duration']}min |"
        num_m = re.match(r"^\|\s*(\d+)\s*\|", line)
        row = f"| {num_m.group(1)} | {body}" if num_m else f"| {body}"
        lines = text.splitlines(keepends=True)
        for i, ln in enumerate(lines):
            if ln.rstrip("\n") == line.rstrip("\n"):
                lines[i] = row + "\n"
                break
        text = "".join(lines)
        print(f"[台账] 已更新 {name} 的行")
    else:
        nums = [int(n) for n in re.findall(r"^\|\s*(\d+)\s*\|", text, re.M)]
        num = (max(nums) + 1) if nums else 1
        text = text.rstrip() + "\n" + f"| {num} | " + body + "\n"
        print(f"[台账] 已追加 {title} → 第 {num} 行")

    CATALOG.write_text(text, encoding="utf-8")
    print(f"  {stats['chars']//1000}K字, {stats['duration']}min")


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
    return {
        "title": prev.get("title", name),
        "folder": name,
        "duration": stats["duration"],
        "words": stats["chars"],
        "source_name": prev.get("source_name") or label or "",
        "source_url": prev.get("source_url") or url or "",
    }


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

    names = _episode_dirs()
    if only and only not in names:
        sys.exit(f"[错误] 找不到播客目录（或没有讲稿）: {only}")

    # 拷贝 content.html（--only 只影响这一步）；音频由 R2 公开 URL 提供，不放进 site/
    for name in names:
        if only and name != only:
            continue
        folder = CONTENT_DIR / name
        html = folder / f"{name} - content.html"
        if html.exists():
            (SITE_DIR / name).mkdir(parents=True, exist_ok=True)
            shutil.copy2(html, SITE_DIR / name / "content.html")

    # 重建 site.json（始终覆盖全部期）
    eps, seen = [], set()
    for name in existing.keys():
        if name in names:
            eps.append(_build_entry(name, existing[name]))
            seen.add(name)
    for name in names:
        if name not in seen:
            eps.append(_build_entry(name, {}))

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    site_json_path.write_text(
        json.dumps(eps, ensure_ascii=False, indent=2), encoding="utf-8")
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
    stats = (f'    <span>收录 {len(eps)} 期</span>\n'
             f'    <span>总计 {total_min} 分钟</span>\n'
             f'    <span>约 {total_words // 1000}K 字</span>')

    # 卡片
    cards = []
    for e in eps:
        href = quote(e["folder"] + "/content.html", safe=",/+=")
        cards.append(
            f'    <div class="card" tabindex="0" role="link" data-href="{href}" '
            f'onclick="window.location=\'{href}\'">\n'
            f'        <div class="card-num">EPISODE</div>\n'
            f'        <h3 class="card-title">{_esc(e["title"])}</h3>\n'
            f'        <div class="card-meta">\n'
            f'            <span class="dur">{e["duration"]}min</span>\n'
            f'            <span class="words">{e["words"] // 1000}K字</span>\n'
            f'            <a href="{_esc(e.get("source_url", ""))}" target="_blank" '
            f'class="source" onclick="event.stopPropagation()">'
            f'{_esc(e.get("source_name", ""))}</a>\n'
            f'        </div>\n'
            f'    </div>')
    cards_html = "\n".join(cards)

    text = index_path.read_text(encoding="utf-8")
    text = _replace_between(text, "<!-- STATS:START -->", "<!-- STATS:END -->", stats)
    text = _replace_between(text, "<!-- CARDS:START -->", "<!-- CARDS:END -->", cards_html)
    index_path.write_text(text, encoding="utf-8")
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
        src_path.write_text(
            f"# 来源信息\n\n## 原始播客\n- 标题：{name}\n\n## 转录来源\n"
            f"- 链接：{url}\n- 转录来源：{url}\n\n## 处理信息\n"
            f"- 处理日期：{d}\n",
            encoding="utf-8")
        written += 1
        print(f"[来源] 已回填 {name}")
    print(f"[来源] 回填 {written} 期，跳过 {skipped} 期")


# ── 一键收尾 ──────────────────────────────────────────────────────

def _run(cmd, cwd, dry_run=False):
    if dry_run:
        print("  [dry-run] " + " ".join(cmd))
        return True
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
        out = (r.stdout or "").strip()
        if out:
            print(out[-2000:])
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            print(f"[错误] 命令失败 (exit {r.returncode})，已跳过")
            if err:
                print(err[-1200:])
            return False
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[错误] {type(exc).__name__}: {exc}，已跳过")
        return False


def finish(name, dry_run=False):
    """一键收尾：台账 → site → 首页 → R2 上传 → Pages 部署。

    前置：已生成音频、已清理中间文件（转录_纠错.txt / audio/）。
    """
    print(f"=== 一键收尾: {name} ===")
    add_to_catalog(name)
    sync_site(only=name)
    gen_index()

    mp3 = _gen_mp3(CONTENT_DIR / name)
    if not mp3:
        print("[R2] 跳过：找不到生成的 mp3")
    else:
        print(f"[R2] 上传 {mp3.name} 到 R2 ...")
        # 对象键不带桶名前缀；R2 公开 URL 据此提供流式音频
        _run(["npx", "wrangler", "r2", "object", "put",
              f"{name}/{name}.mp3",
              "--file", str(mp3), "--ct", "audio/mpeg"],
             cwd=BASE_DIR, dry_run=dry_run)

    print("[部署] Pages 部署 ...")
    _run(["npx", "wrangler", "pages", "deploy", ".",
          "--project-name", "podcast-scripts", "--branch", "main"],
         cwd=SITE_DIR, dry_run=dry_run)
    print("[完成] 台账、site、首页、R2、Pages 已处理")


# ── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="播客台账与站点清单维护（CLAUDE.md 第 6/7 步脚本化）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stats = sub.add_parser("stats", help="打印某期字数/时长，不写入")
    p_stats.add_argument("name")

    p_add = sub.add_parser("add", help="追加/更新某期到 播客目录.md")
    p_add.add_argument("name")

    p_site = sub.add_parser("sync-site", help="同步 content.html + 重建 site.json")
    p_site.add_argument("--only", default=None, help="只同步某期")

    p_index = sub.add_parser("gen-index", help="从 site.json 重建首页 index.html")
    p_index.add_argument("--name", default=None, help="仅重新生成某期卡片（占位，全部重建）")

    p_backfill = sub.add_parser("backfill-sources", help="为缺 来源.md 的期回填来源")

    p_finish = sub.add_parser("finish", help="一键收尾：台账+site+首页+R2+部署")
    p_finish.add_argument("name")
    p_finish.add_argument("--dry-run", action="store_true", help="只打印命令不执行（wrangler 部分）")

    args = parser.parse_args()

    if args.cmd == "stats":
        s = episode_stats(args.name)
        print(f"{args.name}: {s['chars']//1000}K字, {s['duration']}min")
    elif args.cmd == "add":
        add_to_catalog(args.name)
    elif args.cmd == "sync-site":
        sync_site(args.only)
    elif args.cmd == "gen-index":
        gen_index()
    elif args.cmd == "backfill-sources":
        backfill_sources()
    elif args.cmd == "finish":
        finish(args.name, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
