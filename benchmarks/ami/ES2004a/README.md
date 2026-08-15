# AMI ES2004a Benchmark

This directory defines a reproducible five-minute, four-speaker benchmark
from AMI meeting `ES2004a`. Source selection, license, URLs, hashes, and metric
definitions are documented in
`benchmarks/reports/multispeaker-sources.md`.

## Source Layout

Place the upstream artifacts at:

```text
audio/ES2004a.Mix-Headset.wav
source/ami_public_manual_1.6.2.zip
reference/ES2004a.rttm
reference/ES2004a.uem
```

`scripts/prepare_ami_benchmark.py` verifies their pinned SHA-256 values before
generating the 360-660 second clip, shifted references, and `manifest.json`.
Large source files, clipped audio, and generated benchmark runs are ignored by
Git.

## Workflow

The policy contract is `benchmarks/asr-policy.json`. It locks the production
presets, reference fingerprints, required model policies, recommendation, and
acceptance thresholds.

Run the fast contract check before changing ASR defaults or benchmark data:

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py check
```

Run the complete benchmark and acceptance gate:

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py run
```

After the first run, reuse the persisted community-1 diarization when
running cached ASR models. The cache is accepted only when the audio
fingerprint and speaker constraints match:

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py run \
  --reuse-shared-diarization
```

When only metric code changes, reuse existing hypotheses:

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py rescore
```

Validate an existing report without running inference:

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py verify
```

Results are written to:

```text
benchmarks/ami/ES2004a/results/AMI_ES2004a_360_660/
```

`report.json` records schema v2 input fingerprints and package versions.
`policy_verification.json` records the contract gate result.

The sample contains 751 manually transcribed words and four reference
speakers. AMI audio and manual annotations are licensed under CC BY 4.0.
