import copy
import unittest
from datetime import datetime, timedelta
from unittest import mock

import multi_sync
import workspace


class FakeFeishu:
    def __init__(self):
        self.tables = {}
        self.records = {}
        self.batch_count = 0
        self.fail_on_batch = None

    def list_tables(self, app_token):
        return [{'table_id': table_id, 'name': table['name']} for table_id, table in self.tables.items()]

    def list_fields(self, app_token, table_id):
        return self.tables[table_id]['fields']

    def iter_records(self, app_token, table_id, fields):
        yield from copy.deepcopy(self.records[table_id])

    def request(self, method, path, **kwargs):
        if method == 'POST' and path.endswith('/tables'):
            table_id = 'tbl_' + str(len(self.tables) + 1)
            self.tables[table_id] = copy.deepcopy(kwargs['json']['table'])
            self.records[table_id] = []
            return {'data': {'table': {'table_id': table_id}}}
        raise AssertionError((method, path))

    def batch_create(self, app_token, table_id, records):
        for record in records:
            self.records[table_id].append({'record_id': f"{table_id}_{len(self.records[table_id])}",
                                            'fields': copy.deepcopy(record['fields'])})
        self.batch_count += 1
        if self.batch_count == self.fail_on_batch:
            raise RuntimeError('response lost after server accepted batch')

    def batch_update(self, app_token, table_id, records):
        by_id = {row['record_id']: row for row in self.records[table_id]}
        for record in records:
            by_id[record['record_id']]['fields'].update(record['fields'])


class SplitSyncTests(unittest.TestCase):
    def setUp(self):
        self.config = workspace.validate_config({
            'sources': [{'id': 'source', 'name': '业务库', 'database': 'Business', 'server': 'localhost'}],
            'jobs': [{'id': 'job', 'source_id': 'source', 'table': 'Orders',
                      'unique_key': 'Id', 'columns': [
                          {'source': 'Id', 'sql_type': 'int'}, {'source': 'Name', 'sql_type': 'nvarchar'},
                          {'source': 'CreatedAt', 'sql_type': 'datetime2'}],
                      'split': {'enabled': True, 'row_limit': 2, 'column': 'CreatedAt'},
                      'target': {'table_name': '订单'}}]})
        self.job = self.config['jobs'][0]
        self.client = FakeFeishu()
        self.state = {'jobs': {}}

    def rows(self, count):
        return [{'Id': index, 'Name': f'中文商品{index}', 'CreatedAt': datetime(2026, 1, 1) + timedelta(seconds=index)}
                for index in range(count)]

    def run_sync(self, rows, dry_run=False):
        def fetch(source, job, saved_state, row_sink=None, settings=None):
            for start in range(0, len(rows), 1000):
                row_sink(rows[start:start + 1000])
            return [], datetime(2026, 1, 1), False
        with mock.patch.object(multi_sync, 'fetch_job_rows', side_effect=fetch), \
             mock.patch.object(multi_sync, 'save_target_binding'), \
             mock.patch.object(multi_sync, 'save_state'):
            return multi_sync.run_job(self.client, 'app', self.config, self.state, self.job, dry_run)

    def test_time_ordered_split_and_repeat_sync_are_idempotent(self):
        rows = self.rows(5)
        summary = self.run_sync(list(reversed(rows)))
        self.assertEqual(summary['create'], 5)
        self.assertEqual([len(rows) for rows in self.client.records.values()], [2, 2, 1])
        self.assertEqual([[record['fields']['Id'] for record in rows] for rows in self.client.records.values()],
                         [[0, 1], [2, 3], [4]])
        summary = self.run_sync(rows)
        self.assertEqual(summary['skip'], 5)
        self.assertEqual(summary['create'], 0)
        self.assertEqual(len(self.client.tables), 3)

    def test_update_time_changes_update_original_partition(self):
        rows = self.rows(5)
        self.run_sync(rows)
        rows[2]['CreatedAt'] = datetime(2028, 1, 1)
        rows[2]['Name'] = '中文修改'
        summary = self.run_sync([rows[2]])
        self.assertEqual(summary['update'], 1)
        self.assertEqual(len(self.client.tables), 3)
        self.assertEqual(self.client.records['tbl_2'][0]['fields']['Name'], '中文修改')
        self.assertEqual([len(rows) for rows in self.client.records.values()], [2, 2, 1])

    def test_partial_write_retry_recovers_new_partition(self):
        self.client.fail_on_batch = 2
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            self.run_sync(self.rows(5))
        self.assertEqual(self.state['jobs'], {})
        self.client.fail_on_batch = None
        summary = self.run_sync(self.rows(5))
        self.assertEqual(summary['skip'], 4)
        self.assertEqual(summary['create'], 1)
        ids = [row['fields']['Id'] for rows in self.client.records.values() for row in rows]
        self.assertEqual(sorted(ids), list(range(5)))

    def test_dry_run_plans_partitions_without_creating_tables(self):
        summary = self.run_sync(self.rows(5), dry_run=True)
        self.assertEqual(len(summary['partitions']), 3)
        self.assertEqual(self.client.tables, {})
        self.assertEqual(self.state['jobs'], {})

    def test_turning_split_off_still_locates_existing_partition_records(self):
        rows = self.rows(5)
        self.run_sync(rows)
        self.job['split']['enabled'] = False
        summary = self.run_sync(rows)
        self.assertEqual(summary['skip'], 5)
        self.assertEqual(summary['create'], 0)

    def test_40001_rows_create_40000_and_1(self):
        self.job['split']['row_limit'] = 40000
        summary = self.run_sync(self.rows(40001))
        self.assertEqual(summary['create'], 40001)
        self.assertEqual([len(rows) for rows in self.client.records.values()], [40000, 1])

    def test_new_partitions_copy_custom_target_field_types(self):
        self.run_sync(self.rows(1))
        for field in self.client.tables['tbl_1']['fields']:
            if field['field_name'] == 'Id':
                field['type'] = 1
        self.run_sync(self.rows(5))
        for table in self.client.tables.values():
            id_field = next(field for field in table['fields'] if field['field_name'] == 'Id')
            self.assertEqual(id_field['type'], 1)

    def test_null_time_values_are_allocated_last(self):
        rows = self.rows(3)
        rows[0]['CreatedAt'] = None
        self.run_sync(rows)
        self.assertEqual([[record['fields']['Id'] for record in records] for records in self.client.records.values()],
                         [[1, 2], [0]])

    def test_split_names_fit_target_limit(self):
        self.job['target']['table_name'] = '中' * 100
        self.assertEqual(len(multi_sync.partition_name(self.job, 2)), 100)
        self.assertNotEqual(multi_sync.partition_name(self.job, 2), multi_sync.partition_name(self.job, 3))

    def test_time_column_validation_and_creation_time_preference(self):
        self.assertEqual(workspace.default_split_column([
            {'source': '更新时间', 'sql_type': 'datetime2'}, {'source': '创建时间', 'sql_type': 'datetime2'}]), '创建时间')
        self.job['split']['column'] = 'Name'
        with self.assertRaisesRegex(ValueError, '日期字段'):
            workspace.validate_config(self.config)
