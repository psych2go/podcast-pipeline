"""Episode metadata manifest and legacy migration helpers."""
import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

try:
    from atomic_io import atomic_write_json, atomic_write_text
    from evidence import (
        ASR_SOURCE_KINDS,
        effective_source_kind,
        migrate_evidence_provenance,
    )
    from sources import source_label
except ImportError:
    from scripts.atomic_io import atomic_write_json, atomic_write_text
    from scripts.evidence import (
        ASR_SOURCE_KINDS,
        effective_source_kind,
        migrate_evidence_provenance,
    )
    from scripts.sources import source_label


EPISODE_SCHEMA_VERSION = 1
MANIFEST_NAME = "episode.json"


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
            "label": source_label(url) if url else "",
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
    identity = _normalize_identity(identity)
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{suffix}"


def _normalize_identity(identity):
    parsed = urlsplit(identity or "")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return identity
    netloc = parsed.netloc.lower()
    if netloc.endswith(":80") or netloc.endswith(":443"):
        netloc = netloc.rsplit(":", 1)[0]
    return urlunsplit((
        "https",
        netloc,
        parsed.path or "/",
        parsed.query,
        "",
    ))


def _identity(folder, source_url):
    return _normalize_identity(source_url) if source_url else Path(folder).name


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
                source_label(source_url) if source_url else ""),
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
    atomic_write_json(path, payload)
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
    raw_path = folder / "transcript.raw.json"
    if raw_path.exists():
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            source_kind = effective_source_kind(folder, raw)
        except (OSError, json.JSONDecodeError):
            pass
    for key, value in (
            ("url", source_url),
            ("label", source_label(source_url) if source_url else ""),
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


def _review_status(folder):
    review_path = Path(folder) / "ai_review.json"
    if not review_path.exists():
        return "pending"
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "stale"
    if not review.get("passed", False):
        return "failed"
    for name, expected in review.get("reviewed_files", {}).items():
        path = Path(folder) / name
        if not path.exists():
            return "stale"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            return "stale"
    return "passed"


def inspect_episode_state(folder, payload=None):
    """Derive mutable quality state from evidence and current artifacts."""
    folder = Path(folder)
    payload = payload or load_episode(folder)
    raw = {}
    raw_path = folder / "transcript.raw.json"
    if raw_path.exists():
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    source_kind = effective_source_kind(folder, raw)
    corrected = (folder / "转录_纠错.txt").exists()
    stored_quality = payload.get("quality", {})

    if source_kind in ASR_SOURCE_KINDS:
        transcript_status = (
            "已纠错（ASR）" if corrected else "待纠错（ASR）")
        correction_status = (
            "corrected" if corrected else "required_missing")
    elif corrected:
        transcript_status = "已纠错"
        correction_status = "corrected"
    else:
        transcript_status = stored_quality.get(
            "transcript_status", "未标注")
        correction_status = stored_quality.get(
            "correction_status", "not_required_or_pending")

    return {
        "source_kind": source_kind,
        "mode": (
            "strict" if (folder / "content_map.json").exists()
            else stored_quality.get("mode", "legacy")
        ),
        "claim_evidence_mode": stored_quality.get(
            "claim_evidence_mode",
            "precise_required"
            if (folder / "content_map.json").exists() else "legacy",
        ),
        "transcript_status": transcript_status,
        "correction_status": correction_status,
        "content_review_status": _review_status(folder),
    }


def _existing_processing_date(folder):
    path = Path(folder) / "来源.md"
    if not path.exists():
        return ""
    match = re.search(
        r"^- 处理日期：(.+)$",
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def _source_method(kind):
    return {
        "local_asr": "本地 ASR",
        "legacy_asr": "历史本地 ASR",
        "web_transcript": "网页转录抓取",
        "third_party_transcript": "第三方转录",
        "local_transcript": "本地转录导入",
    }.get(kind, kind or "未标注")


def render_source_markdown(folder, payload=None, state=None, processing_date=""):
    """Render 来源.md from structured episode and evidence metadata."""
    folder = Path(folder)
    payload = payload or load_episode(folder)
    state = state or inspect_episode_state(folder, payload)
    source = payload.get("source", {})
    raw = {}
    raw_path = folder / "transcript.raw.json"
    if raw_path.exists():
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    provenance = raw.get("provenance", {})
    asr = provenance.get("asr", {}) if isinstance(
        provenance, dict) else {}
    audio = provenance.get("original_audio", {}) if isinstance(
        provenance, dict) else {}
    processing_date = (
        processing_date or _existing_processing_date(folder) or "未知")

    lines = [
        "# 来源信息",
        "",
        "## 原始播客",
        f"- 标题：{payload.get('display_title') or folder.name}",
    ]
    if source.get("url"):
        lines.append(f"- 链接：{source['url']}")
    lines.extend([
        "",
        "## 转录证据",
        f"- 转录方式：{_source_method(state['source_kind'])}",
    ])
    if raw.get("source"):
        lines.append(f"- 输入来源：{raw['source']}")
    if audio.get("file"):
        lines.append(f"- 原始音频：{audio['file']}")
    if audio.get("sha256"):
        lines.append(f"- 原始音频 SHA-256：{audio['sha256']}")
    if state["source_kind"] in ASR_SOURCE_KINDS:
        lines.append(f"- ASR 引擎：{asr.get('engine') or 'unknown'}")
        lines.append(f"- ASR 模型：{asr.get('model') or 'unknown'}")
        lines.append(f"- ASR 质量：{asr.get('quality') or 'unknown'}")
        lines.append(
            "- 说话人分离："
            + ("已启用" if asr.get("diarization") else "未启用或未知")
        )
    elif source.get("extractor"):
        lines.append(f"- 提取器：{source['extractor']}")
    lines.extend([
        "",
        "## 处理信息",
        f"- 处理日期：{processing_date}",
        "- pipeline 版本：v7",
        f"- 转录质量：{state['transcript_status']}",
        f"- 纠错状态：{state['correction_status']}",
        "- 内容审查：" + {
            "passed": "AI已审查（通过）",
            "failed": "AI审查未通过",
            "stale": "AI审查已过期",
        }.get(state["content_review_status"], "待审查"),
    ])
    return "\n".join(lines) + "\n"


def sync_episode_state(folder, processing_date=""):
    """Persist derived state and regenerate 来源.md from structured evidence."""
    folder = Path(folder)
    payload = load_episode(folder, create=True)
    state = inspect_episode_state(folder, payload)
    current_source_kind = payload.get("source", {}).get("kind")
    current_quality = payload.get("quality", {})
    state_changed = (
        current_source_kind != state["source_kind"]
        or any(
            current_quality.get(key) != state[key]
            for key in (
                "mode", "claim_evidence_mode", "transcript_status",
                "correction_status")
        )
    )
    candidate_source = render_source_markdown(
        folder,
        payload=payload,
        state=state,
        processing_date=processing_date,
    )
    source_path = folder / "来源.md"
    source_changed = (
        not source_path.exists()
        or source_path.read_text(encoding="utf-8") != candidate_source
    )
    if (
            (state_changed or source_changed)
            and (folder / "ai_review.json").exists()):
        state["content_review_status"] = "stale"
    payload.setdefault("source", {})["kind"] = state["source_kind"]
    quality = payload.setdefault("quality", {})
    for key in (
            "mode", "claim_evidence_mode", "transcript_status",
            "correction_status", "content_review_status"):
        quality[key] = state[key]
    save_episode(folder, payload)
    atomic_write_text(
        source_path,
        render_source_markdown(
            folder,
            payload=payload,
            state=state,
            processing_date=processing_date,
        ),
    )
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
    fallback = audio_key(folder)
    try:
        from release import active_audio_key
    except ImportError:
        from scripts.release import active_audio_key
    key = active_audio_key(folder, fallback)
    return f"{base_url.rstrip('/')}/{quote(key, safe='/')}"


def update_review_status(folder, passed):
    folder = Path(folder)
    payload = load_episode(folder, create=True)
    payload.setdefault("quality", {})["content_review_status"] = (
        "passed" if passed else "failed")
    save_episode(folder, payload)
    state = inspect_episode_state(folder, payload)
    state["content_review_status"] = "passed" if passed else "failed"
    atomic_write_text(
        folder / "来源.md",
        render_source_markdown(folder, payload=payload, state=state),
    )


def update_transcript_status(folder, transcript_status, correction_status):
    """Persist the reviewed transcript and correction state."""
    folder = Path(folder)
    payload = load_episode(folder, create=True)
    quality = payload.setdefault("quality", {})
    quality["transcript_status"] = transcript_status
    quality["correction_status"] = correction_status
    save_episode(folder, payload)
    state = inspect_episode_state(folder, payload)
    state["transcript_status"] = transcript_status
    state["correction_status"] = correction_status
    state["content_review_status"] = quality.get(
        "content_review_status", "pending")
    atomic_write_text(
        folder / "来源.md",
        render_source_markdown(folder, payload=payload, state=state),
    )


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
        payload["source"]["label"] = source_label(source_url)
    save_episode(folder, payload)
    if (folder / "transcript.raw.json").exists():
        migrate_evidence_provenance(folder)
        sync_episode_state(folder)
    return folder / MANIFEST_NAME


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
