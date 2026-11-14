# AI 操作入口

本仓库只有两个外部 interface。默认通过这两个入口运行完整流程；只有在用户明确要求、诊断、测试或恢复时，才允许调用内部阶段。内部阶段不得替代 evidence、AI review、quality gate 或远端发布验收，也不得把中间产物当作最终产物。

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

公开的是代码、测试、当前文档、benchmark contract/人工参考，以及两份 Cloudflare 部署配置。提交前按修改范围运行分级验证：

- 文档、注释或非运行时配置：至少运行公开边界检查。
- 单模块运行时代码：运行该模块相关测试，并运行公开边界检查。
- 涉及 evidence、AI review、quality gate、发布、权限、私有数据边界或跨模块流程：必须运行公开边界检查和全量测试。
- 合并到 `main` 前，必须运行：

```bash
.venv/bin/python scripts/check_public_repo.py
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

`private-` 或 `private/` 分支可能在 Git 历史中包含播客内容，不得直接推送到公开远端。无法确认来源的 detached HEAD 同样不得公开推送。即使当前文件已经取消跟踪，也不能据此判断历史已脱敏。

## 强制规则

以下是不可因模型能力增强而放松的安全和结果约束；可以放宽的是执行路径，不是验收标准：

- 允许为诊断、测试或恢复调用内部阶段，但必须记录目的、保留失败状态，并继续经过完整质量门；不得用内部阶段绕过正式入口的安全检查。
- 模型可以自主选择兼容的 runner、模型链和重试策略，但必须受超时、调用预算、schema 校验和失败闭锁约束；模型失败时不得猜测、伪造或跳过结果。
- 当前提示词只有 `scripts/纠错提示词.md` 和 `scripts/讲稿提示词.md`。
- 不要覆盖 `原始转录.txt`；纠错写入 `转录_纠错.txt`。
- 不要绕过 evidence、AI review、quality gate 或远端发布验收。
- 不要 reset、clean、批量删除或覆盖 `content/`、`site/` 中的本地成果。
- 工作区可能长期是 dirty 状态；只修改当前任务需要的文件，并检查精确 diff。

## 文档优先级

1. 当前用户指令。
2. 本文件的入口、私有数据和安全规则。
3. `docs/repository-layout.md` 的目录树和 GitHub 推送边界。
4. `docs/module-map.md` 的模块、owner 和 artifact 导航。
5. `CLAUDE.md` 的完整流程规则。
6. `docs/pipeline.md` 的详细实现说明。
