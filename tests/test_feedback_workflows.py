import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard
import multi_sync
import sync
import workspace
from test_sync_plans import config_fixture
import test_split_sync


class IncrementalDefaultsTests(unittest.TestCase):
    def test_update_time_precedes_identity_and_creation_time(self):
        columns = [{'name': 'Id', 'sql_type': 'int', 'is_identity': True, 'identity_increment': 1},
                   {'name': 'CreatedAt', 'sql_type': 'datetime2'},
                   {'name': 'Updated_At', 'sql_type': 'datetime2'}]
        self.assertEqual(multi_sync.default_incremental_column(columns, 'Id'), 'Updated_At')

    def test_only_positive_identity_unique_key_is_inferred(self):
        column = {'name': 'Id', 'sql_type': 'int', 'is_identity': True, 'identity_increment': 1}
        self.assertEqual(multi_sync.default_incremental_column([column], 'Id'), 'Id')
        for change in ({'is_identity': False}, {'identity_increment': -1}, {'name': 'Other'}):
            self.assertEqual(multi_sync.default_incremental_column([{**column, **change}], 'Id'), '')

    def test_new_jobs_enable_inferred_incremental_except_content_keys(self):
        source = config_fixture()['sources'][0]
        columns = [{'name': 'Id', 'sql_type': 'int', 'nullable': False, 'is_identity': True, 'identity_increment': 1},
                   {'name': 'UpdatedAt', 'sql_type': 'datetime2', 'nullable': False}]
        for primary, enabled in [(['Id'], True), (['Id', 'UpdatedAt'], True), ([], False)]:
            with self.subTest(primary=primary), \
                 mock.patch.object(multi_sync, 'list_columns', return_value=columns), \
                 mock.patch.object(multi_sync, 'list_primary_key', return_value=primary):
                job = multi_sync.automatic_job(source, 'dbo', 'Orders')
                validated = workspace.validate_job(job, [source['id']])
                self.assertEqual(validated['incremental']['enabled'], enabled)
                if enabled:
                    self.assertEqual(validated['incremental']['column'], 'UpdatedAt')


class IncrementalLookupTests(unittest.TestCase):
    def setUp(self):
        self.config = config_fixture()
        self.job = self.config['jobs'][0]
        self.job['incremental'] = {'enabled': True, 'column': 'Id'}
        self.job['split']['row_limit'] = 40000
        self.job['target']['table_id'] = 'tbl'
        types = {'Id': 2}
        fingerprint = multi_sync.job_fingerprint(self.config['sources'][0], self.job, 'app', 'tbl', types,
                                                workspace.runtime_settings(self.config))
        self.state = {'jobs': {self.job['id']: {'fingerprint': fingerprint, 'table_id': 'tbl',
                      'cursor': multi_sync._encode_cursor(139999),
                      'partitions': [{'number': 1, 'table_id': 'tbl', 'table_name': 'Orders', 'rows': 140000}]}}}
        self.client = mock.Mock()
        self.client.list_tables.return_value = [{'table_id': 'tbl', 'name': '中文表名'}]
        self.client.record_count.return_value = 140000
        self.client.iter_records_by_keys.return_value = iter([{'record_id': 'old', 'fields': {'Id': 139999}}])

    def run_sync(self, rows, cursor=140000, types=None):
        def fetch(source, job, state, row_sink=None):
            row_sink(rows)
            return [], cursor, True
        with mock.patch.object(multi_sync, 'ensure_target', return_value=('tbl', types or {'Id': 2}, 'existing')), \
             mock.patch.object(multi_sync, 'fetch_job_rows', side_effect=fetch), \
             mock.patch.object(multi_sync, 'save_state'):
            return multi_sync.run_job(self.client, 'app', self.config, self.state, self.job, False)

    def test_two_changes_do_not_scan_140000_rows_and_still_split_at_capacity(self):
        # Allow a new partition without a real API write.
        self.client.iter_records.return_value = iter(())
        summary = self.run_sync([{'Id': 139999}, {'Id': 140000}])
        self.assertEqual(summary['lookup_mode'], 'keys')
        self.client.iter_records.assert_not_called()
        self.assertEqual(summary['skip'], 1)
        self.assertEqual(summary['create'], 1)
        self.assertEqual([part['rows'] for part in summary['partitions']], [140000, 1])
        self.assertEqual(self.client.iter_records_by_keys.call_args.args[-1], ['139999', '140000'])

    def test_missing_total_falls_back_to_complete_comparison(self):
        self.client.record_count.return_value = None
        self.client.iter_records.return_value = iter([{'record_id': 'old', 'fields': {'Id': 139999}}])
        summary = self.run_sync([{'Id': 139999}])
        self.assertEqual(summary['skip'], 1)
        self.client.iter_records.assert_called_once()
        self.client.iter_records_by_keys.assert_not_called()

    def test_empty_increment_keeps_cursor_and_skips_destination_reads(self):
        summary = self.run_sync([], cursor=None)
        self.assertEqual(summary['lookup_mode'], 'empty')
        self.assertEqual(summary['partitions'][0]['rows'], 140000)
        self.assertEqual(self.state['jobs'][self.job['id']]['cursor'], multi_sync._encode_cursor(139999))
        self.client.iter_records.assert_not_called()
        self.client.iter_records_by_keys.assert_not_called()
        self.client.record_count.assert_not_called()

    def test_text_keys_keep_trim_aware_full_comparison(self):
        self.client.iter_records.return_value = iter([{'record_id': 'old', 'fields': {'Id': ' 139999 '}}])
        summary = self.run_sync([{'Id': 139999}], types={'Id': 1})
        self.assertEqual(summary['create'], 0)
        self.client.record_count.assert_not_called()
        self.client.iter_records_by_keys.assert_not_called()

    def test_search_error_does_not_advance_successful_cursor(self):
        original = copy.deepcopy(self.state)
        self.client.iter_records_by_keys.side_effect = sync.FeishuAPIError('query failed')
        with self.assertRaisesRegex(sync.FeishuAPIError, 'query failed'):
            self.run_sync([{'Id': 140000}])
        self.assertEqual(self.state, original)
        self.client.batch_create.assert_not_called()


