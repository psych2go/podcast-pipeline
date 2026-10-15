"""Deterministic regression tests; no provider or publishing network calls."""
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import catalog_publish, subagent, validator
from scripts.content_finalizer import (
    ContentFinalizationError, _realign_summary_to_titles,
    finalize_content_package, synchronize_summary_chapters,
)


def event(text=None, **extra):
    message = {"role": "assistant", "content": [] if text is None else [
        {"type": "text", "text": text}], **extra}
    return json.dumps({"type": "message_end", "message": message}, ensure_ascii=False)


class PiContractTests(unittest.TestCase):
    def test_extract_final_text_and_unicode_separators(self):
        text = 'hello\u2028world\u2029!'
        self.assertEqual(subagent._extract_pi_response(event(text)), text)
        extracted = subagent._extract_pi_response(event('first') + '\n' + event('last'))
        self.assertEqual(extracted, 'last')

    def test_extract_empty_event_stream_is_not_output(self):
        for output in ('{"type":"session"}', event(),
                       event('preamble') + '\n' + event(stopReason='error'),
                       event('preamble') + '\n' + event(stopReason='toolUse'),
                       event('text') + '\n{"type":"agent_settled","aborted":true}'):
            with self.subTest(output=output):
                self.assertEqual(subagent._extract_pi_response(output), '')

    def test_extract_ignores_nonobject_records_and_reads_agent_end(self):
        output = 'null\n[]\n' + json.dumps({"type": "agent_end", "messages": [
            None, {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]})
        self.assertEqual(subagent._extract_pi_response(output), 'ok')
        self.assertEqual(subagent._extract_pi_response('plain fallback'), 'plain fallback')

    def test_schema_checks_all_array_items(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        self.assertIn('$[100]', subagent._schema_error([1] * 100 + [True], schema))

    def test_schema_nullable_bounds_and_composition(self):
        self.assertIsNone(subagent._schema_error(None, {"type": ["string", "null"]}))
        for value in (-1, 101, float('nan'), True):
            self.assertIsNotNone(subagent._schema_error(value, {
                "type": "number", "minimum": 0, "maximum": 100}))
        self.assertIsNotNone(subagent._schema_error([], {"anyOf": [
            {"type": "object"}, {"type": "string"}]}))

    def test_schema_required_extra_and_nested(self):
        schema = {"type": "object", "required": ["x"], "additionalProperties": False,
                  "properties": {"x": {"type": "array", "minItems": 1,
                                       "items": {"type": "string", "enum": ["ok"]}}}}
        for value in ({}, {"x": []}, {"x": ["bad"]}, {"x": ["ok"], "y": 1}):
            self.assertIsNotNone(subagent._schema_error(value, schema))
        self.assertIsNone(subagent._schema_error({"x": ["ok"]}, schema))

    def test_pi_command_provider_model_and_schema_feedback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'SUBAGENT_PI_PROVIDER': 'openai-codex',
                'SUBAGENT_PI_MODEL': 'gpt-6.1-sol'}, clear=True):
            path = Path(tmp) / 'schema.json'
            path.write_text('{"type":"object"}')
            model = subagent._pi_default_model()
            self.assertEqual(model, 'openai-codex/gpt-6.1-sol')
            cmd = subagent._pi_command(['pi'], task='task', schema_path=path,
                                      write_files=False, enable_search=False,
                                      model=model, schema_feedback='missing x')
            self.assertEqual(cmd[cmd.index('--model') + 1], model)
            self.assertEqual(cmd[cmd.index('--mode') + 1], 'json')
            self.assertIn('--no-mcp', cmd)
            self.assertNotIn('bash', cmd[cmd.index('--tools') + 1])
            self.assertIn('missing x', cmd[-1])
            explicit = subagent._pi_command(['pi', '--model', model], task='task',
                schema_path=None, write_files=True, enable_search=False, model='ignored')
            self.assertEqual(explicit.count('--model'), 1)
            self.assertIn('edit', explicit[explicit.index('--tools') + 1])

    def test_missing_pi_binary_keeps_codex_fallback(self):
        with patch.dict(os.environ, {'SUBAGENT_COMMAND': 'pi',
                'SUBAGENT_FALLBACK_COMMAND': 'codex'}, clear=True), \
                patch.object(subagent.shutil, 'which',
                             side_effect=lambda name: '/bin/codex' if name == 'codex' else None):
            self.assertEqual(subagent._runner_commands(), [['/bin/codex', 'exec']])

    def test_explicit_model_wins_over_chain_and_global_model_starts_chain(self):
        with patch.dict(os.environ, {'SUBAGENT_MODEL_CHAIN': 'fallback,preferred',
                'SUBAGENT_MODEL': 'preferred', 'SUBAGENT_TEST_MODEL_CHAIN': 'scoped'}, clear=True):
            self.assertEqual(subagent._model_candidates('test', 'explicit'), ['explicit'])
            self.assertEqual(subagent._model_candidates('test', None), ['scoped'])
            self.assertEqual(subagent._model_candidates('other', None), ['preferred', 'fallback'])

    def test_pi_schema_retry_uses_string_schema_path(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'SUBAGENT_COMMAND': 'pi', 'SUBAGENT_MAX_RETRIES': '1'}, clear=True), \
                patch.object(subagent.shutil, 'which', return_value='/bin/pi'), \
                patch.object(subagent, '_runner_environment', return_value={}), \
                patch.object(subagent.time, 'sleep'), \
                patch.object(subagent, '_run_process', side_effect=[
                    SimpleNamespace(returncode=0, stdout=event('{"ok":"wrong"}'), stderr=''),
                    SimpleNamespace(returncode=0, stdout=event('{"ok":true}'), stderr=''),
                ]) as run:
            result = subagent.run_json_task(tmp, 'task', {
                'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                'required': ['ok']}, task_name='test')
            self.assertTrue(result['payload']['ok'])
            self.assertEqual(result['retry_count'], 1)
            self.assertIn('上一次输出', run.call_args.args[0][-1])

    def test_disabled_provider_schema_still_validates_pi_locally(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'SUBAGENT_COMMAND': 'pi', 'SUBAGENT_MAX_RETRIES': '0',
                'SUBAGENT_DISABLE_OUTPUT_SCHEMA': '1'}, clear=True), \
                patch.object(subagent.shutil, 'which', return_value='/bin/pi'), \
                patch.object(subagent, '_runner_environment', return_value={}), \
                patch.object(subagent, '_run_process', return_value=SimpleNamespace(
                    returncode=0, stdout=event('{"ok":"wrong"}'), stderr='')):
            with self.assertRaises(subagent.SubagentError) as raised:
                schema = {
                    'type': 'object',
                    'properties': {'ok': {'type': 'boolean'}},
                    'required': ['ok'],
                }
                subagent.run_json_task(tmp, 'task', schema, task_name='test')
            self.assertEqual(raised.exception.failure_metrics['failure_kind'], 'schema')

    def test_authentication_failure_keeps_codex_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'SUBAGENT_COMMAND': 'pi', 'SUBAGENT_FALLBACK_COMMAND': 'codex',
                'SUBAGENT_MAX_RETRIES': '0'}, clear=True), \
                patch.object(subagent.shutil, 'which', side_effect=lambda name: '/bin/' + name), \
                patch.object(subagent, '_runner_environment', return_value={}), \
                patch.object(subagent, '_runner_failure', return_value=('authentication', None)), \
                patch.object(subagent, '_run_process', side_effect=[
                    SimpleNamespace(returncode=1, stdout='', stderr=''),
                    SimpleNamespace(returncode=0, stdout='codex result', stderr='')]):
            self.assertEqual(subagent._run(tmp, 'task', task_name='test')['runner_index'], 1)

    def test_empty_pi_output_falls_back_to_codex(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'SUBAGENT_COMMAND': 'pi', 'SUBAGENT_FALLBACK_COMMAND': 'codex',
                'SUBAGENT_MAX_RETRIES': '0'}, clear=True), \
                patch.object(subagent.shutil, 'which', side_effect=lambda name: '/bin/' + name), \
                patch.object(subagent, '_runner_environment', return_value={}), \
                patch.object(subagent, '_run_process', side_effect=[
                    SimpleNamespace(returncode=0, stdout=event(), stderr=''),
                    SimpleNamespace(returncode=0, stdout='codex result', stderr=''),
                ]) as run:
            result = subagent._run(tmp, 'task', task_name='test')
            self.assertEqual(result['response'], 'codex result')
            self.assertEqual(result['runner_index'], 1)
            self.assertIn('exec', run.call_args.args[0])


