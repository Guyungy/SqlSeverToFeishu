import copy
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard
import multi_sync
import workspace


def config_fixture():
    return workspace.validate_config({
        'sources': [{'id': 'source_a', 'name': '测试库', 'server': 'localhost',
                     'database': 'Business', 'user': 'reader'}],
        'jobs': [{'id': 'job_a', 'source_id': 'source_a', 'schema': 'dbo', 'table': 'Orders',
                  'columns': [{'source': 'Id', 'target': 'Id', 'sql_type': 'int'}],
                  'unique_key': 'Id', 'target': {'table_name': 'Orders'}}],
    })


class PlanPersistenceTests(unittest.TestCase):
    def test_excluding_new_and_existing_tables_survives_reload(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(workspace, 'CONFIG_PATH', Path(folder) / 'sync_config.json'):
            workspace.save_config(config_fixture())
            headers = {'Host': '127.0.0.1:5001', 'X-CSRF-Token': dashboard.CSRF_TOKEN}
            tables = [{'source_id': 'source_a', 'schema': 'dbo', 'name': name, 'enabled': False}
                      for name in ['Orders', 'NeverSelected']]
            with mock.patch.object(dashboard, 'automatic_job') as automatic:
                response = dashboard.app.test_client().post('/api/jobs/bulk', json={'tables': tables}, headers=headers)
            self.assertEqual(response.status_code, 200)
            automatic.assert_not_called()
            config = workspace.load_config()
            self.assertFalse(config['jobs'][0]['enabled'])
            self.assertEqual({item['table'] for item in config['sources'][0]['excluded_tables']},
                             {'Orders', 'NeverSelected'})
            tables[0]['enabled'] = True
            response = dashboard.app.test_client().post('/api/jobs/bulk', json={'tables': tables[:1]}, headers=headers)
            self.assertEqual(response.status_code, 200)
            config = workspace.load_config()
            self.assertTrue(config['jobs'][0]['enabled'])
            self.assertEqual(config['sources'][0]['excluded_tables'], [{'schema': 'dbo', 'table': 'NeverSelected'}])

    def test_worker_binding_does_not_overwrite_plan_edits(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(workspace, 'CONFIG_PATH', Path(folder) / 'sync_config.json'):
            config = config_fixture()
            running_job = copy.deepcopy(config['jobs'][0])
            config['jobs'][0]['enabled'] = False
            config['jobs'][0]['name'] = '计划已修改'
            workspace.save_config(config)
            multi_sync.save_target_binding(running_job, 'tbl_new')
            saved = workspace.load_config()['jobs'][0]
            self.assertFalse(saved['enabled'])
            self.assertEqual(saved['name'], '计划已修改')
            self.assertEqual(saved['target']['table_id'], 'tbl_new')

    def test_worker_does_not_restore_deleted_job_or_changed_target(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(workspace, 'CONFIG_PATH', Path(folder) / 'sync_config.json'):
            config = config_fixture()
            running_job = copy.deepcopy(config['jobs'][0])
            config['jobs'][0]['target']['table_name'] = '新目标'
            workspace.save_config(config)
            multi_sync.save_target_binding(running_job, 'tbl_old')
            self.assertEqual(workspace.load_config()['jobs'][0]['target']['table_id'], '')
            config['jobs'] = []
            workspace.save_config(config)
            multi_sync.save_target_binding(running_job, 'tbl_old')
            self.assertEqual(workspace.load_config()['jobs'], [])


class CancellationTests(unittest.TestCase):
    def test_starting_worker_can_be_cancelled_before_process_exists(self):
        manager = dashboard.JobManager()
        manager.status = 'starting'
        manager.job_id = 'starting'
        self.assertTrue(manager.cancel()['ok'])
        process = mock.Mock(stdout=io.StringIO(''), returncode=-15)
        with mock.patch.object(dashboard.subprocess, 'Popen', return_value=process), \
             mock.patch.object(manager, '_stop_process') as stop:
            manager._run('starting', False, '')
        stop.assert_called_once_with(process)
        self.assertEqual(manager.snapshot()['status'], 'cancelled')

    def test_unresponsive_worker_is_killed_after_timeout(self):
        process = mock.Mock()
        process.wait.side_effect = subprocess.TimeoutExpired('worker', 5)
        dashboard.JobManager()._finish_stop(process)
        process.kill.assert_called_once()


class LargeSyncTests(unittest.TestCase):
    def run_batches(self, batches, records=(), fail_write=False):
        config = config_fixture()
        state = {'jobs': {}}
        job = config['jobs'][0]
        client = mock.Mock()
        client.list_tables.return_value = []
        client.iter_records.return_value = iter(records)
        if fail_write:
            client.batch_create.side_effect = RuntimeError('write failed')
        def fetch(source, job, saved_state, row_sink=None, settings=None):
            self.assertIsNotNone(row_sink)
            for batch in batches:
                row_sink(batch)
            return [], 123, False
        with mock.patch.object(multi_sync, 'ensure_target', return_value=('tbl', {'Id': 2}, 'existing')), \
             mock.patch.object(multi_sync, 'fetch_job_rows', side_effect=fetch), \
             mock.patch.object(multi_sync, 'save_state') as save_state:
            if fail_write:
                with self.assertRaisesRegex(RuntimeError, 'write failed'):
                    multi_sync.run_job(client, 'app', config, state, job, False)
                save_state.assert_not_called()
                self.assertEqual(state['jobs'], {})
                return client, None
            summary = multi_sync.run_job(client, 'app', config, state, job, False)
            save_state.assert_called_once()
        return client, summary

    def test_large_input_uses_bounded_write_batches(self):
        batches = ([{'Id': value} for value in range(start, start + 1000)] for start in range(0, 12000, 1000))
        client, summary = self.run_batches(batches)
        self.assertEqual(summary['create'], 12000)
        self.assertEqual(client.batch_create.call_count, 24)
        self.assertTrue(all(len(call.args[2]) <= multi_sync.BATCH_SIZE for call in client.batch_create.call_args_list))

    def test_updates_and_unchanged_records_match_existing_behavior(self):
        client, summary = self.run_batches([[{'Id': 1}, {'Id': 2}]],
            [{'record_id': 'existing', 'fields': {'Id': 1}}])
        self.assertEqual(summary['create'], 1)
        self.assertEqual(summary['skip'], 1)
        client.batch_update.assert_not_called()

    def test_write_failure_does_not_advance_cursor(self):
        self.run_batches([[{'Id': 1}]], fail_write=True)

    def test_duplicates_between_batches_fail_before_any_record_write(self):
        config = config_fixture()
        client = mock.Mock()
        client.list_tables.return_value = []
        def fetch(source, job, state, row_sink=None, settings=None):
            row_sink([{'Id': 1}])
            row_sink([{'Id': 1}])
        with mock.patch.object(multi_sync, 'ensure_target', return_value=('tbl', {'Id': 2}, 'existing')), \
             mock.patch.object(multi_sync, 'fetch_job_rows', side_effect=fetch):
            with self.assertRaisesRegex(multi_sync.ConfigError, '重复唯一键'):
                multi_sync.run_job(client, 'app', config, {'jobs': {}}, config['jobs'][0], False)
        client.batch_create.assert_not_called()
        client.batch_update.assert_not_called()
