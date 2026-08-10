# Podcast Pipeline v7 改进计划

> 生成时间：2026-08-05
>
> 状态：已由 Codex 逐项评估，并于 2026-08-05 完成第一轮适用改进。

---

## 实施评估

| # | 结论 | 处理 |
|---|------|------|
| 1 | 采纳 | 关键 JSON、HTML、讲稿修复、来源记录、站点清单和目录已统一使用原子写；章节及最终 MP3 使用唯一临时路径验证后替换。 |
| 2 | 不适用 | 当前 `.idea/`、`.vscode/` 既未被跟踪，也未出现在 `git status`。对未跟踪文件执行 `git rm --cached` 不会生效。 |
| 3 | 调整后采纳 | 抓取 transport 链和 Fish TTS 增加可配置指数退避，TTS 支持 `Retry-After`，重试与最后错误进入 metrics。未给 Claude CLI 增加盲目重试，也未引入跨进程熔断。 |
| 4 | 采纳 | 新增 `catalog.py health --since ... [--output ...]`，汇总失败率、耗时、错误、来源、TLS、重试、成本/token 和未发布质量失败单集。 |
| 5 | 调整后采纳 | 保留现有低风险机械修复，并把动作及前后 SHA-256 写入 stage metrics；不自动生成标题或扩写内容。 |
| 6 | 暂不采纳 | evidence 的机械一致性已有确定性校验，改用低价 LLM 会增加不确定性；双阶段 LLM 也无法证明与现有审查质量等价。 |
| 7 | 大部分已存在 | TTS 已按 800 字分片、可重试、记录失败章节、复用成功章节并阻止失败覆盖最终 MP3。本轮补强原子替换和 `Retry-After`；不允许残缺音频发布。 |
| 8 | 暂不采纳 | revision ID、转录/segment/claim/笔记/讲稿哈希及 AI review 文件哈希已形成完整校验链；新增 manifest 会重复状态并增加同步失效点。 |
| 9 | 采纳 | 新增共享 `sources.py`，`episode.py` 与 `catalog.py` 使用同一来源标签映射。 |
| 10 | 调整后采纳 | TTS 和非 dry-run 发布在修改产物前做配置预检。`HF_TOKEN` 保持可选，因为现有契约是缺失时跳过 diarization 并告警。 |
| 11 | 部分替代 | 新增针对原子写、重试、健康汇总、配置预检和修复留痕的测试；完整 mock E2E 留待流水线接口稳定后再做，避免当前形成高耦合脆弱测试。 |
| 12 | 暂缓 | 批量并发前需要先定义 `site.json`、首页和目录的全局事务锁，否则单文件原子写仍无法防止并发丢失更新。 |
| 13 | 合并实施 | 成本、token、耗时和失败统计已并入 `catalog health`，避免另建功能重叠的 `metrics.py`。 |
| 14 | 部分已有 | secret 已集中从 `config.py` 读取，未发现打印完整 key；TLS 降级已有域名限制和质量阻断。密钥轮换与最小权限主要是部署运维策略，不应由代码自动执行。 |

验证结果：

- `tests/test_pipeline.py`：72 个测试通过。
- `tests/test_browser_layout.py`：1 个 Playwright 布局测试通过。
- `catalog.py health --since 7d` 已在当前内容目录成功运行。

---

## 当前基线

- 代码库：Podcast Pipeline v7
- 当前分支：`main`
- 未提交改动：13 个文件（`CLAUDE.md`、9 个脚本、3 个提示词文档、`tests/test_pipeline.py`），以及新建未跟踪文件 `scripts/atomic_io.py`
- 测试：64 个单元测试全部通过（`tests/test_pipeline.py`）
- 近期核心方向：证据完整性（evidence revision、SHA-256 校验、atomic I/O）、ASR 质量门控、来源抓取鲁棒性

---

## 改进目标

1. 提升**发布可靠性**：消除并发/中断导致的关键文件半写风险。
2. 提升**运维可见性**：让失败、成本和人工介入点更可观测。
3. 提升**成本效率**：对高成本 AI/TTS 阶段引入分层与重试策略。
4. 保持**审计可追溯**：所有改动都必须在 `run_report.json` 或专用审计日志中留下记录。
5. 避免**过度工程**：不引入数据库、不扩展 v2 兼容、不加实时 Web UI。

---

## 一、高价值改进（建议优先实施）

### 1. 统一并补齐 atomic I/O 覆盖

**问题**

`scripts/atomic_io.py` 已新建，但扫描发现仍有大量关键写入使用直接写：