class QualityRecoveryTests(unittest.TestCase):
    def test_nonreview_blockers_never_launch_automatic_review(self):
        from scripts.preflight import _review_recovery_decision
        from scripts.quality_errors import AI_REVIEW_MISSING, BRIEFING_AUDIT_NARRATION
        details = [{'code': AI_REVIEW_MISSING, 'message': 'missing'},
                   {'code': BRIEFING_AUDIT_NARRATION, 'message': 'audit narration'}]
        report = {'errors': ['missing', 'audit narration'], 'error_details': details}
        self.assertEqual(_review_recovery_decision(report), (True, False))
        report['errors'] = ['different error']
        self.assertEqual(_review_recovery_decision(report), (False, False))
        report = {'errors': ['missing'], 'error_details': details[:1]}
        self.assertEqual(_review_recovery_decision(report), (True, True))


class FinalizerReliabilityTests(unittest.TestCase):
    def chapters(self):
        return [{"title": title, "unit_ids": [f'U{i}'], "claim_ids": [f'claim-{i}'],
                 "body_sha256": "stale"} for i, title in enumerate(('A', 'B', 'C'))]

    def test_realign_contiguous_merge_preserves_all_claims_without_guessing_prefix(self):
        chapters = self.chapters()
        original = copy.deepcopy(chapters)
        result = _realign_summary_to_titles(chapters, ['A', 'C'])
        self.assertEqual(result[0]['claim_ids'], ['claim-0', 'claim-1'])
        self.assertEqual(result[1]['unit_ids'], ['U2'])
        self.assertNotIn('body_sha256', result[0])
        self.assertEqual(chapters, original)
        aligned = synchronize_summary_chapters('## A\nbody\n## C\nbody', {'chapters': chapters})
        self.assertEqual(len(aligned['chapters']), 2)

    def test_realign_refuses_unknown_split_reorder_and_empty_titles(self):
        for titles in (['A', 'new', 'C'], ['C', 'A'], [], ['A', 'A']):
            with self.subTest(titles=titles), self.assertRaises(ContentFinalizationError):
                _realign_summary_to_titles(self.chapters(), titles)

    def test_finalize_depth_and_audit_fail_before_writing(self):
        audit_leak = '本稿核查通过，按审查意见删除精确金额。' + '甲' * 550
        for body, error in [('甲' * 430, '章节深度'), (audit_leak, '审查过程语言')]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                text = '导览' * 30 + '\n\n## A\n\n' + body
                (folder / '讲书稿.md').write_text(text)
                summary = json.dumps({'chapters': self.chapters()[:1]})
                (folder / 'summary_map.json').write_text(summary)
                with self.assertRaisesRegex(ContentFinalizationError, error):
                    finalize_content_package(folder)
                self.assertEqual((folder / '讲书稿.md').read_text(), text)
                self.assertEqual((folder / 'summary_map.json').read_text(), summary)

    def test_audit_patterns_keep_domain_prose(self):
        for text in ('本稿核查通过。', '根据终审意见改为保守表达。', '见 ai_review.json。'):
            self.assertTrue(validator.audit_narration_issues(text))
        self.assertEqual(validator.audit_narration_issues('嘉宾介绍了机构审查结果及政策影响。'), [])


