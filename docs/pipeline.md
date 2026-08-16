# 播客处理流水线 v8 技术文档


> **文档角色：内部技术说明。** 普通处理请从 `scripts/process.py` 进入，
> 完整发布请使用 `scripts/catalog.py finish` / `finish-batch`。下文列出的
> 单阶段命令只用于明确的维护和诊断任务，不是可自由拼接的新流程。

## 1. 架构

```text
来源 URL / MP3
  -> fetcher.py
  -> episode.json + 来源.md + 原始转录.txt + transcript.raw.json
  -> subagent 转录纠错
  -> subagent content_map.json evidence v3
  -> subagent 中文完整笔记.md + 讲书稿.md + summary_map.json
  -> content_finalizer.py 规范化、章节重平衡、TTS 词典
  -> subagent claim evidence
  -> subagent ai_review.json + bounded review/repair
  -> quality_report.py
  -> tts.py + tts_manifest.json
  -> html_gen.py
  -> catalog.py
  -> R2 + Pages
  -> publish.py 远端验收
```

### 核心深模块

- `process.py` 的内部入口是 `process_episode(source, name, EpisodeOptions)`；旧的
  多参数 `process(...)` 只保留为兼容 adapter，CLI 不再逐项位置透传参数。
- `sections.py` 是 TTS 和 HTML 唯一的 Markdown 章节解析 seam；两侧只保留返回
  旧 tuple 形状的轻量 adapter，避免章节数、标题和正文边界静默漂移。
- `pipeline_metrics.py` 统一读取质量报告指标，`process.py` 与 `preflight.py` 不再
  各维护一份副本。
- `quality_errors.py` 给质量错误附加稳定 `error_details[].code`；自动复审依据 error
  code 决策，不再依赖中文错误文案前缀。原 `errors[]` 文本继续保留用于人类阅读。
- `catalog_health.py` 独立聚合 `run_report.json`/质量/发布状态；
  `site_index.py` 独立完成首页统计和卡片渲染。`catalog.py` 保留 CLI 与发布事务编排，
  通过兼容 wrapper 调用这两个深模块。
- `hashing.py` 统一文件、文本和 bytes 的 SHA-256；`text_distance.py` 统一 benchmark
  Levenshtein 距离及插入/删除/替换明细，避免两套 DP 漂移。

流水线把三类判断分开：

- **语义审查**：Codex subagent 检查转录、覆盖、事实、数字、归因和 TTS 文本。
- **确定性质量门**：校验 schema、证据 ID、正文哈希、覆盖率、审查哈希和 TTS manifest。
  AI 分数是模型自评，确定性层验证的是结构、证据、verdict/URL 与新鲜度，
  不把自评分数表述为人工参考真值。哈希链用于防意外漂移，不防有本地写权限者
  重算整条链；release Git provenance 是外部版本锚。
- **远端发布验收**：验证 Pages 页面和 R2 音频真实可访问、可 Range 播放。

Codex subagent 使用临时 `CODEX_HOME`。隔离配置不是简单丢弃用户
`config.toml`，而是生成最小安全副本：保留 model、自定义 provider、认证字段和
model catalog；强制关闭 hooks、plugins、remote plugins 与 workspace
dependencies，并删除 MCP、project trust、hook state 等副作用配置。这样使用
custom provider 的环境仍可认证，同时不会加载用户自动化扩展。

质量不变量：新单集始终要求 evidence v3 和逐 claim 证据；AI 三项百分制不得
低于九十分；任何 high/critical issue 阻断；所有降级必须显式记录且默认不能
发布。P0/P1/P2 优化只减少静默失败、重复工作和发布等待，不降低这些门槛。

## 2. 核心模块

### `episode.py`

`episode.json` 是单集元数据真源：

- `id`：稳定内容 ID
- `storage_name`：本地目录兼容名
- `slug`：稳定、ASCII、安全的公开路径
- `display_title`：不受文件系统清洗影响的展示标题
- `source`：URL、标签、来源类型、提取器
- `quality`：严格/旧期模式、转录状态、纠错状态、内容审查状态
- `publish`：页面路径、旧路径别名、R2 object key

