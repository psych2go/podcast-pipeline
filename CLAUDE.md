# Podcast Pipeline v8

把英文播客转成可审计的中文完整笔记、中文讲稿、TTS 音频和移动端阅读页，并发布到 Cloudflare Pages + R2。

## AI 入口规则（先读）

- 普通新单集或单集重跑，只从 `scripts/process.py` 进入。
- 发布只使用 `scripts/catalog.py finish` 或 `finish-batch`，且必须完整发布到 R2 + Pages 并验收。
- 其他 `scripts/*.py` CLI 是内部维护/诊断接口；任务未明确指向某阶段时不要直接调用。
- `content/`、生成后的 `site/`、`reports/` 和 `.runlogs/` 是本地私有数据，禁止公开提交。
- 当前有效提示词只有 `scripts/纠错提示词.md` 与 `scripts/讲稿提示词.md`。
- 公开推送前运行 `scripts/check_public_repo.py`；私有分支历史不能仅靠 `.gitignore` 脱敏。
- 更短的入口和目录说明见 `README.md`；编码代理安全规则见 `AGENTS.md`。

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
内部编排通过 `EpisodeOptions` 参数对象进入 `process_episode()`；公开 CLI 与旧
`process(...)` 调用保持兼容，但阶段实现不再接收二十多个位置参数。
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

结果和策略说明见 `benchmarks/reports/asr-model-policy.md`。

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

中文完整笔记和讲书稿面向读者时只保留影响理解的事实状态限定，例如“节目称”“报道称”“仍在洽谈”“这是预测”；不得出现“这里不采用”“这里不保留”“本稿未独立核实”“纠错稿已将”等后台审查决策。无法核实的精确数字应直接改成自然、带归因的概括，审查理由只留在 JSON 产物中。严格质量门分别以 `notes_audit_narration` 和 `briefing_audit_narration` 阻断泄漏。

内容 subagent 完成后，`content_finalizer.py` 是唯一允许写回讲稿和
`summary_map.json` 的最终化阶段：它同步逐字标题、刷新正文哈希、只在自然段和
连续 unit 边界都明确时拆分超过一千字的章节，并只为全大写缩写生成确定性读音。
无法安全拆分或无法确认专名读音时必须阻断，不能猜测。

`enrich-evidence` 只刷新 unit 证据和已有精确 claim 证据，不会再猜测多
claim 单元的映射。历史数据需要按 unit 精炼：

```bash
.venv/bin/python scripts/content_map.py enrich-evidence "content/播客名"
.venv/bin/python scripts/claim_evidence.py "content/播客名" --unit U0001
```

strict 模式下 claim evidence runner 失败会按单 unit 重试，仍失败则阻断。
只有显式传入 `--allow-degraded-evidence` 才会写入可审计的降级映射；确定性
质量门会识别 `deterministic-fallback` 并阻断发布，不能用它降低 evidence v3
或逐 claim 证据要求。

已经发布的 evidence v2 单集只有同时满足以下条件才能暂时兼容：

- `episode.json.quality.claim_evidence_mode=legacy_broad` 是冻结前已有标记；
- `publish_report.json.passed=true`；
- `publish_report.json.checked_at` 换算为 Asia/Shanghai 日期后早于 2026-08-15。

`publish_report.json` 单独存在不能放行，手工新增 episode 标记也不能放行。

**Evidence v2 停止兼容时间：** 2026-08-15 起 legacy 模式只读，
`set-evidence-mode legacy*` 已禁用；2026-09-01 起 strict 质量门不再接受
evidence v2，所有待重新发布单集必须先迁移到 evidence v3。

### 4. AI 审查和确定性质量门

```bash
.venv/bin/python scripts/ai_review.py "content/播客名" --effort max
.venv/bin/python scripts/quality_report.py "content/播客名"
```

缺失或过期审查会进入最多两轮的受限 `review → safe repair → independent
re-review`。自动修复只允许 summary map 最终化、确定性 TTS 词典和明确 unit 的
claim evidence；事实性、医疗、数字、归因、转录质量及未知 high/critical 类别
一律阻断。修复记录写入 `review_repair.json`，绝不直接把 `passed` 改为 true。

复审会根据上次 `reviewed_files` 优先检查变化文件，但最终仍执行完整发布判定，
分数和 high/critical 阈值不变。AI review v3 要求先把复合 claim 拆为原子
subclaim，并用 `parent_claim_id` / `subclaim_id` 绑定 content_map；每个子主张分别填写：

- `claim_origin`：speaker_firsthand、speaker_reported、external_source、editorial_added、episode_metadata。
- `speaker_role`：guest、host、quoted_third_party、editorial、not_applicable、unknown。
- `assertion_type`：fact、opinion、prediction、recommendation、explanation、definition、anecdote、allegation、inference。
- `verification_mode`：web_required、source_document_required、web_spot_check、transcript_attribution、transcript_only、safety_cross_check、not_applicable。
- `risk_domain`：general、medical、legal、financial、political、safety。

`claim_type` 只保留为 v2 兼容派生字段；主持人观点、专家解释、第三方指控和节目
元数据不能再硬塞进 guest/public 类别。说话人内部数据和亲历事件不强制联网，
但必须保留原话、数字、范围和归因；`speaker_reported` 的第三方事实必须明确
归因，不能标记为无归因的 `used_as_fact`；诉状或报道只核查来源是否准确转述，
不能把指控本身当作已证明事实；高风险建议必须执行 safety cross-check。

