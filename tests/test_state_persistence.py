"""Regressions for dashboard resets racing worker state writes."""
import contextlib
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard
import multi_sync
import workspace
from test_sync_plans import config_fixture


class StatePersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'sync_state.json'
        patch = mock.patch.object(workspace, 'STATE_PATH', self.path)
        patch.start()
        self.addCleanup(patch.stop)
        self.initial = {'version': 1, 'jobs': {
            'job_a': {'cursor': {'type': 'int', 'value': '10'}, 'table_id': 'root',
                      'partitions': [{'number': 2, 'table_id': 'child', 'table_name': '中文分表'}]},
            'other': {'cursor': {'type': 'int', 'value': '20'}},
        }}
        workspace.save_state(self.initial)
        self.client = dashboard.app.test_client()
        self.headers = {'Host': '127.0.0.1:5001', 'X-CSRF-Token': dashboard.CSRF_TOKEN}

    def reset(self, job_id):
        response = self.client.delete('/api/jobs/' + job_id + '/state', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['ok'])

    def test_reset_keeps_renamed_partition_bindings_and_success_history(self):
        self.initial['jobs']['job_a']['last_success_at'] = '2026-11-09T00:00:00+08:00'
        workspace.save_state(self.initial)
        self.reset('job_a')
        entry = workspace.load_state()['jobs']['job_a']
        self.assertNotIn('cursor', entry)
        self.assertTrue(entry['reset_pending'])
        self.assertEqual(entry['partitions'], self.initial['jobs']['job_a']['partitions'])
        self.assertEqual(entry['table_id'], 'root')
        self.assertEqual(entry['last_success_at'], self.initial['jobs']['job_a']['last_success_at'])

    def test_success_merges_other_jobs_and_preserves_their_resets(self):
        snapshot = workspace.load_state()
        self.reset('other')
        reset_entry = workspace.load_state()['jobs']['other']
        workspace.save_job_state(workspace.load_state(), 'new', {'cursor': {'type': 'int', 'value': '30'}})
        reset = workspace.save_job_state(snapshot, 'job_a', {'cursor': {'type': 'int', 'value': '11'}})
        self.assertFalse(reset)
        self.assertEqual(snapshot, workspace.load_state())
        self.assertEqual(snapshot['jobs']['other'], reset_entry)
        self.assertIn('new', snapshot['jobs'])
        self.assertEqual(snapshot['version'], 1)

    def test_reset_during_first_sync_or_existing_sync_remains_pending(self):
        for job_id in ('job_a', 'first_sync'):
            with self.subTest(job_id=job_id):
                snapshot = workspace.load_state()
                self.reset(job_id)
                token = workspace.load_state()['jobs'][job_id]['reset_token']
                new_entry = {'cursor': {'type': 'int', 'value': '12'}, 'table_id': 'root',
                             'partitions': [{'number': 2, 'table_id': 'new_child'}]}
                self.assertTrue(workspace.save_job_state(snapshot, job_id, new_entry))
                self.assertEqual(snapshot, workspace.load_state())
                saved = snapshot['jobs'][job_id]
                self.assertNotIn('cursor', saved)
                self.assertTrue(saved['reset_pending'])
                self.assertEqual(saved['reset_token'], token)
                self.assertEqual(saved['partitions'], new_entry['partitions'])
                self.assertIn('cursor', new_entry)
                # The next completed full scan can advance the cursor again.
                self.assertFalse(workspace.save_job_state(snapshot, job_id, new_entry))
                self.assertNotIn('reset_pending', snapshot['jobs'][job_id])
                self.assertEqual(snapshot['jobs'][job_id]['cursor'], new_entry['cursor'])

    def test_second_reset_while_full_rescan_runs_is_retained(self):
        self.reset('job_a')
        snapshot = workspace.load_state()
        self.reset('job_a')
        self.assertTrue(workspace.save_job_state(snapshot, 'job_a', {'cursor': {'type': 'int', 'value': '99'}}))
        self.assertNotIn('cursor', snapshot['jobs']['job_a'])

    def test_failed_atomic_save_leaves_disk_and_memory_unchanged(self):
        snapshot = workspace.load_state()
        before = copy.deepcopy(snapshot)
        disk_before = self.path.read_bytes()
        with mock.patch.object(workspace.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                workspace.save_job_state(snapshot, 'job_a', {'cursor': {'type': 'int', 'value': '99'}})
            response = self.client.delete('/api/jobs/job_a/state', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(snapshot, before)
        self.assertEqual(self.path.read_bytes(), disk_before)
        self.assertEqual(list(self.path.parent.glob('.sync_state.json.*')), [])

    def test_invalid_state_shape_reports_recovery_error(self):
        for value in ([], None, {'jobs': []}, {'jobs': {'job_a': None}}):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'sync_state.json 已损坏'):
                    workspace.load_state()

    def test_worker_saves_after_another_process_releases_state_lock(self):
        script = '''
import sys
from pathlib import Path
import workspace
workspace.STATE_PATH = Path(sys.argv[1])
snapshot = workspace.load_state()
print('ready', flush=True)
workspace.save_job_state(snapshot, 'job_a', {'cursor': {'type': 'int', 'value': '99'}})
'''
        with workspace.state_edit_lock():
            process = subprocess.Popen([sys.executable, '-c', script, str(self.path)],
                                       cwd=workspace.BASE_DIR, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            self.addCleanup(self.stop_process, process)
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with self.assertRaises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.2)
            # Simulate the dashboard's mutation while it owns the OS file lock.
            state = workspace.load_state()
            state['jobs']['job_a'].pop('cursor')
            state['jobs']['job_a'].update(reset_token='dashboard-reset', reset_pending=True)
            state['jobs']['new'] = {'table_id': 'new_target'}
            workspace.save_state(state)
        _, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, errors)
        state = workspace.load_state()
        self.assertNotIn('cursor', state['jobs']['job_a'])
        self.assertTrue(state['jobs']['job_a']['reset_pending'])
        self.assertIn('new', state['jobs'])

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def test_runner_refreshes_state_after_lock_and_before_each_job(self):
        config = config_fixture()
        other = copy.deepcopy(config['jobs'][0])
        other['id'] = 'other'
        config['jobs'].append(other)
        seen = []

        @contextlib.contextmanager
        def lock():
            self.reset('job_a')
            yield

        def run_job(client, token, config, state, job, dry_run):
            seen.append(job['id'])
            self.assertNotIn('cursor', state['jobs'][job['id']])
            self.assertTrue(state['jobs'][job['id']]['reset_pending'])
            if job['id'] == 'job_a':
                self.reset('other')
            return {'create': 0, 'update': 0, 'skip': 0}

        with mock.patch.object(multi_sync, 'load_config', return_value=config), \
             mock.patch.object(multi_sync, 'sync_lock', side_effect=lock), \
             mock.patch.object(multi_sync, 'feishu_client'), \
             mock.patch.object(multi_sync, 'resolve_app_token', return_value='app'), \
             mock.patch.object(multi_sync, 'configure_logging'), \
             mock.patch.object(multi_sync, 'run_job', side_effect=run_job):
            multi_sync.run()
        self.assertEqual(seen, ['job_a', 'other'])
