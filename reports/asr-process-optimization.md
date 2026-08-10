# ASR 过程优化评估

日期：2026-08-07

## 实施状态

前两个实施阶段已于 2026-08-07 完成：

- [x] 新增 `asr_refinement.py` 深模块。
- [x] 自动从标题、音频名、已有来源信息和人工 hotwords 生成受限词表。
- [x] 基于置信度、压缩率、静音冲突、词概率和关键内容标记困难片段。
- [x] 相邻困难片段合并后，以 `max` 参数局部重解码。
- [x] 候选质量比较、失败回退和完整 attempt 审计。
- [x] refinement 指标接入 evidence provenance、run report 和 quality report。
- [x] CUDA 运行库安装、自动 runtime 选择与 CPU/GPU 基准。
- [x] WhisperX 强制对齐与内容漂移保护。
- [x] `community-1` exclusive diarization 接入与短音频 smoke。
- [x] 固定 AMI ES2004a 人工参考集和模型 policy 基准。
- [x] policy contract、输入指纹、缓存校验和统一 workflow 固化。

## 结论

当前流程的主要 ASR、对齐、说话人分离和模型 policy 已完成首轮人工参考校准。
本机默认 `balanced` 已切换为 `large-v3-turbo`，`max` 保留 `large-v3`。
下一阶段最值得做的是扩充参考集，而不是继续统一增大模型或 beam：

1. 增加双人访谈、远场音频、中英混合和专名密集参考样本。
2. 评测 batch adapter 及 6GB 显存下的 OOM 回退。
3. 接入 show notes、历史词表和首轮高置信实体反馈。

## 当前实现

### 已有能力

- `faster-whisper 1.2.1`、`CTranslate2 4.8.1`。
- `balanced` 使用 `large-v3-turbo`，`max` 使用 `large-v3`；两者均启用
  VAD、词级时间戳和抗幻觉阈值。
- `max` 使用温度回退、`no_repeat_ngram_size` 和更严格阈值。
- 默认关闭 `condition_on_previous_text`，能降低错误跨片段扩散。
- 可人工传入 `initial_prompt`、`hotwords`。
- 可选 pyannote 说话人分离，并保留 segment、word、置信度和 provenance。
- 质量报告已统计时间戳覆盖率和低置信片段比例。

### 第一阶段后的主要缺口

- 当前自动词表使用标题、音频名和已有来源信息，尚未接入网页 show notes、
  节目历史词表和首轮高置信实体反馈。
- 定向重解码发生在 diarization 之前，speaker 切换产生的新增风险当前进入
  quality report 待复核，但还不会触发第三轮解码。
- AMI 人工词时间戳已用于量化 MAE：turbo 的 start/end MAE 为
  0.1605/0.2187 秒。
- community-1 已在四人会议上计算严格 DER/JER：28.83%/37.03%；
  0.25 秒 collar 且忽略 overlap 时为 11.02%/17.72%。
- 质量门禁是整集比例，无法表达“关键数字或人名只有一处但错得很严重”。
- 基准工具只有 WER 和数字召回，尚无固定参考集、专名召回、时间对齐误差和说话人指标。

## 本机约束

- CPU：AMD Ryzen 5 3500X，6 核。
- 内存：8GB。
- GPU：GTX 1660 SUPER，6GB。
- 默认配置：`ASR_DEVICE=auto`、`ASR_COMPUTE_TYPE=auto`。
- 已安装 CUDA 12 cuBLAS 和 cuDNN 9 wheel，doctor 动态预加载通过。
- 自动策略在本机解析为 `cuda:int8_float16`；其他机器缺少 GPU 运行条件时
  回退 `cpu:int8`。

`large-v3` 对同一 30 秒片段的最终串行测试中，GPU 转录耗时 5.642 秒，
CPU 为 45.531 秒，GPU 纯转录加速 8.07 倍；GPU 峰值显存 3362MB。
CPU/GPU 输出 82 个标准化单词，仅 1 个 edit distance。完整结果见
`reports/asr-runtime-benchmark.md`。

## P0：优先实施

### 1. 困难片段二次解码

首轮保留当前 `balanced`，然后根据下列信号标记 `needs_redecode`：

- `avg_logprob` 低；
- `compression_ratio` 高；
- `no_speech_prob` 与实际文本冲突；
- 词置信度连续偏低；
- speaker 切换附近；
- 包含数字、货币、年份、URL 或疑似专有名词；
- 清洗改变了词序，导致 speaker 对齐为 `unresolved`。

只截取困难片段，携带节目级 prompt/hotwords 重跑 `large-v3/max`，再按
置信度、长度和词序相似度选择结果。阈值应由基准集校准，不应把示例值写成
永久常量。

每次整集处理生成新的 evidence revision，并为每个重解码区间记录：

- 原 segment 范围；
- 使用模型、参数、prompt 和 hotwords；
- 候选文本与选择原因；
- 替换前后 hash；
- 是否仍需人工复听。

### 2. 自动节目词表

第一阶段已增加 `build_asr_context`，当前从以下来源提取并去重：

- 标题、节目名、嘉宾名；
- 音频文件名；
- 已存在的 `来源.md`；
- 人工 `--hotwords`。

仍待接入：

- 来源页简介和 show notes；
- 已知公司、产品、人物和 URL 主域名；
- 同一节目的历史高频专名；
- 首轮转录中的高置信实体。

词表同时服务于 `initial_prompt` 和 `hotwords`。需要限制总长度和单词重复，避免 prompt 过长或把错误实体强化进解码。