class BatchReliabilityTests(unittest.TestCase):
    def test_verify_exception_does_not_skip_other_episodes_or_claim_batch_success(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            items = [{"name": name, "folder": root / name, "mp3": root / (name + '.mp3'),
                      "key": name + '.mp3', "release": {"state": "prepared"}}
                     for name in ('one', 'two', 'three')]
            saved = {}
            for name in ('validate_for_stage', 'sync_site', 'rebuild_catalog', 'gen_index'):
                stack.enter_context(patch.object(catalog_publish, name))
            for name, value in {'_publish_preflight': True, '_candidate_catalog_errors': [],
                                'catalog_consistency_errors': [], '_upload_r2_item': True,
                                'update_release_state': {'state': 'deployed'},
                                '_release_report': {}, '_display_title': 'title',
                                'episode_page_path': 'page', 'public_audio_url': 'audio',
                                '_run_with_output': (True, '')}.items():
                stack.enter_context(patch.object(catalog_publish, name, return_value=value))
            stack.enter_context(patch.object(catalog_publish, 'R2_PUBLIC_URL', 'https://audio.example'))
            stack.enter_context(patch.object(
                catalog_publish, '_batch_publish_item', side_effect=items))
            verify_effects = [
                {'passed': True},
                RuntimeError('endpoint unavailable'),
                {'passed': True},
            ]
            verify = stack.enter_context(patch.object(
                catalog_publish,
                '_verify_publish_with_retry',
                side_effect=verify_effects,
            ))

            def _save_report(path, report):
                saved[path.parent.name] = copy.deepcopy(report)

            stack.enter_context(patch.object(
                catalog_publish, 'write_publish_report', side_effect=_save_report))
            self.assertFalse(catalog_publish._finish_batch_impl(['one', 'two', 'three']))
            self.assertEqual(verify.call_count, 3)
            self.assertEqual(saved['two']['failed_stage'], 'verify_publish')
            self.assertIn('RuntimeError', saved['two']['error'])
            self.assertTrue(saved['three']['passed'])
            for report in saved.values():
                self.assertFalse(report['batch']['passed'])
                self.assertEqual(report['batch']['episodes'], {
                    'one': 'published', 'two': 'failed', 'three': 'published'})


if __name__ == '__main__':
    unittest.main()