`quality` 状态不再只信任人工写入值。`inspect_episode_state()` 根据当前
`transcript.raw.json`、纠错稿和 AI 审查哈希推导来源、纠错和审查状态；
`sync_episode_state()` 再把派生结果写回 `episode.json` 和 `来源.md`。

迁移命令：

```bash
.venv/bin/python scripts/episode.py migrate "content/播客名"
.venv/bin/python scripts/episode.py migrate-all content
```

### `fetcher.py`

- Podscripts：只提取带时间点的 transcript 文本，排除页面导航和相关推荐。
- 网页：按结构化提取、HTTP 降级策略抓取。
- 音频：faster-whisper，支持 `fast`、`balanced`、`max` 质量预设。
- `balanced` 默认 `large-v3-turbo`；`max` 使用 `large-v3` 作为显式复核档。
- `balanced` / `max` 默认执行困难片段定向重解码；`--no-asr-refine` 可关闭。
- 自动从标题、音频名和已有来源信息生成有长度上限的 prompt/hotwords。
- 输出 evidence revision hash、逐段 hash、segment ID、时间戳、说话人和模型元数据。

同名单集已有原始证据时默认复用；`--force-refetch` 才会抓取新 revision，并把
旧 `原始转录.txt` / `transcript.raw.json` 归档到 `evidence_history/`。
segment ID 在 revision 内稳定，跨 revision 依靠 evidence hash 使下游台账失效，
不按文本相似度猜测复用。

显式 `--asr-model` 优先于质量预设。默认语言为英文，可用
`--asr-language auto` 自动检测。启用 diarization 但缺少 `HF_TOKEN` 时会保留
ASR 结果、跳过说话人分离并写入 warning。

自适应 ASR 会根据 `avg_logprob`、压缩率、静音冲突、词置信度以及关键数字/
专名标记 `needs_redecode`。相邻困难 segment 合并后，以同一模型的 `max`
参数重解码；候选质量未改善、长度异常或解码失败时保留首轮文本。审计数据写入：

```text
transcript.raw.json
  meta.asr_context
  meta.adaptive_refinement
  segments[].quality_flags
  segments[].needs_redecode
  segments[].refinement
```

默认每期最多处理 8 个区间，可通过 `ASR_REFINE_MAX_RANGES` 调整。

候选选择额外处理 prompt echo：当原稿几乎完全由 context 词重复构成、原段
仍为高风险，而候选质量更高、风险消除且语速合理时，可绕过长度和文本相似度
门槛。普通的高置信度跑题候选仍会被拒绝。

ASR runtime 默认使用 `auto:auto`。`asr_runtime.py` 将 CUDA 探测、NVIDIA
wheel 动态库预加载和 benchmark 收敛在一个模块中：

```bash
.venv/bin/pip install -r requirements-asr-gpu.txt
.venv/bin/python scripts/asr_runtime.py doctor \
  --preload-cuda --require-cuda
.venv/bin/python scripts/asr_runtime.py benchmark "episode.mp3" \
  --model large-v3 \
  --runtime cpu:int8 \
  --runtime cuda:int8_float16 \
  --start 120 --duration 30 \
  --output reports/asr-runtime-benchmark.json
```

自动策略只在 CUDA 设备、cuBLAS、cuDNN 和 `int8_float16` 均可用时选择
GPU，否则回退 `cpu:int8`。显式指定的无效配置会报错，不会静默回退。

最终文字确定后，主流程按以下顺序处理时间与说话人：

```text
adaptive ASR
  -> WhisperX forced alignment
  -> pyannote community-1 exclusive diarization
  -> speaker merge
```

WhisperX 使用隔离环境，避免其 Torch 版本约束改写主 ASR 环境：

```bash
.venv/bin/python scripts/setup_alignment_env.py
```

`ALIGNMENT_MODE=auto` 在环境缺失或运行失败时保留 Whisper 时间戳并告警；
`ALIGNMENT_MODE=whisperx` 则严格失败。`--no-align` 可显式关闭。

community-1 的 pyannote 4.x 输出优先读取
`exclusive_speaker_diarization`，并兼容 `speaker_diarization`、
旧 `Annotation.itertracks()` 和序列化结构。实际模型、设备、exclusive 状态、
词时间戳覆盖率均写入 `transcript.raw.json` 和 provenance。

