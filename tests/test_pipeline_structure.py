import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from scripts import process
from scripts.pipeline.cli import build_process_parser, episode_options_from_args
from scripts.pipeline.options import EpisodeOptions
from scripts.pipeline.stages import (
    STAGES,
    artifact_consumers,
    artifact_producers,
    stage_by_key,
    validate_stage_map,
)


class ProcessFacadeTests(unittest.TestCase):
    def test_episode_options_remains_reexported_from_process(self):
        self.assertIs(process.EpisodeOptions, EpisodeOptions)
        self.assertEqual(EpisodeOptions(html_only=True).mode, "html-only")

    def test_parser_and_options_conversion_preserve_cli_contract(self):
        parser = build_process_parser()
        args = parser.parse_args([
            "episode.mp3",
            "--name", "Episode",
            "--asr-quality", "max",
            "--asr-language", "auto",
            "--no-diarize",
            "--no-asr-refine",
            "--no-align",
            "--tts-speed", "1.15",
        ])
        options = episode_options_from_args(
            args,
            display_title="Episode",
            official_url="https://example.com/episode",
            diarize_audio=args.diarize and not args.no_diarize,
        )
        self.assertEqual(options.quality, "max")
        self.assertIsNone(options.asr_language)
        self.assertFalse(options.diarize_audio)
        self.assertFalse(options.adaptive_refinement)
        self.assertFalse(options.align_audio)
        self.assertEqual(options.tts_speed, 1.15)
        self.assertEqual(options.official_url, "https://example.com/episode")

    def test_direct_and_package_import_modes_keep_facade(self):
        # Direct script execution keeps working via the repo-root bootstrap.
        direct = subprocess.run(
            [sys.executable, "scripts/process.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)
        # Package mode resolves the facade to the same option class.
        package = (
            "import scripts.process as process; "
            "from scripts.pipeline.options import EpisodeOptions; "
            "assert process.EpisodeOptions is EpisodeOptions"
        )
        result = subprocess.run(
            [sys.executable, "-c", package],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class PipelineStageMapTests(unittest.TestCase):
    EXPECTED_KEYS = [
        "source-acquisition",
        "transcript-correction",
        "content-map",
        "claim-evidence",
        "canonical-entities",
        "prewrite-fact-checks",
        "content-writing",
        "content-finalization",
        "review-and-quality",
        "tts",
        "release-preparation",
        "reader-page",
        "release-and-publish",
    ]

    def test_stage_map_is_valid_and_ordered(self):
        self.assertEqual(validate_stage_map(), [])
        self.assertEqual([stage.key for stage in STAGES], self.EXPECTED_KEYS)
        self.assertEqual(
            stage_by_key("release-and-publish").public_entrypoint,
            "scripts/catalog.py finish / finish-batch",
        )

    def test_artifact_navigation_finds_producers_and_consumers(self):
        self.assertEqual(
            [stage.key for stage in artifact_producers("quality_report.json")],
            ["review-and-quality"],
        )
        self.assertEqual(
            [stage.key for stage in artifact_consumers("quality_report.json")],
            ["tts", "release-preparation", "reader-page", "release-and-publish"],
        )
        self.assertEqual(
            [stage.key for stage in artifact_producers("release.json")],
            ["release-preparation"],
        )
        self.assertEqual(
            [stage.key for stage in artifact_consumers("release.json")],
            ["reader-page", "release-and-publish"],
        )

    def test_map_order_matches_runtime_stage_tokens(self):
        agent_source = (ROOT / "scripts" / "agent_pipeline.py").read_text(
            encoding="utf-8")
        agent_tokens = [
            '"subagent_claim_evidence"',
            '"subagent_canonical_entities"',
            '"subagent_prewrite_fact_checks"',
            '"subagent_content_writing"',
            '"deterministic_evidence_enrichment"',
        ]
        positions = [agent_source.index(token) for token in agent_tokens]
        self.assertEqual(positions, sorted(positions))

        process_source = (ROOT / "scripts" / "process.py").read_text(
            encoding="utf-8")
        tts_position = process_source.index('with _stage(run_report, "tts")')
        release_position = process_source.index(
            'with _stage(run_report, "prepare_release")')
        html_position = process_source.index('with _stage(run_report, "html")')
        self.assertLess(tts_position, release_position)
        self.assertLess(release_position, html_position)

    def test_pipeline_map_cli_emits_machine_readable_graph(self):
        result = subprocess.run(
            [sys.executable, "scripts/pipeline_map.py", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            [stage["key"] for stage in payload["stages"]],
            self.EXPECTED_KEYS,
        )

    def test_documentation_covers_every_stage_and_ci_command(self):
        module_map = (ROOT / "docs" / "module-map.md").read_text(
            encoding="utf-8")
        for key in self.EXPECTED_KEYS:
            self.assertIn(f"`{key}`", module_map)
        tests_readme = (ROOT / "tests" / "README.md").read_text(
            encoding="utf-8")
        self.assertIn(
            "unittest discover -s tests -p 'test_*.py' -v",
            tests_readme,
        )


class PackagingContractTests(unittest.TestCase):
    """pyproject packaging stays consistent with requirements and imports."""

    @staticmethod
    def _dependency_name(requirement):
        requirement = str(requirement).split(";", 1)[0].strip()
        name = requirement.split("[", 1)[0]
        for marker in (">", "=", "<", "!", "~"):
            name = name.split(marker, 1)[0]
        name = name.strip()
        return name.lower()

    @staticmethod
    def _requirement_names(path):
        names = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                names.add(PackagingContractTests._dependency_name(line))
        return names

    def test_pyproject_dependencies_mirror_requirements(self):
        pyproject = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = {
            self._dependency_name(requirement)
            for requirement in pyproject["project"]["dependencies"]
        }
        required = self._requirement_names(ROOT / "requirements.txt")
        self.assertEqual(declared, required)

    def test_console_entry_points_import_their_modules(self):
        pyproject = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        entry_points = pyproject["project"]["scripts"]
        self.assertEqual(
            entry_points,
            {
                "podcast-process": "scripts.process:main",
                "podcast-catalog": "scripts.catalog:main",
            },
        )
        for module_name in ("scripts.process", "scripts.catalog"):
            result = subprocess.run(
                [sys.executable, "-c", f"import {module_name}"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
