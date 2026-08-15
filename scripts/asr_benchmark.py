"""Multi-speaker ASR benchmark metrics and manifest runner."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment
try:
    from text_distance import edit_details
except ImportError:
    from scripts.text_distance import edit_details


def words(text):
    return re.findall(
        r"[^\W_]+(?:['’-][^\W_]+)*",
        (text or "").lower(),
        flags=re.UNICODE,
    )


def numbers(text):
    return re.findall(
        r"(?<![a-z])\$?\d+(?:[,.]\d+)*(?:%|x|k|m|b)?",
        (text or "").lower(),
    )


def read_rttm(path, uri=None):
    turns = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 8 or fields[0] != "SPEAKER":
            continue
        if uri and fields[1] != uri:
            continue
        start = float(fields[3])
        duration = float(fields[4])
        turns.append({
            "uri": fields[1],
            "start": start,
            "end": start + duration,
            "speaker": fields[7],
        })
    return turns


def read_stm(path, uri=None):
    segments = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(";;"):
            continue
        fields = line.split(maxsplit=6)
        if len(fields) < 7:
            continue
        if uri and fields[0] != uri:
            continue
        text = fields[6]
        text = re.sub(r"^<[^>]*>\s*", "", text)
        if text == "ignore_time_segment_in_scoring":
            continue
        segments.append({
            "uri": fields[0],
            "channel": fields[1],
            "speaker": fields[2],
            "start": float(fields[3]),
            "end": float(fields[4]),
            "text": text,
        })
    return segments


def load_segments_json(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload.get("segments", [])
    if isinstance(payload, list):
        return payload
    raise ValueError(f"不支持的 segment JSON: {path}")


def segments_to_turns(segments):
    return [
        {
            "start": float(segment["start"]),
            "end": float(segment["end"]),
            "speaker": segment["speaker"],
        }
        for segment in segments
        if (
            segment.get("speaker")
            and isinstance(segment.get("start"), (int, float))
            and isinstance(segment.get("end"), (int, float))
            and segment["end"] > segment["start"]
        )
    ]


def normalize_turns(turns):
    """Normalize diarization adapter output to benchmark turn dictionaries."""
    normalized = []
    for turn in turns or []:
        if isinstance(turn, dict):
            start = turn.get("start")
            end = turn.get("end")
            speaker = turn.get("speaker")
            uri = turn.get("uri")
        elif isinstance(turn, (list, tuple)) and len(turn) >= 3:
            start, end, speaker = turn[:3]
            uri = None
        else:
            raise ValueError(f"不支持的 diarization turn: {turn!r}")
        if start is None or end is None or speaker is None:
            raise ValueError(f"diarization turn 缺少字段: {turn!r}")
        item = {
            "start": float(start),
            "end": float(end),
            "speaker": str(speaker),
        }
        if uri:
            item["uri"] = str(uri)
        normalized.append(item)
    return normalized


def _annotation(turns, uri="sample"):
    from pyannote.core import Annotation, Segment

    annotation = Annotation(uri=uri)
    for index, turn in enumerate(normalize_turns(turns)):
        annotation[
            Segment(float(turn["start"]), float(turn["end"])),
            index,
        ] = turn["speaker"]
    return annotation


def _timeline(regions):
    if not regions:
        return None
    from pyannote.core import Segment, Timeline

    return Timeline([
        Segment(float(start), float(end))
        for start, end in regions
    ])


def diarization_metrics(
        reference_turns,
        hypothesis_turns,
        *,
        collar=0.0,
        skip_overlap=False,
        regions=None,
        uri="sample",
):
    from pyannote.metrics.diarization import (
        DiarizationErrorRate,
        JaccardErrorRate,
    )

    reference = _annotation(reference_turns, uri=uri)
    hypothesis = _annotation(hypothesis_turns, uri=uri)
    uem = _timeline(regions)
    der_metric = DiarizationErrorRate(
        collar=collar,
        skip_overlap=skip_overlap,
    )
    details = der_metric(
        reference,
        hypothesis,
        uem=uem,
        detailed=True,
    )
    jer = JaccardErrorRate(
        collar=collar,
        skip_overlap=skip_overlap,
    )(reference, hypothesis, uem=uem)
    mapping = der_metric.optimal_mapping(
        reference,
        hypothesis,
        uem=uem,
    )
    return {
        "der": round(float(details["diarization error rate"]), 4),
        "jer": round(float(jer), 4),
        "missed_detection": round(
            float(details.get("missed detection", 0.0)), 4),
        "false_alarm": round(
            float(details.get("false alarm", 0.0)), 4),
        "confusion": round(float(details.get("confusion", 0.0)), 4),
        "total": round(float(details.get("total", 0.0)), 4),
        "collar_seconds": collar,
        "skip_overlap": skip_overlap,
        "speaker_mapping": {
            str(hypothesis_speaker): str(reference_speaker)
            for hypothesis_speaker, reference_speaker in mapping.items()
        },
    }


def speaker_text(segments):
    grouped = defaultdict(list)
    for segment in sorted(
            segments,
            key=lambda item: (
                float(item.get("start", 0.0) or 0.0),
                float(item.get("end", 0.0) or 0.0),
            )):
        speaker = segment.get("speaker")
        text = (segment.get("text") or "").strip()
        if speaker and text:
            grouped[str(speaker)].extend(words(text))
    return dict(grouped)


def _sum_edit_details(pairs, reference, hypothesis):
    totals = Counter()
    pair_details = []
    for reference_speaker, hypothesis_speaker in pairs:
        ref_words = (
            reference.get(reference_speaker, [])
            if reference_speaker is not None else []
        )
        hyp_words = (
            hypothesis.get(hypothesis_speaker, [])
            if hypothesis_speaker is not None else []
        )
        details = edit_details(ref_words, hyp_words)
        for key in (
                "errors",
                "reference_words",
                "hypothesis_words",
                "insertions",
                "deletions",
                "substitutions"):
            totals[key] += details[key]
        pair_details.append({
            "reference_speaker": reference_speaker,
            "hypothesis_speaker": hypothesis_speaker,
            **details,
        })
    reference_words = totals["reference_words"]
    return {
        **dict(totals),
        "wer": (
            round(totals["errors"] / reference_words, 4)
            if reference_words else None
        ),
        "assignment": pair_details,
    }


def speaker_attributed_wer(
        reference_segments,
        hypothesis_segments,
        speaker_mapping,
):
    """WER after applying the temporal diarization speaker mapping."""
    reference = speaker_text(reference_segments)
    hypothesis = speaker_text(hypothesis_segments)
    pairs = []
    mapped_hypothesis = set()
    for reference_speaker in reference:
        matching = [
            hypothesis_speaker
            for hypothesis_speaker, mapped_reference in speaker_mapping.items()
            if str(mapped_reference) == str(reference_speaker)
        ]
        hypothesis_speaker = matching[0] if matching else None
        if hypothesis_speaker is not None:
            mapped_hypothesis.add(str(hypothesis_speaker))
        pairs.append((reference_speaker, hypothesis_speaker))
    for hypothesis_speaker in hypothesis:
        if hypothesis_speaker not in mapped_hypothesis:
            pairs.append((None, hypothesis_speaker))
    return _sum_edit_details(pairs, reference, hypothesis)


def cp_word_error_rate(reference_segments, hypothesis_segments):
    """Concatenated minimum-permutation WER with Hungarian assignment."""
    reference = speaker_text(reference_segments)
    hypothesis = speaker_text(hypothesis_segments)
    reference_speakers = list(reference)
    hypothesis_speakers = list(hypothesis)
    size = max(len(reference_speakers), len(hypothesis_speakers))
    if size == 0:
        return {
            "errors": 0,
            "reference_words": 0,
            "hypothesis_words": 0,
            "insertions": 0,
            "deletions": 0,
            "substitutions": 0,
            "wer": 0.0,
            "assignment": [],
        }
    cost = np.zeros((size, size), dtype=int)
    for row in range(size):
        for column in range(size):
            reference_speaker = (
                reference_speakers[row]
                if row < len(reference_speakers) else None
            )
            hypothesis_speaker = (
                hypothesis_speakers[column]
                if column < len(hypothesis_speakers) else None
            )
            cost[row, column] = edit_details(
                reference.get(reference_speaker, []),
                hypothesis.get(hypothesis_speaker, []),
            )["errors"]
    rows, columns = linear_sum_assignment(cost)
    pairs = [
        (
            reference_speakers[row]
            if row < len(reference_speakers) else None,
            hypothesis_speakers[column]
            if column < len(hypothesis_speakers) else None,
        )
        for row, column in zip(rows, columns)
    ]
    return _sum_edit_details(pairs, reference, hypothesis)


def _normalized_phrase(text):
    text = (text or "").lower()
    text = text.replace("€", " euro ")
    text = text.replace("£", " pound ")
    text = text.replace("$", " dollar ")
    return " ".join(re.findall(
        r"\d+(?:[.,]\d+)*|[^\W_]+(?:['’-][^\W_]+)*",
        text,
        flags=re.UNICODE,
    ))


def _phrase_hits(normalized_text, phrase):
    normalized_phrase = _normalized_phrase(phrase)
    if not normalized_phrase:
        return 0
    return len(re.findall(
        r"(?<!\w)" + re.escape(normalized_phrase) + r"(?!\w)",
        normalized_text,
    ))


def _target_metrics(reference_text, hypothesis_text, targets):
    reference_normalized = _normalized_phrase(reference_text)
    hypothesis_normalized = _normalized_phrase(hypothesis_text)
    results = []
    for target in targets:
        if isinstance(target, str):
            name = target
            variants = [target]
        else:
            name = target.get("name") or target.get("value")
            variants = target.get("variants") or [name]
        variants = [variant for variant in variants if variant]
        if not name or not variants:
            continue
        reference_hits = max(
            _phrase_hits(reference_normalized, variant)
            for variant in variants
        )
        hypothesis_hits = max(
            _phrase_hits(hypothesis_normalized, variant)
            for variant in variants
        )
        results.append({
            "target": name,
            "variants": variants,
            "reference_hits": reference_hits,
            "hypothesis_hits": hypothesis_hits,
            "matched_hits": min(reference_hits, hypothesis_hits),
        })
    reference_hits = sum(item["reference_hits"] for item in results)
    hypothesis_hits = sum(item["hypothesis_hits"] for item in results)
    matched_hits = sum(item["matched_hits"] for item in results)
    return {
        "recall": (
            round(matched_hits / reference_hits, 4)
            if reference_hits else 1.0
        ),
        "precision": (
            round(matched_hits / hypothesis_hits, 4)
            if hypothesis_hits else (1.0 if not reference_hits else 0.0)
        ),
        "targets": results,
    }


def lexical_metrics(
        reference_segments,
        hypothesis_segments,
        entities=(),
        number_targets=(),
):
    reference_text = " ".join(
        segment.get("text", "") for segment in reference_segments)
    hypothesis_text = " ".join(
        segment.get("text", "") for segment in hypothesis_segments)
    reference_numbers = Counter(numbers(reference_text))
    hypothesis_numbers = Counter(numbers(hypothesis_text))
    matched_numbers = sum(
        (reference_numbers & hypothesis_numbers).values())
    detected_number_recall = (
        round(matched_numbers / sum(reference_numbers.values()), 4)
        if reference_numbers else 1.0
    )
    detected_number_precision = (
        round(matched_numbers / sum(hypothesis_numbers.values()), 4)
        if hypothesis_numbers else (
            1.0 if not reference_numbers else 0.0)
    )
    curated_numbers = _target_metrics(
        reference_text,
        hypothesis_text,
        number_targets,
    )

    entity_results = []
    matched_entities = 0
    hypothesis_entity_hits = 0
    for entity in entities:
        if not _normalized_phrase(entity):
            continue
        reference_hits = _phrase_hits(
            _normalized_phrase(reference_text), entity)
        hypothesis_hits = _phrase_hits(
            _normalized_phrase(hypothesis_text), entity)
        matched = min(reference_hits, hypothesis_hits)
        matched_entities += matched
        hypothesis_entity_hits += hypothesis_hits
        entity_results.append({
            "entity": entity,
            "reference_hits": reference_hits,
            "hypothesis_hits": hypothesis_hits,
            "matched_hits": matched,
        })
    reference_entity_hits = sum(
        item["reference_hits"] for item in entity_results)
    return {
        "number_metric": (
            "curated_targets" if number_targets else "detected_tokens"),
        "number_recall": (
            curated_numbers["recall"]
            if number_targets else detected_number_recall
        ),
        "number_precision": (
            curated_numbers["precision"]
            if number_targets else detected_number_precision
        ),
        "detected_number_recall": detected_number_recall,
        "detected_number_precision": detected_number_precision,
        "reference_numbers": sorted(reference_numbers.elements()),
        "hypothesis_numbers": sorted(hypothesis_numbers.elements()),
        "number_targets": curated_numbers["targets"],
        "entity_recall": (
            round(matched_entities / reference_entity_hits, 4)
            if reference_entity_hits else 1.0
        ),
        "entity_precision": (
            round(matched_entities / hypothesis_entity_hits, 4)
            if hypothesis_entity_hits else (
                1.0 if not reference_entity_hits else 0.0)
        ),
        "entities": entity_results,
    }


def _word_items(segments):
    items = []
    for segment in segments:
        for word in segment.get("words", []):
            token = words(word.get("word", ""))
            if (
                    len(token) == 1
                    and isinstance(word.get("start"), (int, float))
                    and isinstance(word.get("end"), (int, float))):
                items.append({
                    "token": token[0],
                    "start": float(word["start"]),
                    "end": float(word["end"]),
                })
    return items


def word_timestamp_metrics(reference_segments, hypothesis_segments):
    reference = _word_items(reference_segments)
    hypothesis = _word_items(hypothesis_segments)
    rows = len(reference) + 1
    columns = len(hypothesis) + 1
    cost = [[0] * columns for _ in range(rows)]
    path = [[None] * columns for _ in range(rows)]
    for i in range(1, rows):
        cost[i][0] = i
        path[i][0] = "delete"
    for j in range(1, columns):
        cost[0][j] = j
        path[0][j] = "insert"
    for i in range(1, rows):
        for j in range(1, columns):
            equal = reference[i - 1]["token"] == hypothesis[j - 1]["token"]
            candidates = [
                (cost[i - 1][j] + 1, "delete"),
                (cost[i][j - 1] + 1, "insert"),
                (cost[i - 1][j - 1] + (not equal), "match" if equal else "substitute"),
            ]
            cost[i][j], path[i][j] = min(candidates, key=lambda item: item[0])

    matched = []
    i, j = len(reference), len(hypothesis)
    while i or j:
        action = path[i][j]
        if action == "match":
            matched.append((reference[i - 1], hypothesis[j - 1]))
            i -= 1
            j -= 1
        elif action == "substitute":
            i -= 1
            j -= 1
        elif action == "delete":
            i -= 1
        elif action == "insert":
            j -= 1
        else:
            break
    matched.reverse()
    start_errors = [
        abs(reference_word["start"] - hypothesis_word["start"])
        for reference_word, hypothesis_word in matched
    ]
    end_errors = [
        abs(reference_word["end"] - hypothesis_word["end"])
        for reference_word, hypothesis_word in matched
    ]
    return {
        "reference_timestamped_words": len(reference),
        "hypothesis_timestamped_words": len(hypothesis),
        "matched_words": len(matched),
        "matched_reference_ratio": (
            round(len(matched) / len(reference), 4) if reference else 0.0
        ),
        "start_mae_seconds": (
            round(float(np.mean(start_errors)), 4)
            if start_errors else None
        ),
        "end_mae_seconds": (
            round(float(np.mean(end_errors)), 4)
            if end_errors else None
        ),
    }


def benchmark_sample(manifest, hypothesis_payload):
    reference = manifest["reference"]
    uri = manifest.get("uri") or manifest["id"]
    if reference.get("segments_json"):
        reference_segments = load_segments_json(reference["segments_json"])
    elif reference.get("stm"):
        reference_segments = read_stm(reference["stm"], uri=uri)
    else:
        raise ValueError("reference 需要 segments_json 或 stm")
    if reference.get("rttm"):
        reference_turns = read_rttm(reference["rttm"], uri=uri)
    else:
        reference_turns = segments_to_turns(reference_segments)

    hypothesis_segments = (
        hypothesis_payload.get("segments", [])
        if isinstance(hypothesis_payload, dict)
        else hypothesis_payload
    )
    hypothesis_turns = (
        hypothesis_payload.get("diarization_turns")
        if isinstance(hypothesis_payload, dict)
        else None
    )
    if hypothesis_turns is None:
        hypothesis_turns = segments_to_turns(hypothesis_segments)
    else:
        hypothesis_turns = normalize_turns(hypothesis_turns)
    evaluation = manifest.get("evaluation", {})
    diarization = diarization_metrics(
        reference_turns,
        hypothesis_turns,
        collar=float(evaluation.get("collar_seconds", 0.0)),
        skip_overlap=bool(evaluation.get("skip_overlap", False)),
        regions=evaluation.get("regions"),
        uri=uri,
    )
    mapping = diarization["speaker_mapping"]
    return {
        "schema_version": 1,
        "sample_id": manifest["id"],
        "reference_kind": reference.get("kind", "unknown"),
        "diarization": diarization,
        "speaker_attributed_wer": speaker_attributed_wer(
            reference_segments,
            hypothesis_segments,
            mapping,
        ),
        "cpwer": cp_word_error_rate(
            reference_segments,
            hypothesis_segments,
        ),
        "lexical": lexical_metrics(
            reference_segments,
            hypothesis_segments,
            entities=manifest.get("entities", []),
            number_targets=manifest.get("number_targets", []),
        ),
        "timestamps": word_timestamp_metrics(
            reference_segments,
            hypothesis_segments,
        ),
    }