多说话人模型 policy 使用 AMI ES2004a 五分钟人工参考集：

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py check
.venv/bin/python scripts/asr_benchmark_workflow.py run
.venv/bin/python scripts/asr_benchmark_workflow.py verify
```

统一 workflow 的 contract 位于 `benchmarks/asr-policy.json`，固定：

- `fast` / `balanced` / `max` 的生产默认模型；
- manifest、音频和人工参考文件指纹；
- 必须成功的候选模型；
- recommendation、DER/JER、WER、实体/数字、对齐和时间戳阈值。

复跑模型时使用 `run --reuse-shared-diarization`。缓存只有在音频 SHA-256、
字节数和说话人数约束完全一致时才会复用。只修改指标代码时使用 `rescore`，
直接读取已有 hypotheses。`report.json` 记录输入指纹和关键包版本，
`policy_verification.json` 记录验收结果。

修改 ASR 默认模型、参考数据、manifest 或阈值时必须按顺序执行：

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py check
.venv/bin/python scripts/asr_benchmark_workflow.py run \
  --reuse-shared-diarization
.venv/bin/python scripts/asr_benchmark_workflow.py verify
.venv/bin/python -m unittest discover -s tests -v
```

基准报告位于 `benchmarks/reports/asr-model-policy.md`。

Podscripts 只有在明确的证书校验异常时才允许 source-scoped TLS 降级；降级状态
写入转录元数据，并由严格质量门阻断发布。

### `evidence.py`

`evidence.py` 是来源 provenance 的统一模块：

- transcript 与 `local_asr/legacy_asr` 使用明确来源类型。
- ASR revision 记录原始音频 SHA-256、字节数、时长、模型、质量预设、语言、
  时间戳和说话人分离状态。
- 历史目录存在 `*原始音频.mp3` 且旧记录是普通本地转录时，迁移为
  `legacy_asr`，不会继续冒充官方字幕。
- 质量门通过 `effective_source_kind()` 判断 ASR，不能靠错误状态绕过
  时间戳、置信度和纠错要求。
- `转录_纠错.txt` 的词级漂移写入质量报告；该比例用于观察，不等于 WER。

```bash
.venv/bin/python scripts/evidence.py migrate "content/播客名"
.venv/bin/python scripts/evidence.py check "content/播客名"
.venv/bin/python scripts/process.py --name "播客名" --upgrade-asr
```

### `content_map.py`

evidence v3 的约束：

- 每个 transcript segment 在当前 evidence revision 内有稳定 `Sxxxx` ID。
- unit 的 evidence 绑定 segment ID、时间范围和源文本 SHA-256。
- 每个 claim 通过 `claim_evidence` 绑定最小 segment 集合。
- 每个 claim 的 segment 集合有独立 SHA-256、置信度和选择理由。
- 有时间戳来源使用 `evidence_mode=timestamp`；无时间戳来源使用
  `evidence_mode=text_anchor`，通过 segment ID 和源文本哈希定位，不伪造时间。
- 多 claim 单元禁止所有 claim 全量复用整个 unit 证据。
- `summary_map.json` 绑定章节正文 SHA-256。
- 笔记通过显式生成并经 AI 逐条复核的 `notes_claim_ids` 与 `notes_sha256` 绑定；
  deterministic enrich 只刷新哈希，不会自动填入全集来自我认证。
- `summary_map.transcript_basis` 绑定实际用于写稿的原始稿或纠错稿 hash。

```bash
.venv/bin/python scripts/content_map.py check "content/播客名/content_map.json"
.venv/bin/python scripts/content_map.py coverage \
  "content/播客名/content_map.json" \
  "content/播客名/summary_map.json"
.venv/bin/python scripts/content_map.py enrich-evidence "content/播客名"
.venv/bin/python scripts/claim_evidence.py "content/播客名" --unit U0001
```

`enrich-evidence` 不再为多 claim 单元猜测证据。新内容应在生成
`content_map.json` 时直接填写精确映射；`claim_evidence.py` 主要用于按 unit
迁移历史数据。超过 60 个 segment 的 unit 会产生结构告警。

### `ai_review.py`

Codex subagent 读取内容证据文件，输出结构化 `ai_review.json`：

