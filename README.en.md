[中文](README.md) | [English](README.en.md)

# Podcast Pipeline

Turn English podcasts into auditable Chinese notes, Chinese narration scripts, TTS audio, and mobile reader pages, then publish the complete release to Cloudflare Pages + R2.

## Canonical commands

```bash
# Process one episode
.venv/bin/python scripts/process.py "https://transcript-page"

# Process local audio
.venv/bin/python scripts/process.py "episode.mp3" --name "Episode name" --asr-quality max

# Complete publication
.venv/bin/python scripts/catalog.py finish "Episode name"
.venv/bin/python scripts/catalog.py finish-batch "Episode one" "Episode two"
```

`finish-batch` always performs the complete transaction: quality checks, R2 upload, site generation, Pages deployment, and remote verification. There is no R2-only publication path.

## Public and private directories

| Path | Purpose | Public GitHub repository |
|---|---|---|
| `scripts/` | Pipeline implementation | Yes |
| `tests/`, `.github/` | Tests and CI | Yes |
| `examples/` | Sanitized reader-page and script examples | Yes |
| `docs/module-map.md`, `docs/pipeline.md` | Module navigation and detailed architecture | Yes |
| `benchmarks/` | Contracts, manual references, durable reports | Yes, except downloaded media and generated runs |
| `site/deploy.sh`, `site/wrangler.toml` | Cloudflare deployment configuration | Yes |
| `requirements*.txt`, `.env.example` | Dependencies and secret-free configuration template | Yes |
| `content/` | Transcripts, corrections, notes, scripts, reviews, and audio | **No** |
| Other files under `site/` | Generated index, episode pages, and catalog | **No** |
| `reports/`, `.runlogs/` | Local reports and logs | **No** |
| `.env`, `.wrangler/`, `.venv*/`, local agent state | Secrets, credentials, and machine state | **No** |

The boundary is enforced by `.gitignore`, `scripts/check_public_repo.py`, and CI. Before a public push, run:

```bash
.venv/bin/python scripts/check_public_repo.py
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Branches prefixed with `private-` or `private/` may contain private episode data in Git history. The checker refuses to treat them as public branches, and also fails closed for a detached HEAD whose branch cannot be established from local or GitHub Actions context. `--allow-private-branch` and `--allow-detached-head` inspect only the current index and do not prove that history is sanitized.

## Documentation

- `AGENTS.md`: mandatory routing, privacy, and Git rules for coding agents.
- `CLAUDE.md`: complete operating guide, quality gates, and recovery procedures.
- `docs/module-map.md`: maintainer-oriented stage owners and artifact dependencies.
- `docs/pipeline.md`: detailed internal contracts and data flow.
- `tests/README.md`: CI-equivalent validation and domain test index.

Inspect the stage map with `python scripts/pipeline_map.py` or
`python scripts/pipeline_map.py --json`.

For normal work, use `process.py` rather than manually chaining internal CLIs such as `tts.py`, `html_gen.py`, or `ai_review.py`.
