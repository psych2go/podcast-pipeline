"""Isolated WhisperX forced-alignment process."""
import argparse
import json
import time
from pathlib import Path


def _overlap(a_start, a_end, b_start, b_end):
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _source_index(item, source):
    start = float(item.get("start", 0.0) or 0.0)
    end = float(item.get("end", start) or start)
    midpoint = (start + end) / 2
    best_index = 0
    best_overlap = -1.0
    best_distance = float("inf")
    for index, segment in enumerate(source):
        source_start = float(segment.get("start", 0.0) or 0.0)
        source_end = float(segment.get("end", source_start) or source_start)
        overlap = _overlap(start, end, source_start, source_end)
        distance = abs(midpoint - (source_start + source_end) / 2)
        if overlap > best_overlap or (
                overlap == best_overlap and distance < best_distance):
            best_index = index
            best_overlap = overlap
            best_distance = distance
    return best_index


def main():
    parser = argparse.ArgumentParser(description="WhisperX alignment adapter")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    import nltk
    import whisperx
    from whisperx import alignment as alignment_module

    sentence_splitter = "nltk_punkt"
    try:
        nltk.data.find("tokenizers/punkt_tab/english/")
    except LookupError:
        class SegmentSpanTokenizer:
            @staticmethod
            def span_tokenize(text):
                return [(0, len(text))] if text else []

        alignment_module.nltk_load = lambda _path: SegmentSpanTokenizer()
        sentence_splitter = "segment_span_fallback"

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    source = payload.get("segments", [])
    transcript = [
        {
            "start": float(segment.get("start", 0.0) or 0.0),
            "end": float(segment.get("end", 0.0) or 0.0),
            "text": segment.get("text", ""),
            **(
                {"avg_logprob": segment["avg_logprob"]}
                if segment.get("avg_logprob") is not None else {}
            ),
        }
        for segment in source
        if (segment.get("text") or "").strip()
    ]
    started = time.time()
    model, metadata = whisperx.load_align_model(
        language_code=args.language,
        device=args.device,
        model_name=args.model,
    )
    resolved_model = args.model
    if not resolved_model:
        resolved_model = (
            alignment_module.DEFAULT_ALIGN_MODELS_TORCH.get(args.language)
            or alignment_module.DEFAULT_ALIGN_MODELS_HF.get(args.language)
            or metadata.get("type")
        )
    aligned = whisperx.align(
        transcript,
        model,
        metadata,
        args.audio,
        args.device,
        return_char_alignments=False,
    )
    segments = []
    for segment in aligned.get("segments", []):
        item = dict(segment)
        item.pop("chars", None)
        item["source_index"] = _source_index(item, source)
        segments.append(item)
    result = {
        "segments": segments,
        "meta": {
            "adapter": "whisperx",
            "model": resolved_model,
            "device": args.device,
            "language": args.language,
            "sentence_splitter": sentence_splitter,
            "elapsed_seconds": round(time.time() - started, 3),
        },
    }
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