- 转录质量、覆盖率、事实性百分制评分
- 数字、归因、实体准确性、TTS、内容发布状态
- 每个 issue 的 evidence type、segment IDs、URL 和核查日期
- 带 `claim_type`、`verification_mode` 和 publication status 的 `fact_checks`
- 所有受审文件 SHA-256

AI review v3 先把复合 content-map claim 拆成原子 fact checks。每条记录使用
`parent_claim_id=Uxxxx-Cxx` 和连续唯一的 `subclaim_id=Uxxxx-Cxx-F01/F02...`，
避免把公开事实、说话人观点和因果推论放在一个分类里。

分类使用正交字段：

- `claim_origin`：speaker_firsthand、speaker_reported、external_source、editorial_added、episode_metadata。
- `speaker_role`：guest、host、quoted_third_party、editorial、not_applicable、unknown。
- `assertion_type`：fact、opinion、prediction、recommendation、explanation、definition、anecdote、allegation、inference。
- `verification_mode`：web_required、source_document_required、web_spot_check、transcript_attribution、transcript_only、safety_cross_check、not_applicable。
- `risk_domain`：general、medical、legal、financial、political、safety。

旧 `claim_type` 只作兼容派生：external_source+fact 为 `public_fact`，嘉宾一手
fact/anecdote 为 `guest_firsthand`，嘉宾 opinion/prediction/recommendation 为
`guest_opinion`，编辑部 fact/inference 分别为旧 editorial 两类；其他组合使用
`not_applicable`，不能为兼容而错误标注说话人或事实性质。

speaker_firsthand 不要求公开 URL，但必须 transcript attribution；allegation 使用
source document required 和 `accurately_reported`，只证明来源确实这样说；高风险
recommendation 使用 safety cross-check；explanation/definition 根据风险使用转录
归因或 web spot-check。

`entity_accuracy` 独立核对公司、人名、产品、机构、职务、技术术语和主体归属。
名称拼错或主体错配属于发布阻断，不会被“嘉宾一手信息”豁免。医疗、法律和金融等
高风险可执行建议同样保留公开安全核查要求。

AI 不检查 HTML、MP3、R2 或 Pages；这些机械状态由确定性流程负责。

ASR 单集额外区分原始 ASR、纠错后转录及准确率依据。没有人工参考逐字稿时，
`accuracy_basis` 必须是抽样或语义审查，不能把综合分数表述成 WER。

metadata-only provenance 迁移可以使用严格受限的 `--rebind-provenance`：
它只允许 `episode.json`、`来源.md` 和 `transcript.raw.json` 的已证明元数据
变化，任何语义文件变化都会拒绝复用旧审查。

### `quality_report.py`

严格模式要求：

- 完整 evidence v3
- 严格来源/转录状态
- 完整笔记与讲稿均存在
- claim 和笔记覆盖通过
- AI 审查当前且通过
- AI review v3 的原子 subclaim、content-map parent 绑定、实体准确性和多维核查规则通过；v1/v2 审查在下次内容变化时重审迁移
- 三项百分制评分均不低于 90
- balanced/max 本地 ASR 的时间戳与低置信度确定性指标通过
- 本地 ASR 已生成纠错稿，且 summary map 绑定纠错稿 hash
- 来源抓取未关闭 TLS 证书校验
- 无结构错误
- lexicon 应用后的真实 TTS 输入不存在残留数字、未映射缩写、难读符号、重复替换或未确认英文串

笔记与讲稿的长度比例只作告警，语义覆盖由 claim coverage 和 AI 审查决定。

### `tts.py`

TTS manifest 的 section fingerprint 包含：

- lexicon 替换和规范化后的真实朗读文本
- Fish voice/model
- speed
- 是否朗读标题
- 最大切片长度

缓存命中还必须校验章节文件 SHA-256。任何章节失败时：

- 返回失败结果
- 不运行 merge
- 不覆盖已有最终 MP3
- manifest 记录失败章节

全部完成后合并到临时文件并原子替换最终 MP3。发布前会再次验证最终文件大小和 SHA-256。
manifest 同时记录实际 API 请求数、重试数、合成 chunk 数和合成字符数。

直接运行 `tts.py` 时默认先执行统一质量门；只有显式传入
`--allow-unchecked` 才能绕过，并写入 `run_report.json`。

