**English** | [中文](./README.md)

# Podcast Pipeline · English Podcast → Chinese Script

> Turn English podcasts into Chinese scripts, producing a **TTS Chinese audio track** and a **self-contained HTML reader page**, deployed to Cloudflare (Pages + R2).

An end-to-end pipeline: **fetch → transcribe → correct → script → narrate → render → publish**. Scripts are written by Claude following built-in prompts, narration via Fish Audio TTS, and the reader page is a designed, warm "book-paper" layout with an embedded audio player, collapsible TOC, and chapter numbering.

> 📌 This repo contains **methodology only** (scripts, prompts, docs, deploy config). The actual episode content (transcripts, scripts, MP3s) is private and **not in the repo** — you generate it locally under `content/` by running the pipeline.

## Preview

Want to see what the reader page looks like? Open the example in a browser:

👉 **[`examples/reader-page-example.html`](./examples/reader-page-example.html)** — download and open in a browser. Includes the full audio player, collapsible TOC, and chapter numbering.

Input format reference: [`examples/sample-script.md`](./examples/sample-script.md) — a sample script with intro + three chapters that renders into the page above.

## Features

- 🎙️ **Multi-source fetch**: web transcripts (incl. JS-rendered and anti-scrape sites), RSS, local MP3 (ASR)
- 📝 **Tiered correction**: correction policy decided by source type — forced for local ASR, skipped for official subtitles
- ✍️ **Scripts by Claude**: follows `讲稿提示词.md` (intro paragraph, 400–900 char chapters, 3-tier speaker attribution, missing-points checklist)
- 🔊 **Fish Audio TTS**: sliced by `## ` headings, sentence-boundary segmentation, inter-chapter silence, resumable
- 📖 **Self-contained reader page**: warm design, built-in player (play / speed / volume / download), collapsible TOC, auto chapter numbering
- ☁️ **Cloudflare deploy**: Pages hosts the site, R2 streams audio (Range-supported seeking)
- 🤖 **One-command finish**: `catalog.py finish` runs catalog + site + index + R2 upload + deploy

## Pipeline

```
English podcast URL / MP3
        │
        ▼  process.py (fetch / ASR)
raw transcript
        │
        ▼  correction gate (tiered by source)
corrected transcript
        │
        ▼  write script (Claude, follows 讲稿提示词.md)
script.md  ──▶ validator.py (intro / chapter size / format checks)
        │
        ▼  tts.py (Fish Audio, sliced by ## headings)
{name}.mp3
        │
        ▼  html_gen.py (script → reader page with player)
content.html
        │
        ▼  catalog.py (catalog + site.json + index) → R2 audio → Pages deploy
live site
```

## Quick Start

Full walkthrough in [`CLAUDE.md`](./CLAUDE.md). In short:

1. **Fetch** — `python scripts/process.py "https://transcript-url"`
2. **Correct** — decide by source type (forced for ASR, skip for official subtitles)
3. **Write script** — Claude follows `scripts/讲稿提示词.md`
4. **TTS + HTML** — `python scripts/process.py --name "name" --tts-only`
5. **Publish** — `python scripts/catalog.py finish "name"` (catalog + site + R2 + deploy)

## Setup

```bash
cp .env.example .env   # fill in FISH_KEY, optionally HF_TOKEN, R2_PUBLIC_URL
pip install -r requirements.txt
```

| Variable | Purpose | Required |
|----------|---------|----------|
| `FISH_KEY` | Fish Audio API key (TTS) | ✅ |
| `FISH_VOICE` | Fish Audio voice ID | ✅ |
| `HF_TOKEN` | HuggingFace token (speaker diarization) | – |
| `R2_PUBLIC_URL` | R2 public bucket URL (audio streaming) | – |

Cloudflare deploy needs `wrangler` (`npx wrangler login`, OAuth — no API token required).

## Tech Stack

httpx + curl_cffi · Parakeet/Whisper ASR · pyannote diarization · Fish Audio TTS · Claude for scripting · vanilla HTML/CSS/JS reader page · Cloudflare Pages + R2

## License

MIT — see [`LICENSE`](./LICENSE).