- `episode.py`：直接写 `episode.json`
- `content_map.py`：直接写 `content_map.json`
- `ai_review.py`：直接写 `ai_review.json`
- `publish.py`：直接写 `publish_report.json`
- `html_gen.py`：直接写 HTML 文件
- `validator.py`：可能直接写校验相关文件
- `tts.py`：MP3/元数据写入路径
- `catalog.py`：写 `site.json`、`播客目录.md`

并发运行、进程被 `SIGTERM` 或磁盘满时，会导致 JSON 截断、HTML 半写、`episode.json` 损坏。

**实施内容**

1. 把所有**受审文件**的写入统一收口到 `atomic_write_json` / `atomic_write_text`：
   - `episode.json`
   - `content_map.json`
   - `summary_map.json`
   - `ai_review.json`
   - `quality_report.json`
   - `tts_manifest.json`
   - `publish_report.json`
   - `run_report.json`
2. 对 `site.json`、`播客目录.md` 等全局状态文件也使用原子写。
3. 对临时 MP3 分片，确保「先写临时文件，再原子替换最终文件」。

**验收标准**

- `grep -n "write_text\|write_bytes\|json.dump" scripts/*.py | grep -v atomic_io` 不再命中受审文件写入点。
- 新增或扩展一个单元测试：模拟写入过程中抛异常，验证最终文件不会被截断。
- 所有 64 个现有测试继续通过。

**风险**

- 低。`atomic_io.py` 已经实现并测试过。

---

### 2. 清理 `.gitignore` 已排除的 IDE 目录

**问题**

`.gitignore` 已包含 `.vscode/` 和 `.idea/`，但 `git status` 仍显示这两个目录为未跟踪文件，存在误提交风险。

**实施内容**

执行以下命令（不删除本地文件，仅从 Git 索引移除）：

```bash
git rm -r --cached .idea .vscode 2>/dev/null || true
git status
```

**验收标准**

- `git status` 不再显示 `.idea`、`.vscode`。

**风险**

- 极低。

---

### 3. 为外部 API 调用增加可配置重试与熔断

**问题**

当前 `fetcher.py` 对 curl_cffi / curl / httpx 有四级 transport 降级，但同一 transport 没有重试。
TTS（Fish Audio）和 AI review（Claude API）在高频调用时可能偶发 429 / 5xx / 网络抖动，导致整条流水线失败。

**实施内容**

1. 在 `config.py` 新增可选环境变量：
   - `API_MAX_RETRIES`（默认 3）
   - `API_RETRY_BACKOFF`（默认 2.0，指数退避基数）
   - `API_TIMEOUT`（默认 300，覆盖 MCP/tool 调用）
2. 新增 `scripts/retry.py` 或内联装饰器 `retry_with_backoff(max_retries, backoff, retryable_exceptions)`。
3. 应用位置：
   - `fetcher._fetch_html` 中 curl_cffi / curl / httpx 请求
   - `tts.py` 中 Fish Audio 请求
   - `ai_review.py` 中 Claude API 调用
4. 对 HTTP 429 特殊处理：读取响应头 `Retry-After`，按服务端要求等待。
5. 每次重试记录到 `run_report.json` 的对应 stage metrics 中：`retry_count`、`last_error`。

**验收标准**

- 新增单元测试：mock 接口前两次返回 429，第三次成功，验证结果正确且总延迟符合退避公式。
- 新增单元测试：mock 接口返回非 429 的 500，验证按配置重试次数后失败。
- 配置项在 `.env.example` 中可见。

**风险**

- 低。只改变失败时的行为，正常路径不变。

---

### 4. 建立跨单集的 `run_report` 聚合健康视图

**问题**

`run_report.json` 是按单集的。当内容目录有数十期时，需要逐个目录查看才能发现共性问题。

**实施内容**

在 `catalog.py` 新增子命令：

```bash
.venv/bin/python scripts/catalog.py health [--since 7d] [--output reports/weekly_health.md]
```

输出内容：

| 维度 | 示例 |
|------|------|
| 最近 N 天各阶段失败率 | fetch / tts / ai_review / publish |
| 高频错误来源 | 域名、API 错误码、TLS 降级次数 |
| 未通过质量门且未发布单集列表 | 名称、最后失败阶段、失败原因摘要 |
| 平均各阶段耗时 | fetch → tts → publish |

实现要点：

1. 遍历 `content/*/run_report.json`。
2. 只读取 `schema_version` 匹配的合法报告。
3. 过滤掉被用户显式跳过/legacy 的单集。
4. 输出 Markdown 表格到 stdout 或指定文件。

**验收标准**

- 新增 `catalog health` 单元测试：构造 3 期 run_report，验证能正确汇总失败率和未发布列表。
- 输出文件使用 atomic 写。

