#!/bin/bash
set -e

# ===== 播客站点部署到 Cloudflare Pages + R2 =====
# 使用方式：
#   1. 先执行 wrangler login（OAuth 登录）
#   2. 运行 bash deploy.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTENT_DIR="${PODCAST_DIR:-$REPO_ROOT/content}"
PROJECT="${PAGES_PROJECT:-your-pages-project}"
R2_BUCKET="${R2_BUCKET:-your-r2-bucket}"

echo "=== 1/3: 创建 R2 桶（如已存在则跳过）==="
npx wrangler r2 bucket create $R2_BUCKET --no-insights || true

echo "=== 2/3: 上传音频到 R2 ==="
# 只上传每期生成的 {播客名}.mp3（跳过 原始音频.mp3）。
# Wrangler positional 参数使用 bucket/key，桶内实际 key 不含桶名。
while IFS= read -r -d '' mp3_path; do
    folder=$(basename "$(dirname "$mp3_path")")
    filename=$(basename "$mp3_path")
    echo "  Uploading $filename..."
    npx wrangler r2 object put "${R2_BUCKET}/${folder}/${filename}" \
      --file "$mp3_path" --content-type audio/mpeg --remote
done < <(find "$CONTENT_DIR" -name "*.mp3" \
  -not -path "*/audio/*" -not -name "*原始音频*" -print0)

echo "=== 3/3: 部署 Pages ==="
npx wrangler pages deploy . --project-name $PROJECT --branch main

echo ""
echo "✅ 部署完成！"
echo "Pages URL: https://${PROJECT}.pages.dev"
echo "R2 音频桶公开 URL: $(npx wrangler r2 bucket dev-url get $R2_BUCKET 2>/dev/null | grep -oE 'https://[^ ]+\.r2\.dev' || echo '未开启（wrangler r2 bucket dev-url enable $R2_BUCKET）')"
