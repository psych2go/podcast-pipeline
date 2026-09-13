# 仓库目录与 GitHub 推送边界地图

> 本文回答两个问题：项目文件放在哪里，以及哪些文件可以推送到 GitHub。
> 目录梳理不改变运行入口，也不授权用 `git add -f` 绕过私有边界。

## 一眼看懂

```text
podcast-pipeline/                         # 仓库根目录
│
├── 📘 公开：项目说明与规则
│   ├── README.md / README.en.md           # 使用说明
│   ├── AGENTS.md                          # AI 操作入口与安全规则
│   ├── CLAUDE.md                          # 完整流程、质量门和维护规则
│   ├── LICENSE
│   └── docs/                              # 架构、模块、目录和维护文档
│
├── 🧠 公开：流水线实现
│   ├── scripts/                           # process/catalog 和内部阶段实现
│   │   └── pipeline/                      # 稳定参数、CLI、阶段导航元数据
│   ├── tests/                             # 单元、合同、回归测试
│   ├── examples/                          # 脱敏示例
│   └── .github/workflows/                 # CI 工作流
│
├── 🧪 公开：benchmark 合同（仅脱敏材料）
│   └── benchmarks/
│       ├── ami/                            # 人工参考/合同
│       ├── reports/                         # 必要的公开说明
│       └── audio/ source/ results/         # 私有/生成物，禁止推送
│
├── ☁️ 公开：部署入口（只有这两个文件）
│   ├── site/deploy.sh
│   └── site/wrangler.toml
│
├── 🔐 私有：单期工作成果（永不推送）
│   └── content/<单集名>/
│       ├── episode.json                    # 单集元数据
│       ├── 来源.md                         # 来源记录
│       ├── 原始转录.txt / transcript.raw.json
│       ├── 转录_纠错.txt / correction_manifest.json
│       ├── content_map.json                 # 证据地图
│       ├── claim_evidence*.json             # claim 证据
│       ├── canonical_entities.json          # 实体台账
│       ├── editorial_fact_checks*.json      # 事实核查台账
│       ├── 中文完整笔记.md / 讲书稿.md
│       ├── summary_map.json / tts_lexicon.json
│       ├── ai_review*.json / quality_report.json
│       ├── tts_manifest.json / release.json
│       ├── *.mp3                           # 音频
│       ├── *content.html                   # 生成阅读页
│       └── publish_report.json              # 发布验收记录
│
├── 🔐 私有：生成站点（除上面两个配置外永不推送）
│   └── site/
│       ├── site.json / index.html           # 生成首页与站点清单
│       ├── <storage-name>/                  # 兼容/本地生成页面
│       └── <slug>/                          # 发布页面与资产
│
├── 🔐 私有：运行和机器状态
│   ├── reports/                             # 健康、归因、诊断报告
│   ├── .runlogs/                            # 运行日志
│   ├── .env                                 # 密钥和本机配置
│   ├── .wrangler/ / site/.wrangler/        # Cloudflare 本地状态
│   ├── .venv/ / .venv-alignment/            # Python 环境
│   ├── .codex/ / .claude/ / .agents/        # agent 状态
│   └── __pycache__/ .mypy_cache/ .ruff_cache/
│
└── ⚙️ 根目录配置
    ├── .gitignore                           # 私有边界第一道防线
    ├── .env.example                         # 无密钥模板，可推送
    ├── pyproject.toml                       # 包、入口、ruff、mypy 配置
    └── requirements*.txt                   # 依赖锁定/安装入口，可推送
```

## 推送决策表

| 路径/类型 | GitHub | 说明 |
|---|---:|---|
| `scripts/`、`tests/`、`examples/` | ✅ 可以 | 代码、测试和脱敏样例 |
| `.github/`、`docs/` | ✅ 可以 | CI 和当前技术文档 |
| `README*`、`AGENTS.md`、`CLAUDE.md`、`LICENSE` | ✅ 可以 | 不得在其中写入密钥或私人内容 |
| `benchmarks/` 的 contract、人工参考、公开报告 | ✅ 可以 | 下载音频、原始 source、结果目录除外 |
| `.gitignore`、`.env.example`、`pyproject.toml`、`requirements*.txt` | ✅ 可以 | 模板可以公开，实际 `.env` 不可以 |
| `site/deploy.sh`、`site/wrangler.toml` | ✅ 可以 | 两个公开部署配置 |
| `content/` 全部内容 | ❌ 不可以 | 包括转录、笔记、讲稿、审查、音频、单集 HTML 和元数据 |
| `site/` 除两个配置外 | ❌ 不可以 | 包括首页、`site.json`、单集页面、清单和生成资产 |
| `reports/`、`.runlogs/` | ❌ 不可以 | 本地诊断和运行记录 |
| `.env`、API key、token、证书、`.pem`、`.key` | ❌ 不可以 | 机密信息 |
| `*.mp3`、`*.wav`、`*.flac` 等媒体 | ❌ 不可以 | 包括 benchmark 下载/生成媒体 |
| `.venv*`、`.wrangler/`、`.codex/`、`.claude/`、`.agents/`、缓存 | ❌ 不可以 | 本机状态或可重建文件 |

**重要：**`content/` 中的 `episode.json` 也属于私有内容；`site/site.json` 和生成的 HTML 也属于私有内容。即使文件不含明显转录文字，也不要单独强制加入。

## 正确的提交前流程

```bash
# 1. 查看精确变更
git status --short
git diff --stat
git diff --check

# 2. 检查公开边界（失败即停止）
.venv/bin/python scripts/check_public_repo.py

# 3. 运行静态检查和测试
.venv/bin/ruff check scripts tests
.venv/bin/mypy
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v

# 4. 只添加明确的公开路径
git add -- scripts tests docs .github examples \
  README.md README.en.md AGENTS.md CLAUDE.md LICENSE \
  pyproject.toml .env.example requirements*.txt \
  site/deploy.sh site/wrangler.toml benchmarks

git diff --cached --check
.venv/bin/python scripts/check_public_repo.py
```

不要使用：

```bash
git add -A
git add .
git add -f content/...
git add -f site/site.json
git add -f reports/...
```

如果 `git status` 显示 `content/`、生成的 `site/`、`reports/` 等被修改，不代表需要提交；它们是正常的本地工作成果。

## 当前目录与运行入口的关系

普通用户只使用两个入口：

```bash
.venv/bin/python scripts/process.py "SOURCE"
.venv/bin/python scripts/catalog.py finish "播客名"
.venv/bin/python scripts/catalog.py finish-batch "播客一" "播客二"
```

- `process.py` 读取来源并在 `content/` 生成单期成果。
- `catalog.py` 从 `content/` 读取已经通过质量门的成果，生成本地 `site/`，然后发布到 Cloudflare Pages + R2。
- `site/` 的生成页面不是源代码，不应作为 GitHub 版本资产。
- R2 和 Pages 上的公开内容不等于 GitHub 中可以公开的文件；本地仓库边界仍然优先遵守。

## 分支与历史安全

- 公开推送前确认当前分支来源；`private-`、`private/` 分支禁止直接推送。
- detached HEAD 无法确认来源时，检查器应 fail closed。
- “当前文件已被忽略”不能证明历史已脱敏；推送前仍应检查提交范围和远端分支。
- GitHub 主线受保护时，通过功能分支和 PR，不直接强推 `main`。

## 快速判断口诀

> **代码、测试、文档、脱敏合同、部署配置：可以推。**
> **节目内容、生成站点、运行报告、密钥、媒体、本机状态：不可以推。**
