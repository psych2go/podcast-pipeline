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
- `原始转录.txt`：当前不可改写的 evidence revision
- `transcript.raw.json`：带 revision hash、逐段 hash 和 `S0001` segment ID 的结构化转录

抓取成功后，`process.py` 默认继续调用 Codex subagent 编排纠错、
content map、中文笔记、讲书稿、summary map、claim evidence 和 AI review。
如需只停在原始转录，使用 `--fetch-only` 或 `--no-auto-content`。

同名目录已有完整原始证据时，重复执行抓取命令会直接复用，不再次访问来源。
需要重新抓取时必须显式创建新 revision；旧证据会归档到 `evidence_history/`：

```bash
.venv/bin/python scripts/process.py "https://转录页面" \
  --name "播客名" --force-refetch
```

segment ID 只承诺在一个 evidence revision 内稳定。强制重抓后，下游
content map 和 AI review 依靠 SHA-256 自动失效，不跨 revision 猜测复用 ID。

本地音频需要显式名称：

```bash
.venv/bin/python scripts/process.py "episode.mp3" --name "播客名" --asr-quality max
```

默认按英文识别；中英混合或未知语言可使用 `--asr-language auto`。

`balanced` / `max` 默认启用自适应 ASR：从标题、音频文件名和已有来源信息
生成受限专名词表，首轮后仅对低置信区间使用 `max` 参数重解码。候选只有在
质量分数改善或风险消除时才替换，完整尝试记录在
`transcript.raw.json.meta.adaptive_refinement`。需要排查兼容性或只保留首轮
结果时使用：

```bash
.venv/bin/python scripts/process.py "episode.mp3" \
  --name "播客名" --no-asr-refine
```

每期最多重解码区间数由 `ASR_REFINE_MAX_RANGES` 控制，默认 8。

模型 policy 经 AMI ES2004a 人工参考集校准：`balanced` 默认
`large-v3-turbo`，`max` 保留 `large-v3` 作为显式复核档。显式
`--asr-model` 或 `ASR_MODEL` 仍优先。候选选择会识别范围受限的 prompt echo；
只有原稿由 context 词高度重复构成、仍为高风险且候选质量明显改善时，才允许
绕过普通长度/相似度门槛。

ASR 运行设备默认为自动选择：CUDA 设备及可选运行库完整时使用
`cuda:int8_float16`，否则使用 `cpu:int8`。安装和检查命令：

```bash
.venv/bin/pip install -r requirements-asr-gpu.txt
.venv/bin/python scripts/asr_runtime.py doctor \
  --preload-cuda --require-cuda
```

对同一音频做可重复 CPU/GPU benchmark：

```bash
.venv/bin/python scripts/asr_runtime.py benchmark "episode.mp3" \
  --model large-v3 \
  --runtime cpu:int8 \
  --runtime cuda:int8_float16 \
  --start 120 --duration 30 \
  --output reports/asr-runtime-benchmark.json
```

显式设置 `ASR_DEVICE` / `ASR_COMPUTE_TYPE` 会覆盖自动策略。

`balanced` / `max` 本地音频默认在最终文字确定后执行 WhisperX 强制对齐，
再运行 pyannote exclusive diarization。对齐环境与主 ASR 环境隔离：

```bash
.venv/bin/python scripts/setup_alignment_env.py
```

主流程自动查找 `.venv-alignment/bin/python`。缺少该环境或对齐失败时，
`ALIGNMENT_MODE=auto` 会保留 Whisper 原始时间戳并写入 warning；显式设置
`ALIGNMENT_MODE=whisperx` 时失败会阻断。临时关闭：

```bash
.venv/bin/python scripts/process.py "episode.mp3" \
  --name "播客名" --no-align
```

diarization 默认模型为
`pyannote/speaker-diarization-community-1`，优先使用
`exclusive_speaker_diarization`。可通过 `DIARIZATION_MODEL` 切回 3.1，
或用 `DIARIZATION_EXCLUSIVE=false` 使用普通重叠 turns。

