from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import catalog_publish as publish
from scripts.release import release_fingerprint


class FinishBatchRecoveryTests(unittest.TestCase):
    def setup_batch(self, stack, root):
        stack.enter_context(patch.object(publish, 'CONTENT_DIR', root))
        stack.enter_context(patch.object(publish, 'R2_PUBLIC_URL', 'https://audio.example'))
        for name in ('validate_for_stage', 'sync_site', 'rebuild_catalog', 'gen_index'):
            stack.enter_context(patch.object(publish, name))
        for name, value in {'_candidate_catalog_errors': [], 'catalog_consistency_errors': [],
                            '_display_title': 'title', 'episode_page_path': 'page',
                            'public_audio_url': 'audio',
                            '_run_with_output': (True, f'https://abc.{publish.PAGES_PROJECT}.pages.dev')}.items():
            stack.enter_context(patch.object(publish, name, return_value=value))
        preflight = stack.enter_context(patch.object(
            publish, '_publish_preflight', return_value=True))
        upload = stack.enter_context(patch.object(publish, '_upload_r2_item', return_value=True))
        items = {}
        for name in ('one', 'two'):
            folder = root / name
            folder.mkdir()
            items[name] = {'name': name, 'folder': folder, 'mp3': folder / 'audio.mp3',
                           'key': name, 'release': {}}
        stack.enter_context(patch.object(
            publish, '_batch_publish_item', side_effect=items.__getitem__))
        return preflight, upload

    def test_resume_rechecks_changed_release_and_all_episodes(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            preflight, upload = self.setup_batch(stack, root)
            verify = stack.enter_context(patch.object(publish, '_verify_publish_with_retry',
                side_effect=[RuntimeError('offline'), {'passed': True},
                             {'passed': True}, {'passed': False}]))
            self.assertFalse(publish.finish_batch(['one', 'two']))
            initial = json.loads((root / 'two' / 'publish_report.json').read_text())
            batch_id = initial['batch']['batch_id']
            self.assertTrue(initial['batch']['pages_deployed'])
            self.assertFalse(initial['batch']['full_success'])
            self.assertTrue(initial['batch']['pages_deploy_id'])
            (root / 'one' / 'release.json').write_text(json.dumps({
                'audio_sha256': 'changed', 'briefing_sha256': 'new'}))
            self.assertFalse(publish.finish_batch(['one', 'two'], resume_batch_id=batch_id))
            self.assertEqual(preflight.call_count, 4)
            self.assertEqual(upload.call_count, 4)
            self.assertEqual(verify.call_count, 4)
            final = json.loads((root / 'one' / 'publish_report.json').read_text())
            self.assertNotEqual(initial['batch']['candidate_release_fingerprints']['one'],
                                final['release_fingerprint'])
            self.assertFalse(final['batch']['full_success'])
            self.assertEqual(final['batch_id'], batch_id)

    def test_preflight_checks_every_episode(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            preflight, upload = self.setup_batch(stack, root)
            preflight.side_effect = [False, True]
            self.assertFalse(publish._finish_batch_impl(['one', 'two']))
            self.assertEqual(preflight.call_count, 2)
            upload.assert_not_called()
            report = json.loads((root / 'two' / 'publish_report.json').read_text())
            self.assertFalse(report['passed'])
            self.assertEqual(report['batch']['batch_status'], 'failed')

    def test_full_success_and_run_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            self.setup_batch(stack, root)
            verify = stack.enter_context(patch.object(
                publish, '_verify_publish_with_retry', return_value={'passed': True}))
            self.assertTrue(publish.finish_batch(['one', 'two']))
            self.assertEqual(verify.call_count, 2)
            for name in ('one', 'two'):
                report = json.loads((root / name / 'publish_report.json').read_text())
                self.assertTrue(report['batch']['full_success'])
                self.assertFalse(report['batch']['partial'])
                runs = json.loads((root / name / 'run_report.json').read_text())['runs']
                self.assertEqual(runs[-1]['metadata']['batch'], report['batch'])
            with self.assertRaises(ValueError):
                publish._finish_batch_impl(['one', 'two'], resume_batch_id='unrelated')
            self.assertEqual(verify.call_count, 2)

    def test_release_fingerprint_ignores_mutable_state(self):
        identity = {'audio_sha256': 'audio', 'briefing_sha256': 'briefing'}
        self.assertEqual(release_fingerprint(identity),
                         release_fingerprint(dict(identity, state='failed', updated_at='later')))
        self.assertNotEqual(release_fingerprint(identity),
                            release_fingerprint(dict(identity, audio_sha256='different')))
        self.assertEqual(release_fingerprint({}), '')
