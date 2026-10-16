import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.retry import RetryBudget
from scripts import subagent


class RetryBudgetTests(unittest.TestCase):
    def test_bound_and_reasons(self):
        budget = RetryBudget(2, scope='test')
        self.assertTrue(budget.consume('transport_timeout'))
        self.assertTrue(budget.consume('rate_limit'))
        self.assertFalse(budget.consume('empty_response'))
        self.assertEqual(budget.remaining, 0)
        self.assertTrue(budget.exhausted)
        self.assertEqual(budget.events[0]['reason'], 'transport_timeout')
        self.assertTrue(budget.events[0]['at'])

    def test_semantic_reject_is_not_transport_or_fallback(self):
        budget = RetryBudget(1, scope='test')
        self.assertFalse(budget.consume('semantic_reject'))
        self.assertEqual(budget.remaining, 1)
        self.assertFalse(subagent._model_fallback_allowed(subagent.SubagentError(
            'rejected', metrics={'failure_kind': 'semantic_reject'})))

    def test_semantic_runner_error_stops_immediately(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'SUBAGENT_RUNNER': 'pi', 'SUBAGENT_PI_PTY': 'never',
                'SUBAGENT_MAX_RETRIES': '2', 'SUBAGENT_MODEL_CHAIN': 'one,two'}, clear=True), \
                patch.object(subagent, '_runner_commands', return_value=[['pi'], ['codex']]), \
                patch.object(subagent, '_runner_environment', return_value={}), \
                patch.object(subagent, '_run_process', return_value=subprocess.CompletedProcess(
                    ['pi'], 1, '', '{"error":{"code":"semantic_reject"}}')) as run:
            with self.assertRaises(subagent.SubagentError) as raised:
                subagent._run(Path(tmp), 'task', task_name='ai_review')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(raised.exception.failure_metrics['failure_kind'], 'semantic_reject')
        self.assertFalse(raised.exception.failure_metrics['retry_events'][-1]['consumed'])

    def test_shared_call_budget_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'SUBAGENT_RUNNER': 'pi', 'SUBAGENT_PI_PTY': 'never',
                'SUBAGENT_MAX_RETRIES': '0', 'SUBAGENT_STAGE_CALL_BUDGET': '1',
                'SUBAGENT_MODEL_CHAIN': 'one,two'}, clear=True), \
                patch.object(subagent, '_runner_commands', return_value=[['pi']]), \
                patch.object(subagent, '_runner_environment', return_value={}), \
                patch.object(subagent, '_run_process', return_value=subprocess.CompletedProcess(
                    ['pi'], 0, '', '')) as run:
            with self.assertRaises(subagent.SubagentError) as raised:
                subagent._run(Path(tmp), 'task', task_name='ai_review')
        self.assertEqual(run.call_count, 1)
        metrics = raised.exception.failure_metrics
        self.assertEqual(metrics['failure_kind'], 'budget_exhausted')
        self.assertFalse(metrics['content_ok'])
        self.assertEqual(metrics['budget_remaining'], 0)
        self.assertEqual(metrics['retry_reason'], 'empty_response')