class RenamedPartitionsTests(unittest.TestCase):
    def test_renamed_root_and_child_keep_saved_ids(self):
        fixture = test_split_sync.SplitSyncTests()
        fixture.setUp()
        rows = fixture.rows(5)
        fixture.run_sync(rows)
        for index, table in enumerate(fixture.client.tables.values()):
            table['name'] = f'中文名称{index}'
        summary = fixture.run_sync(rows)
        self.assertEqual(summary['skip'], 5)
        self.assertEqual(summary['create'], 0)
        self.assertEqual(len(fixture.client.tables), 3)
        self.assertEqual([part['table_name'] for part in summary['partitions']], ['中文名称0', '中文名称1', '中文名称2'])


class KeySearchApiTests(unittest.TestCase):
    def setUp(self):
        self.client = sync.FeishuClient('test', 'test')

    def test_key_batches_paginate_and_deduplicate_search_results(self):
        with mock.patch.object(self.client, 'request', side_effect=[
            {'data': {'items': [{'record_id': 'one'}], 'has_more': True, 'page_token': 'next'}},
            {'data': {'items': [{'record_id': 'two'}]}},
            {'data': {'items': [{'record_id': 'two'}, {'record_id': 'three'}]}}
        ]) as request:
            records = list(self.client.iter_records_by_keys('app', 'tbl', ['Id'], 'Id', [str(i) for i in range(21)]))
        self.assertEqual([record['record_id'] for record in records], ['one', 'two', 'three'])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[1].kwargs['params']['page_token'], 'next')
        conditions = request.call_args_list[0].kwargs['json']['filter']['conditions']
        self.assertEqual(len(conditions), 20)
        self.assertEqual(conditions[0], {'field_name': 'Id', 'operator': 'is', 'value': ['0']})
        self.assertEqual(len(request.call_args_list[2].kwargs['json']['filter']['conditions']), 1)

    def test_malformed_pagination_fails_instead_of_losing_matches(self):
        with mock.patch.object(self.client, 'request', return_value={'data': {'has_more': True}}):
            with self.assertRaises(sync.FeishuAPIError):
                list(self.client.iter_records_by_keys('app', 'tbl', ['Id'], 'Id', ['1']))

    def test_pagination_cycle_is_rejected(self):
        with mock.patch.object(self.client, 'request', side_effect=[
            {'data': {'has_more': True, 'page_token': token}} for token in ['a', 'b', 'a']]):
            with self.assertRaises(sync.FeishuAPIError):
                list(self.client.iter_records_by_keys('app', 'tbl', ['Id'], 'Id', ['1']))

    def test_count_missing_or_invalid_is_not_assumed_zero(self):
        for total, expected in [(140000, 140000), (None, None), (True, None), (-1, None)]:
            with mock.patch.object(self.client, 'request', return_value={'data': {'total': total}}) as request:
                self.assertEqual(self.client.record_count('app', 'tbl', 'Id'), expected)
                self.assertEqual(request.call_args.kwargs['params']['page_size'], 1)


class BatchConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(workspace, 'CONFIG_PATH', Path(self.temp.name) / 'config.json')
        patch.start()
        self.addCleanup(patch.stop)
        config = config_fixture()
        job = config['jobs'][0]
        job['columns'].append({'source': 'UpdatedAt', 'target': '更新时间', 'sql_type': 'datetime2'})
        job['target']['table_id'] = 'tbl_saved'
        other = copy.deepcopy(job)
        other.update(id='job_b', table='Other', enabled=False)
        other['columns'] = other['columns'][:1]
        config['jobs'].append(other)
        workspace.save_config(config)
        self.headers = {'Host': '127.0.0.1:5001', 'X-CSRF-Token': dashboard.CSRF_TOKEN}
        self.client = dashboard.app.test_client()

    def post(self, **payload):
        return self.client.post('/api/jobs/incremental', json=payload, headers=self.headers)

    def test_custom_field_updates_compatible_jobs_and_reports_skipped(self):
        response = self.post(job_ids=['job_a', 'job_b'], mode='column', column='UpdatedAt')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual([job['id'] for job in body['updated']], ['job_a'])
        self.assertEqual([job['id'] for job in body['skipped']], ['job_b'])
        saved = workspace.load_config()['jobs']
        self.assertTrue(saved[0]['incremental']['enabled'])
        self.assertEqual(saved[0]['target']['table_id'], 'tbl_saved')
        self.assertFalse(saved[1]['enabled'])
        self.assertFalse(saved[1]['incremental']['enabled'])

    def test_auto_uses_timestamp_and_fetches_identity_metadata_for_other_jobs(self):
        columns = [{'name': 'Id', 'sql_type': 'int', 'is_identity': True, 'identity_increment': 1}]
        with mock.patch.object(dashboard, 'list_columns', return_value=columns) as metadata:
            response = self.post(job_ids=['job_a', 'job_b'], mode='auto')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['column'] for item in response.get_json()['updated']], ['UpdatedAt', 'Id'])
        metadata.assert_called_once()

    def test_disable_does_not_read_database_or_change_enabled_flags(self):
        with mock.patch.object(dashboard, 'list_columns') as metadata:
            response = self.post(job_ids=['job_a', 'job_b'], mode='disable')
        metadata.assert_not_called()
        self.assertEqual(len(response.get_json()['updated']), 2)
        self.assertEqual([job['enabled'] for job in workspace.load_config()['jobs']], [True, False])

    def test_missing_job_rejects_whole_request(self):
        original = workspace.load_config()
        response = self.post(job_ids=['job_a', 'missing'], mode='disable')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(workspace.load_config(), original)

    def test_run_selected_jobs_is_exact_and_rejects_disabled_or_empty_scope(self):
        with mock.patch.object(dashboard, 'resolve_app_token', return_value='app'), \
             mock.patch.object(dashboard.jobs, 'start', return_value={'ok': True}) as start:
            response = self.client.post('/api/run', json={'job_ids': ['job_a']}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            start.assert_called_once_with(False, sync_job_ids=['job_a'])
            for ids in ([], ['job_a', 'job_b'], ['missing']):
                start.reset_mock()
                response = self.client.post('/api/run', json={'job_ids': ids}, headers=self.headers)
                self.assertEqual(response.status_code, 400)
                start.assert_not_called()

    def test_worker_passes_each_selected_job_to_cli(self):
        manager = dashboard.JobManager()
        manager.job_id = 'run'
        process = mock.Mock(stdout=io.StringIO(''), returncode=0)
        with mock.patch.object(dashboard.subprocess, 'Popen', return_value=process) as popen:
            manager._run('run', False, '', ['job_a', 'job_b'])
        self.assertEqual(popen.call_args.args[0][-4:], ['--job-id', 'job_a', '--job-id', 'job_b'])

    def test_runner_does_not_run_existing_jobs_when_new_job_selected(self):
        config = workspace.load_config()
        config['jobs'][1]['enabled'] = True
        result = {'create': 0, 'update': 0, 'skip': 0}
        with mock.patch.object(multi_sync, 'load_config', return_value=config), \
             mock.patch.object(multi_sync, 'load_state', return_value={'jobs': {}}), \
             mock.patch.object(multi_sync, 'feishu_client'), \
             mock.patch.object(multi_sync, 'resolve_app_token', return_value='app'), \
             mock.patch.object(multi_sync, 'sync_lock'), \
             mock.patch.object(multi_sync, 'configure_logging'), \
             mock.patch.object(multi_sync, 'run_job', return_value=result) as run_job:
            multi_sync.run(job_ids=['job_b'])
        run_job.assert_called_once()
        self.assertEqual(run_job.call_args.args[4]['id'], 'job_b')

    def test_empty_runner_scope_cannot_execute_all_tasks(self):
        with mock.patch.object(multi_sync, 'configure_logging'), \
             mock.patch.object(multi_sync, 'run_job') as run_job:
            with self.assertRaises(multi_sync.ConfigError):
                multi_sync.run(job_ids=[])
        run_job.assert_not_called()
