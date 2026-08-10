"""Episode metadata manifest and legacy migration helpers."""
import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlsplit


EPISODE_SCHEMA_VERSION = 1
MANIFEST_NAME = "episode.json"


def _source_label(url):
    host = (urlsplit(url).hostname or "").lower()
    labels = {
        "podcasts.happyscribe.com": "HappyScribe",
        "happyscribe.com": "HappyScribe",
        "nav.al": "nav.al",
        "singjupost.com": "SingjuPost",
        "podscripts.co": "podscripts.co",
    }
    return labels.get(host, host)


def _legacy_source(folder):
    path = Path(folder) / "来源.md"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")

    def value(label):
        match = re.search(
            rf"^- {re.escape(label)}：(.+)$", text, re.MULTILINE)
        return match.group(1).strip() if match else ""

    url = value("链接") or value("转录来源")
    return {
        "display_title": value("标题"),
        "source": {
            "url": url,
            "label": _source_label(url) if url else "",
            "kind": value("转录方式"),
            "extractor": value("提取器"),
            "transcript_status": value("转录质量") or "未标注",
        },
        "content_review_status": value("内容审查"),
    }


def stable_slug(value, identity, max_base_length=72):
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    base = base[:max_base_length].rstrip("-") or "episode"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{suffix}"


def _identity(folder, source_url):
    return source_url or Path(folder).name


def _default_manifest(folder, display_title="", source_url=""):
    folder = Path(folder)
    legacy = _legacy_source(folder)
    source = legacy.get("source", {})
    source_url = source_url or source.get("url", "")
    display_title = (
        display_title
        or legacy.get("display_title")
        or folder.name
    )
    identity = _identity(folder, source_url)
    episode_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    slug = stable_slug(display_title, identity)
    existing_audio = any(
        path.suffix.lower() == ".mp3" and "原始音频" not in path.name
        for path in folder.iterdir()
        if path.is_file()
    )
    audio_key = (
        f"{folder.name}/{folder.name}.mp3"
        if existing_audio
        else f"{slug}/audio.mp3"
    )
    transcript_status = source.get("transcript_status") or "未标注"
    review_status = legacy.get("content_review_status") or ""
    if not review_status and "AI已审查（通过）" in transcript_status:
        review_status = "passed"
    return {
        "schema_version": EPISODE_SCHEMA_VERSION,
        "id": episode_id,
        "storage_name": folder.name,
        "slug": slug,
        "display_title": display_title,
        "source": {
            "url": source_url,
            "label": source.get("label") or (
                _source_label(source_url) if source_url else ""),
            "kind": source.get("kind", ""),
            "extractor": source.get("extractor", ""),
        },
        "quality": {
            "mode": (
                "strict" if (folder / "content_map.json").exists()
                else "legacy"
            ),
            "claim_evidence_mode": (
                "precise_required"
                if (folder / "content_map.json").exists()
                else "legacy"
            ),
            "transcript_status": transcript_status,
            "correction_status": (
                "corrected" if (folder / "转录_纠错.txt").exists()
                else "not_required_or_pending"
            ),
            "content_review_status": review_status or "pending",
        },
        "publish": {
            "page_path": slug,
            "legacy_page_path": (
                folder.name if folder.name != slug else ""
            ),
            "audio_key": audio_key,
        },
    }


def load_episode(folder, create=False, display_title="", source_url=""):
    folder = Path(folder)
    path = folder / MANIFEST_NAME
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != EPISODE_SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 episode.json schema: {payload.get('schema_version')}")
        return payload
    if not create:
        return _default_manifest(folder, display_title, source_url)
    payload = _default_manifest(folder, display_title, source_url)
    save_episode(folder, payload)
    return payload


def save_episode(folder, payload):
    folder = Path(folder)
    payload = dict(payload)
    payload["schema_version"] = EPISODE_SCHEMA_VERSION
    payload["storage_name"] = folder.name
    path = folder / MANIFEST_NAME
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def ensure_episode(
        folder,
        display_title="",
        source_url="",
        source_kind="",
        extractor="",
        quality_mode="",
):
    folder = Path(folder)
    existed = (folder / MANIFEST_NAME).exists()
    payload = load_episode(
        folder, create=not existed,
        display_title=display_title,
        source_url=source_url,
    )
    changed = False
    if display_title and (
            not payload.get("display_title")
            or payload.get("display_title") == folder.name):
        payload["display_title"] = display_title
        changed = True
    source = payload.setdefault("source", {})
    for key, value in (
            ("url", source_url),
            ("label", _source_label(source_url) if source_url else ""),
            ("kind", source_kind),
            ("extractor", extractor)):
        if value and source.get(key) != value:
            source[key] = value
            changed = True
    quality = payload.setdefault("quality", {})
    mode = quality_mode or (
        "strict" if (folder / "content_map.json").exists()
        else quality.get("mode", "strict")
    )
    if quality.get("mode") != mode:
        quality["mode"] = mode
        changed = True
    if changed or not existed:
        save_episode(folder, payload)
    return payload


