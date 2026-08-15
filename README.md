[中文](README.md) | [English](README.en.md)

# Podcast Pipeline

把英文播客处理成可审计的中文完整笔记、中文讲稿、TTS 音频和移动端页面，并完整发布到 Cloudflare Pages + R2。

## 标准入口

```bash
# 处理单集
.venv/bin/python scripts/process.py "https://转录页面"

# 处理本地音频
.venv/bin/python scripts/process.py "episode.mp3" --name "播客名" --asr-quality max

# 完整发布
.venv/bin/python scripts/catalog.py finish "播客名"
.venv/bin/python scripts/catalog.py finish-batch "播客一" "播客二" "播客三"
```

`finish-batch` 始终执行完整事务：质量检查、R2 上传、站点生成、Pages 部署和远端验收。项目不提供脱离 Pages 的单独 R2 发布流程。

## 目录结构与公开范围

| 路径 | 用途 | 可公开推送 |
|---|---|---|
| `scripts/` | 流水线代码 | 是 |
| `tests/`、`.github/` | 测试和 CI | 是 |
| `examples/` | 脱敏后的页面与讲稿示例 | 是 |
| `docs/pipeline.md` | 当前技术架构 | 是 |
| `benchmarks/` | benchmark contract、人工参考和必要报告 | 是；媒体与生成结果除外 |
| `site/deploy.sh`、`site/wrangler.toml` | Cloudflare 部署配置 | 是 |
| `requirements*.txt`、`.env.example` | 依赖与无密钥配置模板 | 是 |
| `content/` | 每期原始转录、纠错稿、笔记、讲稿、审查、MP3 | **否** |
| `site/` 的其他内容 | 生成后的首页、单集页面和站点清单 | **否** |
| `reports/`、`.runlogs/` | 本地运行报告和日志 | **否** |
| `.env`、`.wrangler/`、`.venv*/`、本地 agent 配置 | 密钥、认证和机器状态 | **否** |

公开/私有边界由 `.gitignore`、`scripts/check_public_repo.py` 和 CI 共同约束。公开推送前运行：

```bash
.venv/bin/python scripts/check_public_repo.py
```

以 `private-` 或 `private/` 开头的本地分支可能含有历史私有内容，检查器会拒绝把它当成公开分支。无法从本地分支名或 GitHub Actions 环境确定来源的 detached HEAD 也会 fail closed。`--allow-private-branch` / `--allow-detached-head` 只用于人工检查当前索引，不代表历史适合公开。

## AI 与维护文档

- `AGENTS.md`：编码代理必须遵守的入口、私有数据和 Git 安全规则。
- `CLAUDE.md`：完整操作手册、质量门和故障恢复说明。
- `docs/pipeline.md`：内部模块和数据流技术说明。

普通任务不要直接拼接 `tts.py`、`html_gen.py`、`ai_review.py` 等内部阶段；应让 `process.py` 保证顺序、缓存失效和质量门。
