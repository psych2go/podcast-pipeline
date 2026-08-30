# 测试导航

## CI 等价命令

提交或合并前必须先运行公开边界检查，再运行完整测试发现：

```bash
.venv/bin/python scripts/check_public_repo.py
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

GitHub Actions 使用同样的完整 `test_*.py` discovery。下面的单文件命令只用于
开发时缩短反馈，不可替代完整测试。

## 按领域定位

| 领域 | 主要测试文件 |
|---|---|
| 主流程、抓取、TTS、HTML、质量门 | `test_pipeline.py` |
| pipeline 参数、共享 seam、结构合同 | `test_refactor_contracts.py`、`test_pipeline_structure.py` |
| 恢复、严格证据和边界 hardening | `test_pipeline_hardening.py`、`test_pipeline_reliability.py` |
| transcript completeness / correction manifest | `test_transcript_completeness.py` |
| 写作前事实核查与 exact entity repair | `test_prewrite_fact_checks.py` |
| AI review / repair / cache | `test_review_repair.py` |
| subagent 隔离和恢复 | `test_subagent_isolation.py`、`test_orchestration_recovery.py` |
| catalog / release / Pages / R2 | `test_release_flow.py` |
| ASR runtime、refinement、alignment、benchmark | `test_asr_*.py`、`test_diarization_runtime.py`、`test_prepare_ami_benchmark.py` |
| 浏览器布局 | `test_browser_layout.py` |
| 公开仓库和文档导航 | `test_repository_navigation.py` |
| 历史审计回归 | `test_claude_audit_regressions.py`、`test_glm_audit_regressions.py` |

## 常用定向命令

```bash
# pipeline facade、参数和模块地图
.venv/bin/python -m unittest discover -s tests -p 'test_pipeline_structure.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_refactor_contracts.py' -v

# 转录合同
.venv/bin/python -m unittest discover -s tests -p 'test_transcript_completeness.py' -v

# 发布事务
.venv/bin/python -m unittest discover -s tests -p 'test_release_flow.py' -v

# 浏览器布局
.venv/bin/python -m unittest discover -s tests -p 'test_browser_layout.py' -v
```

## 为什么暂不移动现有测试文件

当前测试通过 `Path(__file__)` 计算仓库根目录，并按顶层 `test_*.py` 进行 discovery。
直接把文件批量移动到 `tests/asr/`、`tests/review/` 等子目录会同时改变导入、fixture
路径和 discovery 行为。现阶段通过本导航表和模块地图降低查找成本；后续只有在统一
测试 bootstrap 后，才按领域逐个迁移。
