#!/bin/bash
set -e

# ===== 播客站点部署到 Cloudflare Pages + R2 =====
# 使用方式：
#   1. 先执行 wrangler login（OAuth 登录）
#   2. 运行 bash deploy.sh

SRC="."
PROJECT="podcast-scripts"
R2_BUCKET="podcast-audio"
# 内容目录：默认与本仓库同级的 ../content（按 CLAUDE.md 布局），可用环境变量覆盖
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTENT_DIR="${PODCAST_CONTENT_DIR:-$REPO_ROOT/content}"

echo "=== 1/3: 创建 R2 桶（如已存在则跳过）==="
npx wrangler r2 bucket create $R2_BUCKET --no-insights || true

echo "=== 2/3: 上传音频到 R2 ==="
# 只上传每期生成的 {播客名}.mp3（跳过 原始音频.mp3），对象键不带桶名前缀
if [ ! -d "$CONTENT_DIR" ]; then
    echo "  跳过：内容目录不存在 ($CONTENT_DIR)。设置 PODCAST_CONTENT_DIR 或先运行流水线生成内容。"
else
    for mp3_path in $(find "$CONTENT_DIR" -name "*.mp3" -not -path "*/audio/*" -not -name "*原始音频*"); do
        folder=$(basename "$(dirname "$mp3_path")")
        filename=$(basename "$mp3_path")
        echo "  Uploading $filename..."
        npx wrangler r2 object put "${folder}/${filename}" --file "$mp3_path" --ct audio/mpeg || echo "  FAILED: $filename (skip)"
    done
fi

echo "=== 3/3: 部署 Pages ==="
npx wrangler pages deploy . --project-name $PROJECT --branch main

echo ""
echo "✅ 部署完成！"
echo "Pages URL: https://${PROJECT}.pages.dev"
echo "R2 音频桶公开 URL: $(npx wrangler r2 bucket dev-url get $R2_BUCKET 2>/dev/null | grep -oE 'https://[^ ]+\.r2\.dev' || echo '未开启（wrangler r2 bucket dev-url enable $R2_BUCKET）')"
