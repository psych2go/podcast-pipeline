"""Prepare a clipped AMI meeting benchmark from official artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

try:
    from asr_benchmark import read_rttm
    from atomic_io import atomic_write_json, atomic_write_text
except ImportError:
    from scripts.asr_benchmark import read_rttm
    from scripts.atomic_io import atomic_write_json, atomic_write_text


EXPECTED_HASHES = {
    "audio/ES2004a.Mix-Headset.wav": (
        "3e2560b19bee6952c7c7ce041b0f1ea8"
        "a7ea9468044c4eea79d2a2c67e24ab0f"
    ),
    "source/ami_public_manual_1.6.2.zip": (
        "b56e5babb2496b8795deeeda7e71178d"
        "7fbc9963f94276cf2a3f4b56ebbc9f9d"
    ),
    "reference/ES2004a.rttm": (
        "9869c6146c2fd9595403edb36c2caeda"
        "65c12ffa2c0af4ce48d6814b673fd5a9"
    ),
    "reference/ES2004a.uem": (
        "514373eae2841ddf15fb07938a7d74bb"
        "ecc2acad8afc2a28a93e0f7b24bfb446"
    ),
}
NITE_ID = "{http://nite.sourceforge.net/}id"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sources(root):
    for relative, expected in EXPECTED_HASHES.items():
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(
                f"AMI artifact hash mismatch: {relative} "
                f"expected={expected} actual={actual}"
            )


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def meeting_speakers(archive, meeting_id):
    root = ET.fromstring(archive.read("corpusResources/meetings.xml"))
    for meeting in root:
        if meeting.attrib.get("observation") != meeting_id:
            continue
        return {
            speaker.attrib["nxt_agent"]: {
                "global_name": speaker.attrib["global_name"],
                "role": speaker.attrib.get("role"),
                "channel": speaker.attrib.get("channel"),
            }
            for speaker in meeting
            if _local_name(speaker.tag) == "speaker"
        }
    raise ValueError(f"AMI meeting metadata not found: {meeting_id}")


def meeting_words(archive, meeting_id, speakers):
    result = []
    for agent, speaker in speakers.items():
        member = f"words/{meeting_id}.{agent}.words.xml"
        root = ET.fromstring(archive.read(member))
        for element in root:
            if _local_name(element.tag) != "w":
                continue
            if element.attrib.get("punc") == "true":
                continue
            text = (element.text or "").strip()
            if not text:
                continue
            try:
                start = float(element.attrib["starttime"])
                end = float(element.attrib["endtime"])
            except (KeyError, ValueError):
                continue
            result.append({
                "id": element.attrib.get(NITE_ID),
                "speaker": speaker["global_name"],
                "agent": agent,
                "role": speaker.get("role"),
                "start": start,
                "end": end,
                "word": text,
            })
    return sorted(result, key=lambda item: (
        item["start"], item["end"], item["speaker"]))


def clip_turns(turns, start, end, uri):
    result = []
    for turn in turns:
        clipped_start = max(start, turn["start"])
        clipped_end = min(end, turn["end"])
        if clipped_end <= clipped_start:
            continue
        result.append({
            "uri": uri,
            "start": clipped_start - start,
            "end": clipped_end - start,
            "speaker": turn["speaker"],
        })
    return result


def reference_segments(turns, word_items, start, end, uri):
    clipped_words = [
        {
            **word,
            "start": word["start"] - start,
            "end": word["end"] - start,
        }
        for word in word_items
        if start <= (word["start"] + word["end"]) / 2 < end
    ]
    segments = []
    assigned = set()
    for turn in turns:
        matched = []
        for index, word in enumerate(clipped_words):
            if word["speaker"] != turn["speaker"]:
                continue
            midpoint = (word["start"] + word["end"]) / 2
            if turn["start"] - 0.02 <= midpoint <= turn["end"] + 0.02:
                matched.append((index, word))
        if not matched:
            continue
        assigned.update(index for index, _word in matched)
        words_payload = [
            {
                "word": word["word"],
                "start": round(max(0.0, word["start"]), 3),
                "end": round(min(end - start, word["end"]), 3),
            }
            for _index, word in matched
        ]
        segments.append({
            "uri": uri,
            "speaker": turn["speaker"],
            "start": round(turn["start"], 3),
            "end": round(turn["end"], 3),
            "text": " ".join(word["word"] for _index, word in matched),
            "words": words_payload,
        })
    lexical_words = [
        word for word in clipped_words
        if word["word"].strip()
    ]
    coverage = (
        len(assigned) / len(lexical_words)
        if lexical_words else 0.0
    )
    return segments, {
        "word_count": len(lexical_words),
        "assigned_words": len(assigned),
        "assignment_coverage": round(coverage, 4),
    }


def render_rttm(turns, uri):
    lines = []
    for turn in turns:
        duration = turn["end"] - turn["start"]
        lines.append(
            f"SPEAKER {uri} 1 {turn['start']:.3f} {duration:.3f} "
            f"<NA> <NA> {turn['speaker']} <NA> <NA>"
        )
    return "\n".join(lines) + "\n"


def prepare(root, start=360.0, duration=300.0):
    root = Path(root)
    verify_sources(root)
    meeting_id = "ES2004a"
    sample_id = f"AMI_{meeting_id}_{int(start)}_{int(start + duration)}"
    end = start + duration
    audio_source = root / "audio" / f"{meeting_id}.Mix-Headset.wav"
    clip_path = root / "audio" / (
        f"{meeting_id}.{int(start)}-{int(end)}.wav")
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(audio_source),
            "-t", f"{duration:.3f}",
            "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(clip_path),
        ],
        capture_output=True,
        check=True,
        timeout=900,
    )

    archive_path = root / "source" / "ami_public_manual_1.6.2.zip"
    with ZipFile(archive_path) as archive:
        speakers = meeting_speakers(archive, meeting_id)
        word_items = meeting_words(archive, meeting_id, speakers)
    original_turns = read_rttm(
        root / "reference" / f"{meeting_id}.rttm",
        uri=meeting_id,
    )
    turns = clip_turns(original_turns, start, end, sample_id)
    segments, reference_stats = reference_segments(
        turns,
        word_items,
        start,
        end,
        sample_id,
    )

    reference_json = root / "reference" / (
        f"{meeting_id}.{int(start)}-{int(end)}.segments.json")
    reference_rttm = root / "reference" / (
        f"{meeting_id}.{int(start)}-{int(end)}.rttm")
    reference_uem = root / "reference" / (
        f"{meeting_id}.{int(start)}-{int(end)}.uem")
    atomic_write_json(reference_json, {
        "schema_version": 1,
        "sample_id": sample_id,
        "source": "AMI manual annotations v1.6.2",
        "license": "CC BY 4.0",
        "clip": {
            "source_meeting": meeting_id,
            "start_seconds": start,
            "duration_seconds": duration,
        },
        "speakers": speakers,
        "stats": reference_stats,
        "segments": segments,
    })
    atomic_write_text(reference_rttm, render_rttm(turns, sample_id))
    atomic_write_text(
        reference_uem,
        f"{sample_id} 1 0.000 {duration:.3f}\n",
    )

    manifest = {
        "schema_version": 1,
        "id": sample_id,
        "uri": sample_id,
        "title": "Product design and finance meeting",
        "language": "en",
        "audio": str(clip_path.relative_to(root)),
        "reference": {
            "kind": "human_manual_ami_v1.6.2",
            "segments_json": str(reference_json.relative_to(root)),
            "rttm": str(reference_rttm.relative_to(root)),
            "uem": str(reference_uem.relative_to(root)),
        },
        "attribution": (
            "AMI Meeting Corpus, meeting ES2004a, audio and manual "
            "annotations, licensed under CC BY 4.0."
        ),
        "min_speakers": 4,
        "max_speakers": 4,
        "shared_diarization": True,
        "entities": [
            "T Rex",
            "crocodile",
            "vampire bat",
            "eagle",
            "seagull",
        ],
        "number_targets": [
            {
                "name": "25 EUR",
                "variants": [
                    "twenty five euros",
                    "twenty-five euros",
                    "25 euros",
                    "25 euro",
                    "€25",
                ],
            },
            {
                "name": "1.4 EUR/GBP",
                "variants": [
                    "one point four euro",
                    "1.4 euro",
                ],
            },
            {
                "name": "17 GBP",
                "variants": [
                    "seventeen pounds",
                    "17 pounds",
                    "£17",
                ],
            },
            {
                "name": "15",
                "variants": ["fifteen", "15"],
            },
            {
                "name": "12.50",
                "variants": ["twelve fifty", "12.50"],
            },
            {
                "name": "50 million EUR",
                "variants": [
                    "fifty million euros",
                    "50 million euros",
                    "50000000 euros",
                    "€50 million",
                ],
            },
        ],
        "evaluation": {
            "collar_seconds": 0.0,
            "skip_overlap": False,
            "regions": [[0.0, duration]],
        },
        "policies": [
            {
                "name": "large-v3",
                "model": "large-v3",
                "quality": "balanced",
                "adaptive_refinement": True,
                "align": True,
            },
            {
                "name": "large-v3-turbo",
                "model": "large-v3-turbo",
                "quality": "balanced",
                "adaptive_refinement": True,
                "align": True,
            },
        ],
        "policy_selection": {
            "max_cpwer_regression": 0.02,
            "max_speaker_attributed_wer_regression": 0.02,
            "min_speedup": 1.5,
        },
        "reference_stats": reference_stats,
    }
    manifest_path = root / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    return manifest_path, manifest


def main():
    parser = argparse.ArgumentParser(
        description="准备 AMI ES2004a 多说话人 benchmark")
    parser.add_argument(
        "--root",
        default="benchmarks/ami/ES2004a",
    )
    parser.add_argument("--start", type=float, default=360.0)
    parser.add_argument("--duration", type=float, default=300.0)
    args = parser.parse_args()
    path, manifest = prepare(
        args.root,
        start=args.start,
        duration=args.duration,
    )
    print(json.dumps({
        "manifest": str(path),
        "sample_id": manifest["id"],
        "reference_stats": manifest["reference_stats"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
