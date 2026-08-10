# ASR Alignment And Diarization Smoke Test

日期：2026-08-07

## 输入

- 音频：`Live in the Future - 原始音频.mp3` 的 120s 至 135s。
- ASR：`large-v3`，`cuda:int8_float16`。
- Alignment：WhisperX 3.8.6，`WAV2VEC2_ASR_BASE_960H`，CPU。
- Diarization：`pyannote/speaker-diarization-community-1`，CPU。
- 说话人数约束：1。

## 结果

- ASR 输出 2 个 segment、41 个标准化单词。
- WhisperX 对齐 41/41 个词，时间戳覆盖率 100%。
- 对齐耗时 3.061 秒。
- 对齐前后转录词序完全一致。
- community-1 输出 3 个 exclusive turns。
- diarization 耗时 7.810 秒。
- speaker 合并后 2 个 segment 均归属 `SPEAKER_00`。

## 结论

生产顺序 `adaptive ASR -> forced alignment -> exclusive diarization` 已在真实音频
上完成端到端验证。该短样本只有一个说话人，只能验证模型加载、输出接口、
exclusive turns 和合并逻辑，不能替代多说话人参考集上的 DER/JER 基准。

WhisperX 隔离环境使用 `.venv-alignment`，只安装 WhisperX 和 NLTK，并通过
`.pth` 复用主 `.venv` 的 CPU Torch/torchaudio/transformers，不修改主 ASR
环境的 CUDA CTranslate2 依赖。

由于本机网络策略阻止 NLTK 在线下载 `punkt_tab`，adapter 在缺少该资源时将
每个 ASR segment 视为一个句子。CTC forced alignment 仍由 WhisperX 完成；
该回退会记录为 `sentence_splitter=segment_span_fallback`。
