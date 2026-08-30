# Podcast Pipeline 模块与产物流转地图

> 本文是维护者的导航入口。业务细节和质量不变量见
> [`pipeline.md`](pipeline.md)，日常命令见 [`../README.md`](../README.md)，
> 测试入口见 [`../tests/README.md`](../tests/README.md)。

## 1. 稳定入口

只有以下命令属于普通用户入口：

```bash
.venv/bin/python scripts/process.py "SOURCE"
.venv/bin/python scripts/catalog.py finish "播客名"
.venv/bin/python scripts/catalog.py finish-batch "播客一" "播客二"
```

其他 `scripts/*.py` 命令均是内部维护、迁移、benchmark 或诊断接口。
`scripts/process.py` 和 `scripts/catalog.py` 必须长期保留为兼容 facade。

可用以下命令查看与本文对应的机器可读阶段图：

```bash
.venv/bin/python scripts/pipeline_map.py
.venv/bin/python scripts/pipeline_map.py --json
```

## 2. 代码区域

| 区域 | 主要职责 | 说明 |
|---|---|---|
| `scripts/process.py` | 单集 CLI facade 和端到端编排 | 对外路径稳定；参数对象和 parser 位于 `scripts/pipeline/` |
| `scripts/pipeline/` | 稳定参数合同、CLI 构造、阶段导航元数据 | 不替代运行时编排器 |
| `fetcher.py`、`episode.py`、`evidence.py` | 来源、ASR、episode 元数据和 evidence revision | `episode.json` 是单集元数据真源 |
| `agent_pipeline.py` | AI 内容阶段编排 | 负责纠错、content map、事实核查和写作顺序 |
| `content_map.py`、`claim_evidence.py` | source accountability 和逐 claim 证据 | 不负责外部事实纠正 |
| `prewrite_fact_checks.py` | 写作前原子事实核查和断点恢复 | 外部纠正保留在事实台账中 |
| `content_finalizer.py` | 讲稿、summary map、章节和 TTS 词典最终化 | AI review 前唯一允许的确定性写回阶段 |
| `ai_review.py`、`review_repair.py` | AI 终审和受限修复 | 修复后必须重新独立审查 |
| `quality_report.py`、`preflight.py` | 确定性质量门 | 结构、哈希、证据和新鲜度是主要约束 |
| `tts.py`、`html_gen.py`、`sections.py` | 音频与阅读页 | TTS 和 HTML 共用章节解析 seam |
| `catalog.py`、`catalog_*` | 发布事务 facade 与实现 | 已按 core/site/health/publish 拆分 |
| `release.py`、`publish.py` | release provenance 和远端验收 | Wrangler 成功不等于发布成功 |
| `atomic_io.py`、`hashing.py`、`retry.py`、`run_report.py` | 共享基础设施 | stage 名和报告字段属于持久合同 |

## 3. 阶段顺序

以下顺序来自实际 `process.py` / `agent_pipeline.py` / `catalog.py` 编排，
不是可以任意重新排列的脚本清单。

| 顺序 | Stage key | Owner | 主要输入 | 主要输出 |
|---:|---|---|---|---|
| 1 | `source-acquisition` | `fetcher.py`、`episode.py`、`evidence.py` | URL、MP3、转录文件 | `episode.json`、`来源.md`、`原始转录.txt`、`transcript.raw.json` |
| 2 | `transcript-correction` | `agent_pipeline.py`、`transcript_correction.py` | 原始 evidence | `correction_manifest.json`、`转录_纠错.txt` |
| 3 | `content-map` | `agent_pipeline.py`、`content_map.py` | 当前 evidence revision | `content_map.json` |
| 4 | `claim-evidence` | `claim_evidence.py` | content map、segment evidence | 更新后的 claim evidence、`claim_evidence_progress.json` |
| 5 | `canonical-entities` | `agent_pipeline.py`、`canonical_entities.py` | content map、纠错稿 | `canonical_entities.json` |
| 6 | `prewrite-fact-checks` | `prewrite_fact_checks.py` | source claims、实体、纠错稿 | `editorial_fact_checks.json`、批次和进度文件 |
| 7 | `content-writing` | `agent_pipeline.py` | content map、实体、事实台账 | `中文完整笔记.md`、`讲书稿.md`、`summary_map.json` |
| 8 | `content-finalization` | `content_finalizer.py` | 笔记、讲稿、summary map | 最终讲稿、最终 summary map、`tts_lexicon.json` |
| 9 | `review-and-quality` | `ai_review.py`、`review_repair.py`、`quality_report.py` | 全部内容和证据产物 | `ai_review.json`、`review_repair.json`、`quality_report.json` |
| 10 | `tts` | `tts.py` | 通过的质量报告和最终讲稿 | 章节音频、最终 MP3、`tts_manifest.json` |
| 11 | `release-preparation` | `process.py`、`release.py` | 最终 MP3、讲稿、Git provenance | `release.json` 和内容哈希音频 key |
| 12 | `reader-page` | `html_gen.py`、`episode.py` | 最终讲稿、episode、release | 使用 release 音频 key 的单集 HTML |
| 13 | `release-and-publish` | `catalog.py`、`publish.py` | release、质量、音频、页面 | 更新 release 状态、`publish_report.json`、Pages + R2 |

## 4. 关键 artifact 依赖

```text
episode.json
  └─ 单集 ID、slug、来源、质量和发布状态

transcript.raw.json + 原始转录.txt
  ├─ correction_manifest.json → 转录_纠错.txt
  └─ content_map.json
       └─ claim evidence
            └─ canonical_entities.json
                 └─ editorial_fact_checks.json
                      └─ 中文完整笔记.md + 讲书稿.md + summary_map.json
                           └─ ai_review.json + quality_report.json
                                └─ tts_manifest.json + MP3
                                     └─ release.json（先确定内容哈希音频 key）
                                          └─ 单集 HTML
                                               └─ Pages + R2 + publish_report.json
```

### 失效原则

- 新 evidence revision 会令 correction、content map、写作和 review 失效。
- `content_map.json` 的语义变化会令事实台账、写作和 review 失效。
- 纠错稿、实体表或事实台账变化会令写作输入绑定失效。
- 讲稿、笔记或 summary map 变化会令 AI review 和 quality report 失效。
- 最终朗读文本或 TTS 配置变化会令 TTS manifest 和音频缓存失效。
- 页面、音频或 release provenance 变化必须重新执行远端发布验收。

不要通过手工修改 hash、status 或 `passed` 字段绕过上述失效链。

## 5. 私有工作区边界

以下目录仍保留在仓库工作区中，但不属于公开 Git tree：

```text
.env
content/
site/ 生成内容
reports/
.runlogs/
.venv/
.venv-alignment/
```

本次模块整理不移动或重命名这些数据。公开边界由 `.gitignore`、
`scripts/check_public_repo.py` 和 GitHub Actions 共同验证。

## 6. 维护规则

1. 新功能先确定属于哪个 stage，再选择 owner 模块。
2. 不在 `process.py` 中新增纯参数 schema；稳定参数合同放入 `pipeline/options.py`。
3. 不在 `process.py` 中继续扩展大段 parser 定义；CLI 构造放入 `pipeline/cli.py`。
4. `pipeline/stages.py` 只描述导航，不可成为第二套运行时状态机。
5. 移动旧模块前必须保留原 import/CLI facade，并增加 package/direct-import 测试。
6. 不为“目录好看”批量移动 stateful 模块或测试；每次只拆一个有明确测试的 seam。
7. 修改阶段或 artifact 时，同时更新 `pipeline/stages.py`、本文和相关测试。
