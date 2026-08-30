# AI 操作入口

本仓库只有两个外部 interface。除非用户明确要求诊断或维护内部阶段，不要手工拼接流水线。

## 处理单集

```bash
.venv/bin/python scripts/process.py "SOURCE"
```

由 `process.py` 编排抓取/ASR、纠错、证据映射、中文笔记、讲书稿、审查、质量门、TTS 和 HTML。

## 完整发布

```bash
.venv/bin/python scripts/catalog.py finish "播客名"
.venv/bin/python scripts/catalog.py finish-batch "播客一" "播客二"
```

发布必须同时完成 R2、Pages 和远端验收。不要实现或使用 R2-only 路径。

## 私有数据边界

以下内容只能留在本地，禁止 `git add -f` 或公开推送：

- `content/` 中的转录、纠错稿、笔记、讲稿、审查文件和音频。
- `site/` 中除 `deploy.sh`、`wrangler.toml` 之外的生成页面和清单。
- `reports/`、`.runlogs/`、`.env`、`.wrangler/` 和本地 agent/虚拟环境状态。

公开的是代码、测试、当前文档、benchmark contract/人工参考，以及两份 Cloudflare 部署配置。提交前运行：

```bash
.venv/bin/python scripts/check_public_repo.py
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

`private-` 或 `private/` 分支可能在 Git 历史中包含播客内容，不得直接推送到公开远端。无法确认来源的 detached HEAD 同样不得公开推送。即使当前文件已经取消跟踪，也不能据此判断历史已脱敏。

## 强制规则

- 当前提示词只有 `scripts/纠错提示词.md` 和 `scripts/讲稿提示词.md`。
- 不要覆盖 `原始转录.txt`；纠错写入 `转录_纠错.txt`。
- 不要绕过 evidence、AI review、quality gate 或远端发布验收。
- 不要 reset、clean、批量删除或覆盖 `content/`、`site/` 中的本地成果。
- 工作区可能长期是 dirty 状态；只修改当前任务需要的文件，并检查精确 diff。

## 文档优先级

1. 当前用户指令。
2. 本文件的入口、私有数据和安全规则。
3. `docs/module-map.md` 的模块、owner 和 artifact 导航。
4. `CLAUDE.md` 的完整流程规则。
5. `docs/pipeline.md` 的详细实现说明。
