#!/usr/bin/env bash
# ==============================================================
# 重新处理播客 1-5 流水线脚本
# 已在沙箱中完成：旧音频/文稿清理
# ==============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
PY=".venv/bin/python"
CONTENT="content"

log()   { echo "[$(date '+%H:%M:%S')] $*"; }

# 播客 1
log "=== 1/5: Modern Wisdom with Alex Hormozi ==="
$PY scripts/process.py \
  --transcript "$CONTENT/Modern Wisdom with Alex Hormozi - 33 Brutal Truths/原始转录.txt" \
  --name "Modern Wisdom with Alex Hormozi - 33 Brutal Truths"

# 播客 2
log "=== 2/5: Naval - The AI Industrial Revolution ==="
$PY scripts/process.py \
  --transcript "$CONTENT/Naval - The AI Industrial Revolution/原始转录.txt" \
  --name "Naval - The AI Industrial Revolution"

# 播客 3
log "=== 3/5: Naval Roundtable - AI Scaling Laws ==="
$PY scripts/process.py \
  --transcript "$CONTENT/Naval Roundtable - AI Scaling Laws/原始转录.txt" \
  --name "Naval Roundtable - AI Scaling Laws"

# 播客 4
log "=== 4/5: All-In Podcast - AI Sovereignty Wars ==="
$PY scripts/process.py \
  --transcript "$CONTENT/All-In Podcast - AI Sovereignty Wars/原始转录.txt" \
  --name "All-In Podcast - AI Sovereignty Wars"

# 播客 5
log "=== 5/5: All-In Podcast - More Trillion Dollar IPOs ==="
$PY scripts/process.py \
  --transcript "$CONTENT/All-In Podcast - More Trillion Dollar IPOs/原始转录.txt" \
  --name "All-In Podcast - More Trillion Dollar IPOs"

log "=== 全部完成 ==="
