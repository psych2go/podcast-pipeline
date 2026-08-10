# Podcast Pipeline v7

把英文播客转成可审计的中文完整笔记、中文讲稿、TTS 音频和移动端阅读页，并发布到 Cloudflare Pages + R2。

## 标准流程

### 1. 抓取

```bash
.venv/bin/python scripts/process.py "https://转录页面"
```

URL 会自动提取展示标题并创建 `content/<storage_name>/`。Podscripts 页面使用时间戳转录提取器，输出：

- `episode.json`：稳定 ID、展示标题、slug、来源、质量状态和发布路径
- `来源.md`：可读来源记录
- `原始转录.txt`：不可改写的原始证据
- `transcript.raw.json`：带稳定 `S0001` segment ID 的结构化转录

本地音频需要显式名称：

```bash
.venv/bin/python scripts/process.py "episode.mp3" --name "播客名" --asr-quality max
```

### 2. 纠错和证据台账

按 `scripts/纠错提示词.md` 检查转录。第三方转录也要抽查人名、公司、数字和说话人；需要纠错时写 `转录_纠错.txt`，但不覆盖原始证据。

新一期必须建立 evidence v3 台账：

```bash
.venv/bin/python scripts/content_map.py init \
  "content/播客名/transcript.raw.json" \
  "content/播客名/content_map.json" \
  --title "展示标题"
```

`content_map.json` 的每个 unit/claim 必须绑定转录 segment ID 和源文本
SHA-256。多 claim 单元不得给所有 claim 复制整个 unit 的 segment 集合；
每条 claim 还要记录证据置信度和选择理由。

### 3. 写中文内容

按 `scripts/讲稿提示词.md`：

1. 先写 `中文完整笔记.md`。
2. 再写适合收听的 `讲书稿.md`。
3. 写 `summary_map.json`，绑定章节正文哈希、unit/claim ID、笔记 claim ID 和笔记正文哈希。
4. 专有名词需要控制读音时添加 `tts_lexicon.json`，格式为 `{"原词": "朗读文本"}`。

`enrich-evidence` 只刷新 unit 证据和已有精确 claim 证据，不会再猜测多
claim 单元的映射。历史数据需要按 unit 精炼：

```bash
.venv/bin/python scripts/content_map.py enrich-evidence "content/播客名"
.venv/bin/python scripts/claim_evidence.py "content/播客名" --unit U0001
```

已经发布且有成功 `publish_report.json` 的 evidence v2 单集可暂时兼容，
但质量报告会持续告警；新单集不能使用 v2。

### 4. AI 审查和确定性质量门

```bash
.venv/bin/python scripts/ai_review.py "content/播客名" --model opus --effort max
.venv/bin/python scripts/quality_report.py "content/播客名"
```

以下任一情况都会阻断 TTS：

- 缺 `episode.json`、结构化转录、evidence v3、完整笔记或讲稿
- segment/claim/章节/笔记哈希不匹配
- high/medium claim 未覆盖
- AI 审查过期或失败
- 转录质量、覆盖率、事实性任一低于 90
- 数字、归因、TTS 可读性或稿件内容发布状态失败
- 结构体检存在错误

AI 只审内容，HTML、MP3、R2 和 Pages 的新鲜度由后续确定性检查负责。修改任一受审文件后必须重审。

### 5. TTS 和 HTML

```bash
.venv/bin/python scripts/process.py --name "播客名" --tts-only
```

TTS 使用内容与配置指纹缓存，而不是文件时间。`tts_manifest.json` 绑定实际朗读文本、音色、模型、语速、章节设置、每节音频 SHA-256 和最终 MP3 SHA-256。

- 任一章节失败会立即阻断。
- 失败时不会合并，也不会覆盖已有最终 MP3。
- 成功后先生成临时合并文件，再原子替换最终 MP3。
- HTML 只会在质量门和 TTS 都通过后生成。

仅重建页面：

```bash
.venv/bin/python scripts/process.py --name "播客名" --html-only --skip-ai-review
```

旧期没有 `content_map.json` 时，必须显式使用：