def display_title(folder):
    return load_episode(folder).get("display_title") or Path(folder).name


def source_metadata(folder):
    return load_episode(folder).get("source", {})


def quality_metadata(folder):
    return load_episode(folder).get("quality", {})


def page_path(folder):
    payload = load_episode(folder)
    return payload.get("publish", {}).get("page_path") or payload["slug"]


def legacy_page_path(folder):
    return load_episode(folder).get("publish", {}).get(
        "legacy_page_path", "")


def audio_key(folder):
    payload = load_episode(folder)
    return payload.get("publish", {}).get(
        "audio_key", f"{payload['slug']}/audio.mp3")


def public_audio_url(folder, base_url):
    return f"{base_url.rstrip('/')}/{quote(audio_key(folder), safe='/')}"


def update_review_status(folder, passed):
    folder = Path(folder)
    payload = load_episode(folder, create=True)
    payload.setdefault("quality", {})["content_review_status"] = (
        "passed" if passed else "failed")
    save_episode(folder, payload)

    source_path = folder / "来源.md"
    if source_path.exists():
        text = source_path.read_text(encoding="utf-8")
        value = "AI已审查（通过）" if passed else "AI审查未通过"
        if re.search(r"^- 内容审查：.*$", text, re.MULTILINE):
            text = re.sub(
                r"^- 内容审查：.*$",
                f"- 内容审查：{value}",
                text,
                flags=re.MULTILINE,
            )
        else:
            text = text.rstrip() + f"\n- 内容审查：{value}\n"
        source_path.write_text(text, encoding="utf-8")


def set_claim_evidence_mode(folder, mode):
    allowed = {"precise_required", "legacy_broad", "legacy"}
    if mode not in allowed:
        raise ValueError(f"无效 claim evidence mode: {mode}")
    folder = Path(folder)
    payload = load_episode(folder, create=True)
    payload.setdefault("quality", {})["claim_evidence_mode"] = mode
    save_episode(folder, payload)
    return payload


def migrate_folder(folder, display_title="", source_url=""):
    folder = Path(folder)
    payload = load_episode(
        folder,
        create=True,
        display_title=display_title,
        source_url=source_url,
    )
    if display_title and payload.get("display_title") != display_title:
        payload["display_title"] = display_title
        identity = _identity(
            folder, source_url or payload.get("source", {}).get("url", ""))
        payload["slug"] = stable_slug(display_title, identity)
        payload.setdefault("publish", {})["page_path"] = payload["slug"]
    if source_url and not payload.get("source", {}).get("url"):
        payload.setdefault("source", {})["url"] = source_url
        payload["source"]["label"] = _source_label(source_url)
    return save_episode(folder, payload)


def main():
    parser = argparse.ArgumentParser(description="单集 episode.json 元数据管理")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate", help="为目录创建 episode.json")
    migrate.add_argument("folder")
    migrate_all = sub.add_parser(
        "migrate-all", help="为 content 下所有单集创建 episode.json")
    migrate_all.add_argument("content_dir", nargs="?", default="content")
    evidence_mode = sub.add_parser(
        "set-evidence-mode", help="设置 claim evidence 兼容/严格模式")
    evidence_mode.add_argument("folder")
    evidence_mode.add_argument(
        "mode", choices=["precise_required", "legacy_broad", "legacy"])
    args = parser.parse_args()

    if args.command == "migrate":
        path = migrate_folder(args.folder)
        print(path)
        return 0
    if args.command == "set-evidence-mode":
        set_claim_evidence_mode(args.folder, args.mode)
        print(f"[episode] {args.folder}: claim_evidence_mode={args.mode}")
        return 0

    content_dir = Path(args.content_dir)
    site_entries = {}
    site_json = content_dir.parent / "site" / "site.json"
    if site_json.exists():
        site_entries = {
            entry.get("folder"): entry
            for entry in json.loads(
                site_json.read_text(encoding="utf-8"))
        }
    count = 0
    for folder in sorted(content_dir.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        existing = site_entries.get(folder.name, {})
        migrate_folder(
            folder,
            display_title=existing.get("title", ""),
            source_url=existing.get("source_url", ""),
        )
        count += 1
    print(f"[episode] migrated={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