**风险**

- 低。只读已有 `run_report.json`，不写回单集状态。

---

### 5. 让质量门中的「可自动修复项」真正自动化并留痕

**问题**

当前测试输出：`[校验] 发现 2 个问题（可自动修复项已处理）`，但日志未展示具体修复动作。
「可自动修复项」包括标题太少、篇幅偏短提示、引言段过短建议等，目前依赖人工判断。

**实施内容**

1. 在 `validator.py` 或 `process.py` 中识别低风险自动修复项：
   - 标题太少：如果内容自然分段明显，自动建议/插入二级标题（仅在 `--auto-fix` 启用时）。
   - 引言段过短：在质量报告中给出明确扩写提示，不直接改原文。
   - 篇幅偏短：仅作为 warning，不改原文。
2. 增加 `--auto-fix` 命令行参数：
   - `scripts/process.py --name "XXX" --auto-fix`
   - 默认关闭，避免意外改写内容。
3. 任何自动修复必须：
   - 在 `run_report.json` 中记录 `auto_fixes` 列表，含修复项、修复前摘要、修复后摘要。
   - 触发 AI review 失效（因为内容已变）。

**验收标准**

- 新增单元测试：`--auto-fix` 能修复标题太少，并正确写入 `run_report.json`。
- 新增单元测试：默认不带 `--auto-fix` 时只报告不修改。

**风险**

- 中。自动改写内容需要谨慎，默认关闭可降低风险。

---

## 二、中等价值改进（建议纳入下阶段规划）

### 6. AI 审查成本分层

**问题**

当前 `scripts/ai_review.py` 默认 `--model opus --effort max`，每期成本较高。但不同阶段对模型能力需求不同。

**建议分层**

| 阶段 | 建议模型 | 任务 |
|------|---------|------|
| 事实性/证据链机械校验 | haiku / sonnet | 检查 claim 是否都有 segment 证据、unsupported claim 是否进入发布稿 |
| 内容质量、风格、讲稿润色 | opus / max | 判断讲稿是否自然、是否遗漏重点 |
| 失败后的重审 | opus | 第一次审查未通过时，用最强模型深入分析 |

**实施内容**

1. 在 `ai_review.py` 增加 `--tiered-review` 参数。
2. 定义阶段函数：
   - `_run_evidence_audit()`：低成本，只做机械校验。
   - `_run_quality_audit()`：opus，做内容质量判断。
3. 默认仍保持 `--model opus --effort max`，但 `--tiered-review` 可作为日常批量处理选项。
4. 记录各阶段模型和 token 消耗到 `ai_review.json` 和 `run_report.json`。

**验收标准**

- 新增单元测试：mock 两个阶段，验证 `--tiered-review` 会依次调用并汇总结果。
- 证据校验阶段失败时，不进入高成本质量审查。

**风险**

- 中。需要确保分阶段结果与现有 `--model opus` 结果在质量上等价或更好。

---

### 7. TTS 失败后的降级策略

**问题**

当前 TTS 任一章节失败会「立即阻断」，且不会合并最终音频。这对长单集人工介入成本高。

**实施内容**

1. 增加 `--tts-fallback-chapter-split`：
   - 失败章节按自然段落拆成更短片段（如每 500 字）重试。
   - 如果拆分后仍失败，记录该章节 fingerprint 到 `tts_manifest.json` 的 `failed_chapters`。
2. 重跑 TTS 时：
   - 先检查 `failed_chapters`，只重试失败部分，避免全量重跑。
3. 对 Fish Audio 错误码分类处理：
   - 401：key 无效，立即失败。
   - 429：进入退避重试。
   - 5xx：重试 3 次后标记失败。
   - 内容过滤/安全拒绝：记录到质量报告，不反复重试。
4. 失败章节是否允许「跳过并生成不完整 MP3」：默认不允许，但增加 `--allow-partial-audio` 供人工确认后使用。

**验收标准**

- 新增单元测试：mock TTS 第一次失败，拆分后成功，验证最终 MP3 生成。
- 新增单元测试：429 场景验证退避重试。

**风险**

- 中。涉及音频合并逻辑，需确保跳过章节后时间戳/播放器章节索引正确。

---

### 8. 生成跨文件的 evidence manifest

**问题**

当前 evidence 校验是逐文件 SHA-256，缺少一个统领整个 revision 的清单文件。

**实施内容**

在 `process.py` 抓取/生成阶段写 `evidence_manifest.json`：