### 3. 强制对齐 seam（已完成）

最终文字确定后，再经过独立 `Aligner` 生成词级时间戳；说话人归属使用对齐后的词，而不是 Whisper 原始词时间戳。

WhisperX 的官方项目说明其通过 wav2vec2 forced alignment 提供更准确的词级时间戳，并可结合 pyannote diarization。它适合作为可替换 adapter，不应把其 CLI 或数据结构泄漏到主流程接口中。

当前实现通过 `.venv-alignment` 子进程 adapter 隔离 WhisperX 依赖；主流程只
接收稳定 JSON。对齐结果必须与最终转录的标准化词序完全一致，否则拒绝替换。
真实 15 秒样本达到 41/41 词时间戳覆盖，详情见
`reports/asr-alignment-diarization-smoke.md`。

### 4. GPU smoke test 与性能基准（已完成）

安装 faster-whisper 官方要求的 CUDA 12 cuBLAS 和 cuDNN 9 运行库后，依次验证：

1. `device=cuda` 能加载 `medium`；
2. `large-v3` 能以 `int8_float16` 或设备支持的量化类型运行；
3. 5 分钟样本在 batch 1、4 下的耗时、峰值显存和文本指标；
4. OOM 能自动回退 batch 1 或 CPU，而不是中断整期流程。

GPU 主要解决吞吐和允许二次解码，不应被当作准确率提升本身。当前实测支持
`large-v3 + int8_float16`，但 batch 模式仍需单独评测后才能启用。

## P1：基准后实施

### 5. 模型分工（已完成首轮校准）

当前 policy：

- 默认 `balanced`：`large-v3-turbo`；
- 显式 `max` 复核：`large-v3`；
- 资源不足回退：`medium`。

AMI ES2004a 五分钟样本中，turbo 的 cpWER/SA-WER 为 36.09%，
`large-v3` 为 38.22%；端到端模型耗时分别为 51.281 秒和 97.199 秒。
因此默认策略切换为 turbo，完整结果见
`reports/ami-es2004a-model-policy.md`。

### 6. 升级说话人分离（已完成接入与多说话人基准）

评测 `pyannote/speaker-diarization-community-1`。官方模型卡显示它相对 3.1 改进了说话人数估计和归属，并提供 `exclusive_speaker_diarization`，后者更适合与非重叠 ASR 时间段对齐。

升级时必须适配 pyannote 4.x 输出接口，优先读取：

- `output.speaker_diarization`
- `output.exclusive_speaker_diarization`

不能只依赖当前的 `serialize()` / `itertracks()` 兼容分支。

### 7. 固定人工基准集

建立 10 至 20 分钟人工精校样本，至少覆盖：

- 单人清晰英语；
- 双人访谈和频繁打断；
- 多人圆桌；
- 中英混合、口音、背景音乐；
- 人名、公司、金额、百分比和年份密集片段。

指标至少包括：

- WER；
- 专有名词 recall / precision；
- 数字 recall / precision；
- 词时间戳平均绝对误差；
- speaker DER/JER 或 speaker-attributed WER；
- 实时因子、峰值内存和显存。

模型或阈值只有在参考集上不退化时才能成为默认值。

首个固定样本已经落地到 `benchmarks/ami/ES2004a`，包含 751 个参考词、
四位说话人、严格 DER/JER、speaker-attributed WER、cpWER、人工数字目标、
实体召回和词时间戳 MAE。`benchmarks/asr-policy.json` 固定生产 preset、
参考指纹、必测模型和验收阈值；`asr_benchmark_workflow.py` 提供
`check/run/rescore/verify` interface。二进制上游数据和生成结果不进入 Git。

### 8. 长音频批处理

faster-whisper 已提供 `BatchedInferencePipeline`，并在批处理路径默认结合 VAD。可在 GPU 基准通过后增加 batch adapter；对于本机 6GB 显存，应以自动探测和 OOM 回退为前提。

## P2：模块设计

建议将 ASR 深化为以下接口：

```text
AsrPipeline.transcribe(audio, policy, context) -> EvidenceRevision

AudioAnalyzer  -> 音频属性、声道、静音和异常
Recognizer     -> 初始 segment/word 候选
QualityPolicy  -> 困难片段与重解码决策
Aligner        -> 最终文字的词级强制对齐
Diarizer       -> speaker turns / exclusive turns
EvidenceMerger -> 候选选择、speaker 归属、revision provenance
```

主流程只依赖稳定的 `EvidenceRevision`，不直接依赖 faster-whisper、WhisperX 或 pyannote 的对象结构。这样可在不改写发布、纠错和讲稿模块的情况下替换识别器或对齐器。

## 推荐实施顺序

1. 建立参考集并扩展指标。
2. 做 CUDA smoke test，确定可用 compute type 和 batch。
3. 实现自动词表和困难片段标记。
4. 实现局部二次解码及 evidence revision。
5. 接入强制对齐 adapter。
6. 基准 `community-1` exclusive diarization。
7. 基准 `large-v3-turbo` 首轮 + `large-v3` 复核策略。

## 官方资料

- [faster-whisper README](https://github.com/SYSTRAN/faster-whisper)
- [CTranslate2 GPU support](https://opennmt.net/CTranslate2/installation.html#gpu-support)
- [CTranslate2 quantization](https://opennmt.net/CTranslate2/quantization.html)
- [OpenAI Whisper large-v3-turbo model card](https://huggingface.co/openai/whisper-large-v3-turbo)
- [WhisperX](https://github.com/m-bain/whisperX)
- [pyannote speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