AI review 之后，`process.py` 的 TTS/HTML 路径只读验证讲稿和 summary map。
任何仍需规范化、拆章或刷新哈希的情况都会阻断，必须回到
`content_finalizer.py`，再重新生成证据绑定并独立复审。

已有音频只能用 `--backfill-manifest` 登记文件清单，结果会标记为
`legacy_unverified`，不会被严格发布接受，因为仅凭 MP3 无法证明它与当前文本对应。
要重新发布必须执行一次受 manifest 记录的完整 TTS 合成。

```bash
.venv/bin/python scripts/tts.py "content/播客名/讲书稿.md" \
  "content/播客名/audio" --backfill-manifest
```

### `html_gen.py`

阅读页为自包含 HTML：

- 桌面端目录侧栏
- 移动端首行目录按钮 + 音频播放器
- 播放、进度、倍速、音量、下载
- 章节锚点和滚动高亮
- 减少动态效果偏好支持

播放器 URL 来自 `episode.json.publish.audio_key`，经 URL 编码后拼接 `R2_PUBLIC_URL`。
如果存在 `release.json`，优先使用其中的内容哈希音频 key。
直接运行 `html_gen.py` 时同样默认执行统一质量门，显式绕过参数为
`--allow-unchecked`。

### `catalog.py` 和 `publish.py`

`catalog.py finish` 是发布事务入口。它会先全量重建 `site.json` 和
`播客目录.md`，校验标题、顺序、字数和 `ffprobe` 真实时长一致，再上传 R2、
部署 Pages，随后由 `publish.py` 验证：

- 首页 HTTP 200 且出现展示标题
- 单集 URL 最终 HTTP 200、标题正确、有播放器
- R2 HEAD 的类型、长度和 Range 能力
- `bytes=0-1023` 返回 206、1024 字节和正确 `Content-Range`

结果写入 `publish_report.json`。远端不一致时命令失败。
新 release 另外写入 `release.json`，记录内容哈希音频 key、Git commit、
工作区 dirty 状态、pipeline 代码差异 SHA-256、pipeline version 和
`prepared/uploaded/site_ready/deployed/published/failed` 状态；旧期没有该文件时
继续使用原有音频 key。默认仅记录 dirty 状态；`scripts/release.py ...
--require-clean` 可选择阻断脏工作区。
每个自动化命令的阶段耗时、状态、调用量、重试和可用成本字段追加写入
单集的 `run_report.json`；写入使用临时文件原子替换，中断的 running 记录会在
下一次执行时自动标记为失败。

`catalog.py finish-batch` 对多期先统一预检，再以默认三路并发上传内容哈希音频，
只同步一次站点并部署一次 Pages，最后逐期执行远端页面和 Range 验收。它只支持
完整发布事务，不提供脱离 Pages 的 R2-only 模式：

```bash
.venv/bin/python scripts/catalog.py finish-batch \
  "播客一" "播客二" --upload-concurrency 3
```

任何 R2、站点生成、Pages 部署或远端验收失败都会令整个批量事务失败；只有逐期
页面与音频均验收通过后，release 状态才进入 `published`。

### `content_finalizer.py`、`review_repair.py` 与 fact-check cache

- 所有章节标题、summary map 和正文 hash 在 review 前一次性最终化。
- 超过一千中文字的章节只在多个自然段、多个连续 unit、claim 前缀都可机械对齐，
  且拆分后两章均为四百至一千字时自动拆分；否则阻断。
- TTS 自动词典只生成全大写缩写的确定性拼读，不猜混合大小写专名。
- review repair 最多执行 `REVIEW_REPAIR_MAX_ROUNDS` 轮；只处理 summary、TTS 和
  明确 unit 的 evidence integrity，其他 high/critical 类别阻断。
- 每次修复后重新独立 review，旧 review 只提供变化范围线索，最终阈值不变。
- `fact_check_cache.json` 只缓存 external_source/editorial_added 的 objective fact，key 绑定规范化
  claim hash、source URL 和 source date；一手信息、观点、建议、解释和 allegation 不进入外部事实缓存。
  动态事实按 TTL 失效，历史事实可长期复用为核查线索。AI review 会读取缓存，
  按当前 claim hash 生成非权威 `fact_check_cache_context.json`；只有 claim 与 source URL
  都一致且仍在 TTL 内时才能作为线索，缓存 verdict 不能替代独立复审。