```json
{
  "revision_id": "uuid",
  "created_at": "2026-08-05T...",
  "files": {
    "transcript.raw.json": {
      "sha256": "...",
      "generator": "process.py fetch_transcript",
      "role": "transcript_source"
    },
    "content_map.json": {
      "sha256": "...",
      "generator": "content_map.py init",
      "role": "claim_evidence"
    },
    "讲书稿.md": {
      "sha256": "...",
      "generator": "manual or ai",
      "role": "briefing"
    }
  },
  "commands": [
    "scripts/process.py 'https://...' --name 'XXX'"
  ]
}
```

用途：

- `--force-refetch` 或重审时，用一个文件快速校验整个 revision 是否被意外改动。
- 与 `evidence_history/` 中的归档 revision 对应。

**验收标准**

- `process.py` 抓取/生成后，`evidence_manifest.json` 存在且所有受审文件条目完整。
- 新增单元测试：修改其中一个受审文件后，`evidence_manifest.json` 校验失败。

**风险**

- 低。新增文件，不影响现有流程。

---

### 9. 统一 source label 映射

**问题**

`episode.py` 的 `_source_label` 和 `catalog.py` 的 `_SOURCE_LABELS` 都维护了一份域名映射，未来新增来源需要改两处。

**实施内容**

1. 新增 `scripts/sources.py` 或合并到 `config.py`：
   ```python
   SOURCE_LABELS = {
       "podcasts.happyscribe.com": "HappyScribe",
       "happyscribe.com": "HappyScribe",
       "nav.al": "nav.al",
       "singjupost.com": "SingjuPost",
       "podscripts.co": "podscripts.co",
   }
   ```
2. `episode.py` 和 `catalog.py` 统一导入该映射。

**验收标准**

- 两处 `_source_label` 调用消失，统一使用共享映射。
- 新增来源时只需改一处。

**风险**

- 极低。

---

### 10. 配置校验前置

**问题**

`.env` 中的必填项（`FISH_KEY`、`R2_PUBLIC_URL`、`PAGES_PROJECT`、`HF_TOKEN` 等）当前是到具体阶段才报错。

**实施内容**

1. 在 `config.py` 增加 `validate_for_stage(stage_name)`：
   - `tts`：检查 `FISH_KEY`、`FISH_VOICE`、`FISH_MODEL`
   - `publish`：检查 `R2_PUBLIC_URL`、`R2_BUCKET`、`PAGES_PROJECT`、`PAGES_BASE_URL`
   - `diarize`：检查 `HF_TOKEN`
2. 在 `process.py`、TTS 入口、发布入口调用该校验，提前失败并给出清晰提示。

**验收标准**

- 新增单元测试：缺少 `FISH_KEY` 时，TTS 阶段启动前即失败。
- 错误信息明确告诉用户在 `.env` 中配置哪个 key。

**风险**

- 极低。

---

## 三、长期/战略性优化

### 11. 引入端到端集成测试

**问题**

当前单元测试覆盖了大量边界，但缺少一条完整的「URL → 转录 → content_map → 讲稿 → TTS → HTML → 发布」mock 流程。

**实施内容**

新增 `tests/test_end_to_end.py`，使用 `unittest.mock` 和固定 fixture：

1. mock `fetch_transcript_from_url` 返回短转录。
2. mock ASR 模型返回短 segment。
3. mock `run_tts` 返回 2KB 假 MP3。
4. mock AI review 返回通过结果。
5. mock R2 / Pages 验证返回成功。

重点验证：

- `--force-refetch` 会创建新 evidence revision 并归档旧版。
- 修改受审文件后 `ai_review.json` 按哈希规则失效。
- TTS 指纹缓存不会漏掉配置变更。
- 最终生成 `publish_report.json` 且 `passed` 为 true。

**验收标准**

- `python -m unittest tests/test_end_to_end.py` 通过。
- 测试不依赖外部 API、不写入真实 R2/Pages。

**风险**

- 低。纯测试补充。

---

### 12. 批量操作 CLI

**问题**

当前每次只能处理一个 `name`。如果一期播客是系列（如 10 期 Sam Harris），逐个跑很繁琐。

**实施内容**

新增 `scripts/batch.py`：

```bash
# 从列表文件批量处理到指定阶段
.venv/bin/python scripts/batch.py process list.txt --stage tts --max-workers 2

# 批量完成发布
.venv/bin/python scripts/catalog.py finish-all --dry-run
.venv/bin/python scripts/catalog.py finish-all
```

实现要点：

- 逐行读取名称列表，自动跳过已完成（根据 `publish_report.json` 或 `run_report.json`）。
- 支持 `--max-workers` 并行，默认 1，避免 API 并发超限。
- 失败单集记录到汇总报告，不中断后续单集。

**验收标准**