公开多说话人回归基准：

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py check
.venv/bin/python scripts/asr_benchmark_workflow.py run
.venv/bin/python scripts/asr_benchmark_workflow.py verify
```

`benchmarks/asr-policy.json` 是默认模型、固定参考指纹、必测 policy 和验收阈值
的 contract。首次完成 community-1 后，`run --reuse-shared-diarization`
复用相同 turns；缓存的音频 SHA-256、字节数或说话人数约束不匹配时会阻断。
只修改指标实现时使用 `asr_benchmark_workflow.py rescore`，不重新运行模型。

任何 `ASR_PRESETS`、参考文件、manifest、指标或阈值变更必须依次通过：

1. `asr_benchmark_workflow.py check`
2. `run` 或 `rescore`
3. `asr_benchmark_workflow.py verify`
4. `.venv/bin/python -m unittest discover -s tests -v`

结果和策略说明见 `reports/ami-es2004a-model-policy.md`。

历史单集如果保留了 `*原始音频.mp3`，但旧转录没有模型、时间戳和说话人
元数据，先迁移为 `legacy_asr`：

```bash
.venv/bin/python scripts/episode.py migrate "content/播客名"
.venv/bin/python scripts/evidence.py check "content/播客名"
```

需要用当前最高质量配置重新 ASR 时：

```bash
.venv/bin/python scripts/process.py --name "播客名" --upgrade-asr
```

该命令自动使用目录中的原始音频、创建新 evidence revision、归档旧 revision，
并令 content map、讲稿和 AI 审查按哈希失效后重建。

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

没有真实时间戳的网页或本地文本使用 `evidence_mode=text_anchor`，通过
`segment_id + source_sha256` 回溯原始文本，不伪造音频时间；有真实时间戳的
来源继续使用默认的 `timestamp` 模式。

### 3. 写中文内容

主脚本按 `scripts/讲稿提示词.md` 调用 subagent：

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

已经发布的 evidence v2 单集只有在 `episode.json` 显式标记后才能暂时兼容，
`publish_report.json` 不再具备放行能力：

```bash
.venv/bin/python scripts/episode.py set-evidence-mode \
  "content/旧期" legacy_broad
```

该命令会修改受审文件，因此已有 `ai_review.json` 会按哈希规则失效，需要重审。
新单集不能使用 v2。

### 4. AI 审查和确定性质量门

```bash
.venv/bin/python scripts/ai_review.py "content/播客名" --effort max
.venv/bin/python scripts/quality_report.py "content/播客名"
```

以下任一情况都会阻断 TTS：

- 缺 `episode.json`、结构化转录、evidence v3、完整笔记或讲稿
- segment/claim/章节/笔记哈希不匹配
- 原始文本与 evidence revision hash 不匹配
- high/medium claim 未覆盖
- AI 审查过期或失败
- 转录质量、覆盖率、事实性任一低于 90
- balanced/max 本地 ASR 的时间戳覆盖率低于 95%、低置信度片段超过 15%
- TLS 证书校验被关闭的来源转录
- 本地 ASR 缺少纠错稿，或 `summary_map.transcript_basis` 未绑定纠错稿
- 原始音频存在但来源身份、音频哈希或 ASR provenance 缺失/冲突
- 数字、归因、TTS 可读性或稿件内容发布状态失败
- 结构体检存在错误

AI 只审内容，HTML、MP3、R2 和 Pages 的新鲜度由后续确定性检查负责。修改任一受审文件后必须重审。

`transcript_quality.score` 表示下游实际使用的综合转录质量。ASR 审查还会
区分 `raw_score`、`corrected_score` 和 `accuracy_basis`；没有人工标准稿时
不得声称计算过 WER。

只有 provenance 的 metadata-only 迁移且所有语义文件哈希完全未变时，才可在
远端审查服务不可用时执行：

```bash
.venv/bin/python scripts/ai_review.py "content/播客名" --rebind-provenance
```

内容、claim、讲稿或原始转录任一变化时，该命令会拒绝执行。

### 5. TTS 和 HTML

```bash
.venv/bin/python scripts/process.py --name "播客名" --tts-only
```

TTS 使用内容与配置指纹缓存，而不是文件时间。`tts_manifest.json` 绑定实际朗读文本、音色、模型、语速、章节设置、每节音频 SHA-256 和最终 MP3 SHA-256。

- 任一章节失败会立即阻断。
- 失败时不会合并，也不会覆盖已有最终 MP3。
- 成功后先生成临时合并文件，再原子替换最终 MP3。
- 429 会遵守 `Retry-After`，429/5xx/网络错误按可配置指数退避重试。
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

`tts.py` 和 `html_gen.py` 的 CLI 默认也会执行统一质量门；只有显式传入
`--allow-unchecked` 才能绕过，并将绕过记录写入 `run_report.json`。

### 6. 台账、站点和 Cloudflare

推荐一键收尾：

```bash
.venv/bin/python scripts/catalog.py finish "播客名" --dry-run
.venv/bin/python scripts/catalog.py finish "播客名"
```

多期同时发布时使用批量事务，音频逐期上传，但站点只部署一次：

```bash
.venv/bin/python scripts/catalog.py finish-batch \
  "播客一" "播客二" "播客三" --dry-run