## 3. 严格模式与旧期

**Evidence v2 停止兼容时间：** 2026-08-15 起 legacy 模式只读，禁止新设或恢复；
2026-09-01 起 strict 质量门不再接受 evidence v2，所有待重新发布单集必须先迁移到
evidence v3。

新单集一律严格模式。缺少 `content_map.json` 时，`process.py` 默认失败。
新单集还必须使用 evidence v3。历史 evidence v2 必须同时具备冻结前已有的
`episode.json.quality.claim_evidence_mode=legacy_broad` 和通过的
`publish_report.json`，且 `checked_at` 换算为 Asia/Shanghai 日期后早于
2026-08-15。发布报告单独存在或手工新增 episode 标记都不能启用兼容。

2026-08-15 起 `set-evidence-mode legacy*` 已禁用；只能把旧期迁移回
`precise_required`。episode manifest 属于 AI 审查输入，迁移后必须重新运行 AI 审查。

`--allow-legacy-quality` 只允许维护已经存在于 `site.json` 的旧期。`catalog.py sync-site` 不允许一个新的无证据单集进入站点。

## 4. Cloudflare 配置

```env
R2_PUBLIC_URL=https://pub-xxx.r2.dev
R2_BUCKET=podcast-audio
PAGES_PROJECT=podcast-scripts
PAGES_BASE_URL=https://podcast-scripts.pages.dev
```

本地 Wrangler 默认使用 OAuth，发布前确认当前身份：

```bash
npx wrangler login
npx wrangler whoami
```

不要把 `CLOUDFLARE_API_TOKEN` 写入项目 `.env`。Wrangler 会在命令启动时
再次读取工作目录中的 `.env`，过期 token 会覆盖有效 OAuth。流水线的
Wrangler 包装层会在 `site/` 目录运行；若项目 `.env` 中含 Cloudflare 凭据且值
与当前环境相同，会移除该疑似本地注入值，避免覆盖 OAuth。CI/shell 直接注入且不在
项目 `.env` 中的 API Token 会保留。

音频上传必须带 `--remote` 和 `audio/mpeg`：

```bash
npx wrangler r2 object put "podcast-audio/<object-key>" \
  --file "content/播客名/播客名.mp3" \
  --content-type audio/mpeg --remote
```

Pages：

```bash
npx wrangler pages deploy site --project-name podcast-scripts --branch main
```

实际发布使用 `catalog.py finish`，避免漏掉远端验收。

## 5. 测试

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_pipeline.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_browser_layout.py' -v
.venv/bin/python -m playwright install --with-deps chromium
```

覆盖范围包括：

- Podscripts fixture 抽取
- evidence v3、claim 最小证据和哈希防伪
- 严格/旧期质量门
- TTS fail-fast、缓存指纹、旧最终音频保护
- site readiness
- Pages/R2 MockTransport 发布验证
- 320/375/430 像素移动端几何布局

浏览器依赖见 `requirements-browser.txt`。测试不再因 Chromium 或系统库缺失
而静默跳过；GitHub Actions 会安装完整依赖并强制执行。

## 6. 发布不变量

1. 原始转录按 evidence revision 保留，强制重抓不得销毁旧 revision。
2. AI 审查哈希必须匹配当前内容。
3. TTS manifest 必须匹配当前朗读文本与配置。
4. 失败 TTS 不得覆盖已发布音频。
5. 新单集不得走旧期兼容通道。
6. 页面使用稳定 slug，并保留历史路径别名。
7. Wrangler 成功不等于发布成功，必须通过远端 Pages/R2 验收。
8. 台账和 site.json 必须由当前内容全量重建并通过一致性检查。
9. 新单集的多 claim 单元不得全量复用整个 unit 证据。
10. 原始音频存在时必须有 ASR provenance；历史未知模型必须显式标为
    `legacy_asr/unknown`，不得写成官方字幕。
11. 转录纠错只修复听写和说话人识别，嘉宾本身的事实错误必须保留原话并在
    fact check 或稿件归因中处理。