- 新增单元测试：构造 3 期 mock 数据，批量 finish 后验证每个单集都被调用一次。
- 失败单集不影响其他单集。

**风险**

- 中。并发运行需要配合 atomic I/O 和文件锁，否则可能互相覆盖 `site.json`。

---

### 13. 引入基于 cost/time 的 metrics dashboard

**问题**

`run_report.json` 已记录 duration，但没有汇总视图，难以判断优化投入方向。

**实施内容**

定期（如每周）生成 `reports/weekly_metrics.md`，统计：

- 每期各阶段耗时分布（P50 / P95）
- AI tokens / TTS 字符 / API 调用成本
- 人工介入次数（失败 → 成功）
- 高频失败原因 TOP5
- 来源域名失败率

实现方式：

1. 在 `run_report.json` 的 stage metrics 中补充 `cost_usd`（估算）和 `tokens`（若 API 返回）。
2. 新增 `scripts/metrics.py` 读取所有 run_report 并生成 Markdown。

**验收标准**

- 手动运行 `scripts/metrics.py` 能生成有效报告。
- 报告中能识别出耗时最长和失败率最高的阶段。

**风险**

- 低。只读已有日志。

---

### 14. 安全：API key 轮换与最小权限

**问题**

当前 `.env` 是主要 secret 来源，且部分日志可能泄漏 key 片段。TLS 降级路径虽已受控，但仍需持续审计。

**实施内容**

1. 统一 secret 读取收口到 `config._require`，并对 key 做 mask 日志处理：
   - 日志中只打印 `hf_***xxx` 形式。
2. 在 `fetcher.py` 的 TLS 降级路径中：
   - 只允许 `podscripts.co` 域名降级（当前已做）。
   - 降级事件必须进 `quality_report.json`（当前已做）。
   - 增加计数器，超过阈值（如 7 天内 >3 次）在 `catalog health` 中告警。
3. 文档中增加 `.env.example` 和密钥最小权限说明。

**验收标准**

- 新增单元测试：验证日志中不会出现完整 API key。
- `catalog health` 能汇总 TLS 降级次数。

**风险**

- 低。

---

## 四、明确不做的事

| 事项 | 原因 |
|------|------|
| 引入数据库替代文件系统 + JSON | 当前规模和审计需求下，文件系统更透明、更易 diff |
| 继续扩展 evidence v2 兼容逻辑 | v3 已经稳定，应坚定淘汰 v2，避免技术债 |
| 为核心流程加实时 Web UI | 当前 CLI + run_report 已够用，Web UI 增加维护负担 |
| 用复杂工作流引擎替代 shell 调用 | 当前脚本组合足够灵活，引入引擎会提高学习成本 |

---

## 五、推荐实施顺序

| 顺序 | 改进项 | 预计工作量 | 依赖 |
|------|--------|-----------|------|
| 1 | 清理 `.idea`/`.vscode` 索引 | 5 分钟 | 无 |
| 2 | 统一 source label 映射 | 30 分钟 | 无 |
| 3 | 配置校验前置 | 1 小时 | 无 |
| 4 | 补齐 atomic I/O 覆盖 | 3-4 小时 | 无 |
| 5 | API 调用重试与熔断 | 4 小时 | 依赖 4 的 atomic 写 |
| 6 | TTS 失败降级策略 | 4 小时 | 依赖 5 的重试 |
| 7 | 自动修复项留痕 | 2 小时 | 依赖 4 的 atomic 写 |
| 8 | evidence manifest | 2 小时 | 依赖 4 的 atomic 写 |
| 9 | run_report 聚合健康视图 | 3 小时 | 无 |
| 10 | AI 审查成本分层 | 4 小时 | 无 |
| 11 | 端到端集成测试 | 6 小时 | 依赖 4、5、6 |
| 12 | 批量操作 CLI | 4 小时 | 依赖 4、5 |
| 13 | metrics dashboard | 3 小时 | 依赖 9 |
| 14 | API key 安全加固 | 2 小时 | 无 |

---

## 六、验收总清单

实施上述任意一项后，都应检查：

- [ ] `python -m unittest discover -s tests -p 'test_pipeline.py' -v` 通过
- [ ] 如修改了写入逻辑，确认使用 `atomic_write_*`
- [ ] 如新增 CLI 命令，更新 `CLAUDE.md` 使用说明
- [ ] 如修改提示词或文档，同步更新对应 `.md` 文件
- [ ] 变更记录到 `run_report.json` 或新增审计文件
- [ ] 不影响现有发布流程和 evidence v3 校验逻辑

---

*本计划只描述应做事项，尚未对代码库做任何修改。*