.venv/bin/python scripts/catalog.py finish-batch \
  "播客一" "播客二" "播客三"
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

每期的 `release.json` 记录 release ID、讲稿/音频哈希、内容哈希音频 key
和发布阶段。旧期没有该文件时继续使用旧的稳定音频 key。

分步命令：

```bash
.venv/bin/python scripts/catalog.py add "播客名"
.venv/bin/python scripts/catalog.py sync-site
.venv/bin/python scripts/catalog.py rebuild
.venv/bin/python scripts/catalog.py check
.venv/bin/python scripts/catalog.py gen-index
```

跨单集查看最近运行健康度：

```bash
.venv/bin/python scripts/catalog.py health --since 7d
.venv/bin/python scripts/catalog.py health --since 7d \
  --output reports/weekly_health.md
```

报告汇总阶段失败率与平均耗时、高频错误、来源失败、TLS 降级、重试、
AI 报告成本/token，以及质量门未通过且尚未发布的单集。它只读取
`run_report.json` 等审计文件，不修改单集状态。

## 单集目录

```text
content/<storage_name>/
├── episode.json
├── 来源.md
├── 原始转录.txt
├── transcript.raw.json      # 含 provenance、revision 和 segment hash
├── evidence_history/          # --force-refetch 时归档旧 revision
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
├── publish_report.json
└── release.json
```

`storage_name` 只用于本地兼容；页面使用稳定 slug，展示标题独立存放在 `episode.json`。旧期保留旧 R2 object key；新 release 使用 `<slug>/audio/<audio_sha256>.mp3`。

不要删除审计文件。`run_report.json` 追加记录每次抓取、质量门、AI 审查、
TTS、HTML 和发布事务的耗时、失败、重试、调用量与模型成本字段。
`audio/` 分章节音频可在发布后清理，但保留它可以提高同配置重跑速度。

## 配置

`.env`：

```env
FISH_KEY=xxx
FISH_VOICE=xxx
FISH_MODEL=s2.1-pro-free
API_MAX_RETRIES=3
API_RETRY_BACKOFF=2.0
API_TIMEOUT=120
R2_PUBLIC_URL=https://pub-xxx.r2.dev
R2_BUCKET=podcast-audio
PAGES_PROJECT=podcast-scripts
PAGES_BASE_URL=https://podcast-scripts.pages.dev
HF_TOKEN=hf_xxx               # 可选；缺失时自动跳过 diarization 并告警
```

`FETCH_MAX_RETRIES`/`FETCH_TIMEOUT` 和
`TTS_MAX_RETRIES`/`TTS_TIMEOUT` 可覆盖通用网络参数。subagent 使用独立的
长时超时，避免把内容审查错误限制为普通 HTTP 请求时长。

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
- 强制重抓创建新 evidence revision，旧 revision 必须保留。
- 新单集默认严格门禁，兼容模式必须显式开启。
- TTS 只读 `讲书稿.md`，lexicon 只改变读音，不改变 HTML。
- 移动端目录按钮和播放器必须在同一行，正文区域不得被固定控件浪费。
- 发布成功的定义是远端 Pages 与 R2 验收通过，不是 Wrangler 命令返回零。