```bash
.venv/bin/python scripts/process.py --name "旧期" --html-only \
  --skip-ai-review --allow-legacy-quality
```

`--allow-legacy-quality` 只用于已经存在于站点清单的历史内容，新内容不能借此进入站点。

### 6. 台账、站点和 Cloudflare

推荐一键收尾：

```bash
.venv/bin/python scripts/catalog.py finish "播客名" --dry-run
.venv/bin/python scripts/catalog.py finish "播客名"
```

`finish` 会依次：

1. 验证 MP3、HTML、严格质量报告和 TTS manifest。
2. 同步 slug 页面和旧路径兼容页，并全量重建 `site/site.json`。
3. 从当前单集统计全量重建 `content/播客目录.md`。
4. 校验台账、`site.json`、真实音频时长和讲稿字数完全一致，再重建首页。
5. 上传音频到 R2。
6. 部署 Cloudflare Pages。
7. 验证首页、单集最终 URL、标题、播放器、R2 `Content-Type`、文件大小、`Accept-Ranges` 和 `206` Range 响应。
8. 写入 `publish_report.json`；任何远端检查失败都会令命令失败。

分步命令：

```bash
.venv/bin/python scripts/catalog.py add "播客名"
.venv/bin/python scripts/catalog.py sync-site
.venv/bin/python scripts/catalog.py rebuild
.venv/bin/python scripts/catalog.py check
.venv/bin/python scripts/catalog.py gen-index
```

## 单集目录

```text
content/<storage_name>/
├── episode.json
├── 来源.md
├── 原始转录.txt
├── transcript.raw.json
├── 转录_纠错.txt             # 需要时
├── content_map.json
├── 中文完整笔记.md
├── 讲书稿.md
├── summary_map.json
├── tts_lexicon.json          # 可选
├── ai_review.json
├── quality_report.json
├── tts_manifest.json
├── run_report.json
├── <storage_name>.mp3
├── <storage_name> - content.html
└── publish_report.json
```

`storage_name` 只用于本地兼容；页面使用稳定 slug，展示标题独立存放在 `episode.json`。已有音频保留旧 R2 object key，避免迁移时破坏线上 URL；新单集默认使用 `<slug>/audio.mp3`。

不要删除审计文件。`run_report.json` 追加记录每次抓取、质量门、AI 审查、
TTS、HTML 和发布事务的耗时、失败、重试、调用量与模型成本字段。
`audio/` 分章节音频可在发布后清理，但保留它可以提高同配置重跑速度。

## 配置

`.env`：

```env
FISH_KEY=xxx
FISH_VOICE=xxx
FISH_MODEL=s2.1-pro-free
R2_PUBLIC_URL=https://pub-xxx.r2.dev
R2_BUCKET=podcast-audio
PAGES_PROJECT=podcast-scripts
PAGES_BASE_URL=https://podcast-scripts.pages.dev
HF_TOKEN=hf_xxx
```

Cloudflare 认证可使用 `wrangler login` 的 OAuth 凭据或环境中的 API Token；发布前以
`npx wrangler whoami` 确认当前账号和权限。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_pipeline.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_browser_layout.py' -v
```

浏览器测试需要：

```bash
.venv/bin/pip install -r requirements-browser.txt
.venv/bin/python -m playwright install --with-deps chromium
```

它在 320、375、430 像素宽度验证目录开关和播放器同处首行、互不重叠、
正文不横向溢出。浏览器或系统依赖缺失会令测试失败；GitHub Actions
使用 `.github/workflows/ci.yml` 安装 Chromium 系统依赖并强制执行该测试。

## 关键约束

- 原始证据不可覆盖；纠错稿是下游规范化副本。
- 新单集默认严格门禁，兼容模式必须显式开启。
- TTS 只读 `讲书稿.md`，lexicon 只改变读音，不改变 HTML。
- 移动端目录按钮和播放器必须在同一行，正文区域不得被固定控件浪费。
- 发布成功的定义是远端 Pages 与 R2 验收通过，不是 Wrangler 命令返回零。
