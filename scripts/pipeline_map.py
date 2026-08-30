#!/usr/bin/env python3
"""Print the canonical pipeline stage and artifact map."""
import argparse
import json
import sys
from pathlib import Path

_scripts = str(Path(__file__).resolve().parent)
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

from pipeline.stages import STAGES, validate_stage_map


def _text_map() -> str:
    lines = []
    for index, stage in enumerate(STAGES, start=1):
        lines.append(f"{index:02d}. {stage.key} — {stage.title}")
        lines.append(f"    owners: {', '.join(stage.owners)}")
        lines.append(f"    inputs: {', '.join(stage.inputs)}")
        lines.append(f"    outputs: {', '.join(stage.outputs)}")
        if stage.updates:
            lines.append(f"    updates: {', '.join(stage.updates)}")
        if stage.public_entrypoint:
            lines.append(f"    entrypoint: {stage.public_entrypoint}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="显示 Podcast Pipeline 的阶段、owner 和 artifact 依赖",
    )
    parser.add_argument(
        "--json", action="store_true", help="输出机器可读 JSON",
    )
    args = parser.parse_args(argv)
    errors = validate_stage_map()
    if errors:
        for error in errors:
            print(f"[pipeline-map] {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(
            {"schema_version": 1, "stages": [stage.to_dict() for stage in STAGES]},
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(_text_map())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
