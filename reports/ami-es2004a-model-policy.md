# AMI ES2004a ASR Model Policy

Date: 2026-08-07

## Decision

- `balanced` defaults to `large-v3-turbo`.
- `max` remains `large-v3` as an explicit full-episode review mode.
- `--asr-model` and `ASR_MODEL` continue to override the preset.
- Shared community-1 exclusive diarization is computed once per benchmark
  run and reused across model policies.

On this reference sample, turbo was both more accurate and faster. Retaining
`large-v3` for `max` preserves a deliberately different review path until the
reference set covers more acoustic and language conditions.

## Sample

- Source: AMI meeting `ES2004a`, mixed headset signal.
- Clip: 360.000-660.000 seconds.
- Duration: 300 seconds.
- Reference: AMI manual annotations v1.6.2.
- Speakers: 4.
- Reference turns: 90.
- Reference words: 751, all assigned to a speaker turn.
- Primary scoring: zero collar, overlapping speech included.

Source URLs, pinned hashes, annotation parsing rules, and attribution are in
`reports/multispeaker-benchmark-sources.md`.

## Reproduction

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py check
.venv/bin/python scripts/asr_benchmark_workflow.py run
.venv/bin/python scripts/asr_benchmark_workflow.py verify
```

For model-only reruns after community-1 has been computed:

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py run \
  --reuse-shared-diarization
```

For metric-only changes:

```bash
.venv/bin/python scripts/asr_benchmark_workflow.py rescore
```

The machine-readable policy contract is `benchmarks/asr-policy.json`.
`report.json` uses schema v2 and records manifest, audio, reference
fingerprints, Python/platform details, and installed benchmark package
versions. Reused diarization is bound to the audio fingerprint and speaker
constraints.

The fair comparison below used cached model weights and reused the exact same
68 community-1 exclusive turns for both policies.

## Results

| Metric | large-v3 | large-v3-turbo |
| --- | ---: | ---: |
| End-to-end model wall time | 97.199 s | **51.281 s** |
| ASR time | 44.170 s | **21.580 s** |
| WhisperX alignment time | 30.332 s | **19.739 s** |
| Hypothesis words | 534 | **559** |
| cpWER | 38.22% | **36.09%** |
| Speaker-attributed WER | 38.22% | **36.09%** |
| Curated number recall | 71.43% | 71.43% |
| Entity recall | 60.00% | 60.00% |
| Alignment word coverage | **99.63%** | 99.46% |
| Word start MAE | 0.2211 s | **0.1605 s** |
| Word end MAE | 0.3061 s | **0.2187 s** |

Turbo reduced wall time by 47.24% (`1.895x` speedup) while improving cpWER and
speaker-attributed WER by 2.13 percentage points. It also produced lower word
timestamp error. These results satisfy the policy threshold without consuming
the allowed 2-point quality regression because turbo is the best-quality run.

## Diarization

community-1 detected four speakers and produced 68 exclusive turns.

| Condition | DER | JER |
| --- | ---: | ---: |
| Strict: zero collar, overlap included | 28.83% | 37.03% |
| Operational: 0.25 s collar, overlap excluded | 11.02% | 17.72% |

The strict result includes 72.003 seconds of missed reference speaker time.
Exclusive turns cannot represent simultaneous speakers, so overlap-inclusive
DER is intentionally harsher than the downstream podcast turn-assignment
condition. The strict score remains the primary regression metric.

## Prompt-Echo Finding

The first run used the benchmark label `AMI ES2004a multi-speaker meeting
clip` as decoding context. `large-v3` repeated `ES2004a, AMI` in multiple
segments, yielding only 174 hypothesis words and 84.29% cpWER.

Targeted re-decodes recovered plausible speech, but the candidate selector
rejected them because their length and token similarity differed too much
from the hallucinated original. Two fixes were applied:

1. Benchmark context now describes the audio content rather than injecting
   dataset identifiers.
2. Adaptive refinement recognizes a narrow prompt-echo pattern. A clean,
   higher-quality, plausible-rate candidate may bypass length and similarity
   gates only when the original is context-dominated, repetitive, and still
   high-risk.

The regression test keeps ordinary high-confidence but unrelated candidates
rejected.

## Limitations

This is one five-minute English meeting from a mixed-headset channel. It does
not yet cover remote microphone audio, music, two-person interviews, accented
speech, code-switching, or dense named entities. The default policy is
supported for the current workload, but future model changes should add
samples rather than replace this reference.
