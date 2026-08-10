# ASR CPU/GPU Runtime Benchmark

日期：2026-08-07

## 环境

- GPU：NVIDIA GeForce GTX 1660 SUPER，6GB，compute capability 7.5。
- 驱动：591.86。
- CPU：AMD Ryzen 5 3500X，6 核。
- faster-whisper：1.2.1；CTranslate2：4.8.1。
- cuBLAS wheel：12.9.2.10；cuDNN wheel：9.24.0.43。
- 模型：`large-v3`。
- 音频：`Live in the Future - 原始音频.mp3`。
- 片段：120s 至 150s，共 30 秒。
- 解码：beam 5、英文、VAD 开启。
- CPU：`int8`。
- GPU：`int8_float16`。

## 最终串行结果

| 指标 | CPU | GPU |
|---|---:|---:|
| 模型加载 | 9.015s | 10.934s |
| 30 秒转录 | 45.531s | 5.642s |
| 总耗时 | 54.546s | 16.576s |
| 实时因子 | 1.5177 | 0.1881 |
| 进程峰值 RSS | 3285.5MB | 3541.8MB |
| GPU 峰值显存 | - | 3362MB |

GPU 的纯转录速度为 CPU 的 **8.07 倍**，总耗时为 **3.29 倍**。模型常驻后，
长音频主要受转录时间影响，因此整期收益更接近纯转录加速比。

CPU 与 GPU 均输出 82 个标准化单词，pairwise edit distance 为 1，
pairwise WER 为 0.0122；两个数字 `100`、`24` 均一致。该比较只能说明两种
运行时输出高度接近，不能替代人工参考稿 WER。

## 决策

- 默认配置改为 `ASR_DEVICE=auto`、`ASR_COMPUTE_TYPE=auto`。
- CUDA、cuBLAS、cuDNN 和目标 compute type 可用时，自动选择
  `cuda:int8_float16`。
- CUDA 不完整时自动选择 `cpu:int8`。
- 显式指定 CUDA 但环境不完整时直接报错，不静默改变用户配置。

原始机器可读结果：

- `reports/asr-runtime-large-v3-cpu-final.json`
- `reports/asr-runtime-large-v3-cuda-final.json`
