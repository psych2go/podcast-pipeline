import contextlib
import io
import os
import unittest
from unittest.mock import patch

from scripts.subagent import resolve_stage_strategy, print_stage_strategy


class StageStrategyTests(unittest.TestCase):
    def test_defaults_and_effort(self):
        with patch.dict(os.environ, {}, clear=True):
            strategy = resolve_stage_strategy('ai_review', effort='max')
        self.assertEqual(strategy['thinking'], 'low')
        self.assertIn('astra', strategy['resolved_model'])
        self.assertEqual(strategy['prompt_effort'], 'max')
        self.assertEqual(strategy['thinking_source'], 'default:low')

    def test_transcription_exempt(self):
        with patch.dict(os.environ, {}, clear=True):
            strategy = resolve_stage_strategy('transcript_correction_001')
        self.assertIsNone(strategy['resolved_model'])
        self.assertIsNone(strategy['thinking'])
        self.assertTrue(strategy['transcription_exempt'])

    def test_global_and_scoped(self):
        with patch.dict(os.environ, {'SUBAGENT_PI_THINKING': 'medium',
                                    'SUBAGENT_REVIEW_THINKING': 'high'}, clear=True):
            self.assertEqual(resolve_stage_strategy('ai_review')['thinking'], 'high')
            for task in ('claim_evidence', 'prewrite_fact_checks', 'transcript_correction'):
                self.assertEqual(resolve_stage_strategy(task)['thinking'], 'medium')
        for category, task in [('CLAIM', 'claim_evidence_001'),
                               ('FACT_CHECK', 'prewrite_fact_checks_001')]:
            with patch.dict(os.environ, {f'SUBAGENT_{category}_THINKING': 'high'}, clear=True):
                self.assertEqual(resolve_stage_strategy(task)['thinking'], 'high')
                self.assertEqual(resolve_stage_strategy('ai_review')['thinking'], 'low')

    def test_models(self):
        with patch.dict(os.environ, {'SUBAGENT_MODEL': 'inherited'}, clear=True):
            self.assertEqual(resolve_stage_strategy('ai_review')['model_source'], 'inherit')
            self.assertEqual(resolve_stage_strategy(
                'ai_review', model='explicit')['resolved_model'], 'openai-codex/explicit')

    def test_print_has_no_secrets(self):
        with patch.dict(os.environ, {
                'API_KEY': 'super-secret', 'SUBAGENT_MODEL': 'super-secret'}, clear=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                print_stage_strategy(resolve_stage_strategy('ai_review'))
        self.assertNotIn('super-secret', out.getvalue())
        self.assertIn('prompt_effort != thinking', out.getvalue())