只有 external_source/editorial_added 的客观 fact 可以写入
`fact_check_cache.json`。一手信息、观点、建议、解释和 allegation 不进入外部事实
缓存。动态公开事实有 TTL。AI review 会把精确 claim 命中的新鲜条目读取为
`fact_check_cache_context.json`，但只有 source URL 也一致时才能作为线索；缓存 verdict
不能替代本次独立判断、来源和日期核对。

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
- 实体准确性失败，例如公司、人名、产品、机构、职务或人物归属错误
- lexicon 应用后的真实 TTS 输入仍有数字、未映射缩写、难读符号、重复替换或未确认英文专名
- 结构体检存在错误

AI 只审内容，HTML、MP3、R2 和 Pages 的新鲜度由后续确定性检查负责。修改任一受审文件后必须重审。
质量报告同时写入人类可读的 `errors[]` 和机器稳定的
`error_details[].code`。自动复审只根据结构化 code 判断，不依赖中文错误文案。

AI 的 transcript/coverage/factuality 分数属于模型自评；确定性质量门能机械验证
schema、哈希、证据覆盖、verdict/URL 约束和产物新鲜度，但不能证明模型“诚实打分”。
因此结构约束是主要安全保证，分数门是补充信号，不应被解释为人工参考 WER 或
独立事实证明。

evidence 哈希链的信任边界是防止意外漂移和陈旧产物复用，不是抵御拥有本地写权限
且能重跑 enrich/review 的恶意修改。release 中的 Git commit 和 pipeline diff 指纹
提供外部版本锚；需要更强防篡改时应把 revision hash 固化到受保护的远端提交。

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
TTS、release、evidence 和 ASR 共用 `hashing.py`；TTS 与 HTML 共用
`sections.py`，避免缓存指纹、音频章节和页面章节采用不同边界。

通过 AI review 后，TTS 和 HTML 阶段只做只读校验，不再自动修改
`讲书稿.md` 或 `summary_map.json`。如果最终化仍会产生任何变化，流水线会阻断并
要求回到内容最终化和独立复审阶段，避免 TOCTOU。

- 任一章节失败会立即阻断。
- 失败时不会合并，也不会覆盖已有最终 MP3。
- 成功后先生成临时合并文件，再原子替换最终 MP3。
- 429 会遵守 `Retry-After`，但服务端指定的等待时间最多采用五分钟；
  429/5xx/网络错误按可配置指数退避重试。
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

`finish-batch` 只提供完整发布事务，不支持脱离 Pages 的单独 R2 上传。每期都会
追加 `catalog.finish-batch` 的 `run_report.json` 记录。R2 上传默认并发三路，可用
`--upload-concurrency` 调整；上传完成后必须继续生成站点、
部署 Pages，并逐期通过页面、R2 HEAD、`Content-Type`、文件大小、
`Accept-Ranges` 和 Range 响应验收，才算发布成功。

`catalog.py` 仍是唯一发布 CLI；健康报告实现位于 `catalog_health.py`，首页
渲染实现位于 `site_index.py`，CLI 和 `finish`/`finish-batch` 调用方式不变。

`finish` 会依次：

1. 验证 MP3、HTML、严格质量报告和 TTS manifest。
2. 同步 slug 页面和旧路径兼容页，并全量重建 `site/site.json`。
3. 从当前单集统计全量重建 `content/播客目录.md`。
4. 校验台账、`site.json`、真实音频时长和讲稿字数完全一致，再重建首页。
5. 上传音频到 R2。
6. 部署 Cloudflare Pages。
7. 验证首页、单集最终 URL、标题、播放器、R2 `Content-Type`、文件大小、`Accept-Ranges` 和 `206` Range 响应。
8. 写入 `publish_report.json`；任何远端检查失败都会令命令失败。

每期的 `release.json` 记录 release ID、讲稿/音频哈希、内容哈希音频 key、
发布阶段、`git_commit`、`git_dirty`、pipeline 代码差异指纹和
`pipeline_version`。默认只记录脏工作区而不阻断；需要可复现 clean release 时：

```bash
.venv/bin/python scripts/release.py \
  "content/播客名" "content/播客名/播客名.mp3" \
  "content/播客名/讲书稿.md" --require-clean
```

旧期没有该文件时继续使用旧的稳定音频 key。

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

subagent 默认创建隔离的临时 `CODEX_HOME`：复制 `auth.json`，并从用户
`config.toml` 生成最小安全配置，只保留 model、自定义 model provider、认证相关
字段和 `model_catalog_json`。hooks、plugins、remote plugins、workspace
dependencies、MCP servers、project trust、hook state 和 notice 不会复制。
这样既保留 custom provider 认证路径，也不会启动用户级 hook/MCP。子进程环境
只保留基础运行变量、实际 model provider 的认证变量和显式
`SUBAGENT_ENV_ALLOWLIST`；FISH、HF、Cloudflare 等流水线密钥不会透传。
可用 `SUBAGENT_CODEX_HOME` 指定由调用方维护的专用目录；只有显式设置
`SUBAGENT_INHERIT_CODEX_HOME=true` 才完整继承当前配置。所有 object
structured-output schema 会递归补齐 `required` 和
`additionalProperties=false`；`SUBAGENT_DISABLE_OUTPUT_SCHEMA` 仅用于调试。

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
- strict claim evidence 不静默降级；任何降级必须显式、可审计且默认禁止发布。
- AI repair 后必须重新独立审查，不能人工或脚本直接翻转审查结果。
- 移动端目录按钮和播放器必须在同一行，正文区域不得被固定控件浪费。
- 发布成功的定义是远端 Pages 与 R2 验收通过，不是 Wrangler 命令返回零。
