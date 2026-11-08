"""Real database regressions, with optional PostgreSQL, MySQL and MariaDB fixtures."""
import copy
import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import dashboard
import databases
import multi_sync
import workspace
from test_split_sync import FakeFeishu


class SQLiteWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = workspace.validate_source({
            'id': 'source_test', 'name': '中文测试库', 'type': 'sqlite',
            'database': str(self.root / '中文业务.db'),
        })
        self.schema = 'main'
        self.writer = sqlite3.connect(self.source['database'])
        self.addCleanup(self.writer.close)
        self.execute('CREATE TABLE "订单" ("Id" INTEGER PRIMARY KEY AUTOINCREMENT, "名称" TEXT, "UpdatedAt" DATETIME, "数据" BLOB)')
        self.execute('CREATE VIEW "订单视图" AS SELECT * FROM "订单"')
        self.finish_setup()

    def execute(self, query, params=None):
        with contextlib.closing(self.writer.cursor()) as cursor:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            return cursor.fetchall() if cursor.description else []

    def finish_setup(self):
        self.table = databases.qualified_table(self.source, self.schema, '订单')
        self.marker = databases.placeholder(self.source)
        self.execute(f'INSERT INTO {self.table} ("名称", "UpdatedAt", "数据") VALUES ({self.marker}, {self.marker}, {self.marker})',
                            ('中文商品😀', '2026-01-01 12:00:00', b'\x00\xff'))
        self.writer.commit()
        self.patches = mock.patch.multiple(workspace, CONFIG_PATH=self.root / 'sync_config.json',
                                          ENV_PATH=self.root / '.env', STATE_PATH=self.root / 'sync_state.json')
        self.patches.start()
        self.addCleanup(self.patches.stop)
        workspace.save_config({'sources': [self.source], 'jobs': []})
        self.client = dashboard.app.test_client()
        self.headers = {'Host': '127.0.0.1:5001', 'X-CSRF-Token': dashboard.CSRF_TOKEN}

    def request(self, path, body=None):
        response = self.client.get(path, headers=self.headers) if body is None else self.client.post(path, json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['ok'])
        return response.get_json()

    def create_job(self):
        result = self.request('/api/jobs/bulk', {'tables': [
            {'source_id': self.source['id'], 'schema': self.schema, 'name': '订单'}]})
        self.assertEqual(result['errors'], [])
        return result['config']['jobs'][0]

    def test_connection_discovery_unicode_preview_and_bulk_task(self):
        base = '/api/sources/' + self.source['id']
        self.request(base + '/test', {})
        self.assertTrue(self.request(base + '/databases')['databases'])
        tables = self.request('/api/discovery', {'source_id': self.source['id']})['tables']
        self.assertTrue(any(t['name'] == '订单' and t['type'] == 'table' for t in tables))
        self.assertTrue(any(t['name'] == '订单视图' and t['type'] == 'view' for t in tables))
        columns = multi_sync.list_columns(self.source, self.schema, '订单')
        self.assertEqual([c['name'] for c in columns], ['Id', '名称', 'UpdatedAt', '数据'])
        self.assertTrue(columns[0]['is_identity'])
        preview = self.request(base + '/preview', {'schema': self.schema, 'table': '订单', 'columns': ['名称', '数据']})
        self.assertEqual(preview['rows'], [['中文商品😀', '00ff']])
        job = self.create_job()
        self.assertEqual(job['unique_key'], 'Id')
        self.assertEqual(job['incremental'], {'enabled': True, 'column': 'UpdatedAt'})
        self.assertEqual(job['split']['column'], 'UpdatedAt')

    def test_real_full_incremental_sync_and_retry(self):
        job = self.create_job()
        config = workspace.load_config()
        config['settings']['timezone_offset'] = 0
        fake = FakeFeishu()
        state = {'jobs': {}}
        first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(first['create'], 1)
        first_record = next(iter(fake.records.values()))[0]['fields']
        self.assertEqual(first_record['名称'], '中文商品😀')
        self.assertEqual(first_record['UpdatedAt'], 1767268800000)
        self.execute(f'UPDATE {self.table} SET "名称" = {self.marker}, "UpdatedAt" = {self.marker}',
                            ('中文更新', '2026-01-02 12:00:00'))
        self.execute(f'INSERT INTO {self.table} ("名称", "UpdatedAt") VALUES ({self.marker}, {self.marker})',
                            ('新增中文', '2026-01-02 12:00:00'))
        self.writer.commit()
        # Force full target comparison while still exercising actual incremental source SQL.
        fake.record_count = lambda *args: None
        second = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(second['incremental'])
        self.assertEqual((second['create'], second['update']), (1, 1))
        third = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((third['create'], third['update'], third['skip']), (0, 0, 2))

    def test_single_job_edit_preserves_latest_other_jobs(self):
        job = self.create_job()
        config = workspace.load_config()
        other = copy.deepcopy(job)
        other.update(id='job_other', name='其他任务')
        config['jobs'].append(other)
        workspace.save_config(config)
        job['name'] = '编辑名称'
        data = self.request('/api/jobs', {'job': job})
        self.assertEqual([j['name'] for j in data['config']['jobs']], ['编辑名称', '其他任务'])
        job['incremental'] = {'enabled': True, 'column': ''}
        response = self.client.post('/api/jobs', json={'job': job}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn('任务 编辑名称', response.get_json()['message'])
        self.assertIn('增量字段', response.get_json()['message'])
        self.assertEqual(workspace.load_config()['jobs'][0]['incremental']['column'], 'UpdatedAt')

    def test_exact_large_keys_and_decimal_values_survive_split_and_update(self):
        kind = databases.database_type(self.source)
        table = databases.qualified_table(self.source, self.schema, '精度测试')
        amount_type = 'TEXT' if kind == 'sqlite' else 'DECIMAL(30,10)'
        time_type = 'DATETIME' if kind in {'sqlite', 'mysql', 'mariadb'} else 'TIMESTAMP'
        self.execute(f'CREATE TABLE {table} ("Id" BIGINT PRIMARY KEY, "Amount" {amount_type}, "Small" DECIMAL(10,2), "UpdatedAt" {time_type})')
        values = [(9007199254740993, '123456789012345.6789000000', '12.50', '2026-01-01 00:00:00'),
                  (9007199254740994, '0.0000000001', '0.01', '2026-01-01 00:00:00')]
        for row in values:
            self.execute(f'INSERT INTO {table} VALUES ({", ".join([self.marker] * 4)})', row)
        self.writer.commit()
        result = self.request('/api/jobs/bulk', {'tables': [
            {'source_id': self.source['id'], 'schema': self.schema, 'name': '精度测试'}]})
        self.assertEqual(result['errors'], [])
        job = next(job for job in result['config']['jobs'] if job['table'] == '精度测试')
        self.assertEqual(job['columns'][0]['target_type'], 1)
        if kind != 'sqlite':
            self.assertEqual(job['columns'][1]['target_type'], 1)
            self.assertNotIn('target_type', job['columns'][2])
        job['split']['row_limit'] = 1
        self.request('/api/jobs', {'job': job})
        config = workspace.load_config()
        job = next(item for item in config['jobs'] if item['id'] == job['id'])
        fake, state = FakeFeishu(), {'jobs': {}}
        first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(first['create'], 2)
        self.assertEqual(len(fake.tables), 2)
        records = {r['fields']['Id']: r['fields'] for rows in fake.records.values() for r in rows}
        self.assertEqual(set(records), {'9007199254740993', '9007199254740994'})
        self.assertEqual(records['9007199254740993']['Amount'], values[0][1])
        self.assertEqual(records['9007199254740993']['Small'], 12.5 if kind != 'sqlite' else '12.5')
        self.execute(f'UPDATE {table} SET "Amount"={self.marker}, "UpdatedAt"={self.marker} WHERE "Id"={self.marker}',
                     ('987654321012345.1234567890', '2026-01-02 00:00:00', 9007199254740993))
        self.writer.commit()
        updated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(updated['incremental'])
        self.assertEqual((updated['create'], updated['update']), (0, 1))
        repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((repeated['create'], repeated['update']), (0, 0))
        records = {r['fields']['Id']: r['fields'] for rows in fake.records.values() for r in rows}
        self.assertEqual(records['9007199254740993']['Amount'], '987654321012345.1234567890')
        before = copy.deepcopy(state)
        stored = workspace.STATE_PATH.read_bytes()
        job['columns'][0]['target_type'] = 2
        with self.assertRaisesRegex(multi_sync.ConfigError, '类型.*不一致'):
            multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(state, before)
        self.assertEqual(workspace.STATE_PATH.read_bytes(), stored)
        # Even a matching numeric target must reject an unsafe ID before writes.
        for target in fake.tables.values():
            next(field for field in target['fields'] if field['field_name'] == 'Id')['type'] = 2
        write_count = fake.batch_count
        with self.assertRaisesRegex(multi_sync.ConfigError, '唯一键 Id.*安全整数'):
            multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(fake.batch_count, write_count)
        self.assertEqual(state, before)
        self.assertEqual(workspace.STATE_PATH.read_bytes(), stored)

    def test_null_incremental_values_are_rechecked_and_retry_is_idempotent(self):
        job = self.create_job()
        job['split']['row_limit'] = 1
        self.request('/api/jobs', {'job': job})
        config = workspace.load_config()
        config['settings']['null_policy'] = 'overwrite'
        job = config['jobs'][0]
        fake = FakeFeishu()
        fake.record_count = lambda *args: None
        state = {'jobs': {}}
        multi_sync.run_job(fake, 'app_test', config, state, job, False)
        first_cursor = copy.deepcopy(state['jobs'][job['id']]['cursor'])
        self.execute(f'INSERT INTO {self.table} ("名称", "UpdatedAt") VALUES ({self.marker}, {self.marker})',
                     ('空时间新增', None))
        self.writer.commit()
        before = copy.deepcopy(state)
        disk_before = workspace.STATE_PATH.read_bytes()
        fake.fail_on_batch = fake.batch_count + 1
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(state, before)
        self.assertEqual(workspace.STATE_PATH.read_bytes(), disk_before)
        fake.fail_on_batch = None
        retried = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(retried['incremental'])
        self.assertEqual((retried['create'], retried['update'], retried['skip']), (0, 0, 2))
        self.assertEqual(len(fake.tables), 2)
        self.assertEqual(state['jobs'][job['id']]['cursor'], first_cursor)
        self.execute(f'UPDATE {self.table} SET "名称"={self.marker} WHERE "UpdatedAt" IS NULL', ('空时间修改',))
        self.writer.commit()
        updated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(updated['update'], 1)
        self.execute(f'UPDATE {self.table} SET "UpdatedAt"={self.marker} WHERE "UpdatedAt" IS NULL', ('2026-01-03 00:00:00',))
        self.writer.commit()
        filled = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((filled['create'], filled['update']), (0, 1))
        later_cursor = copy.deepcopy(state['jobs'][job['id']]['cursor'])
        self.assertNotEqual(first_cursor, later_cursor)
        self.execute(f'UPDATE {self.table} SET "名称"={self.marker}, "UpdatedAt"=NULL', ('时间清空后更新',))
        self.writer.commit()
        cleared = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((cleared['create'], cleared['update']), (0, 2))
        self.assertEqual(state['jobs'][job['id']]['cursor'], later_cursor)
        for rows in fake.records.values():
            self.assertEqual(rows[0]['fields']['名称'], '时间清空后更新')
            self.assertIsNone(rows[0]['fields']['UpdatedAt'])
        repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((repeated['create'], repeated['update'], repeated['skip']), (0, 0, 2))

    def test_corrupt_saved_cursor_recovers_by_full_scan(self):
        job = self.create_job()
        config = workspace.load_config()
        fake = FakeFeishu()
        state = {'jobs': {}}
        multi_sync.run_job(fake, 'app_test', config, state, job, False)
        original_cursor = copy.deepcopy(state['jobs'][job['id']]['cursor'])
        state['jobs'][job['id']]['cursor'] = {'type': 'datetime', 'value': 'invalid-date'}
        summary = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertFalse(summary['incremental'])
        self.assertEqual((summary['create'], summary['skip']), (0, 1))
        self.assertEqual(state['jobs'][job['id']]['cursor'], original_cursor)

    def test_reset_during_write_preserves_partitions_and_forces_full_rescan(self):
        job = self.create_job()
        job['split']['row_limit'] = 1
        self.request('/api/jobs', {'job': job})
        config = workspace.load_config()
        job = config['jobs'][0]
        fake, state = FakeFeishu(), {'jobs': {}}
        fake.record_count = lambda *args: None
        multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.execute(f'INSERT INTO {self.table} ("名称", "UpdatedAt") VALUES ({self.marker}, {self.marker})',
                     ('后续记录', '2026-01-02 12:00:00'))
        self.writer.commit()
        write = fake.batch_create

        def reset_after_write(*args):
            write(*args)
            response = self.client.delete('/api/jobs/' + job['id'] + '/state', headers=self.headers)
            self.assertEqual(response.status_code, 200)

        with mock.patch.object(fake, 'batch_create', side_effect=reset_after_write):
            summary = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(summary['state_reset_pending'])
        self.assertNotIn('cursor', state['jobs'][job['id']])
        self.assertEqual(len(state['jobs'][job['id']]['partitions']), 2)
        for index, table in enumerate(fake.tables.values()):
            table['name'] = f'重命名分表{index}'
        self.execute(f'UPDATE {self.table} SET "名称"={self.marker} WHERE "Id"=1', ('历史记录修正',))
        self.writer.commit()
        pending = workspace.load_state()
        pending_bytes = workspace.STATE_PATH.read_bytes()
        preview = multi_sync.run_job(fake, 'app_test', config, pending, job, True)
        self.assertFalse(preview['incremental'])
        self.assertEqual(preview['update'], 1)
        self.assertEqual(workspace.STATE_PATH.read_bytes(), pending_bytes)
        with mock.patch.object(fake, 'batch_update', side_effect=RuntimeError('write failed')):
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                multi_sync.run_job(fake, 'app_test', config, pending, job, False)
        self.assertTrue(pending['jobs'][job['id']]['reset_pending'])
        self.assertNotIn('cursor', pending['jobs'][job['id']])
        self.assertEqual(workspace.STATE_PATH.read_bytes(), pending_bytes)
        # Restart the actual runner from disk, including renamed partition IDs.
        with mock.patch.object(multi_sync, 'feishu_client', return_value=fake), \
             mock.patch.object(multi_sync, 'resolve_app_token', return_value='app_test'), \
             mock.patch('sync.LOCK_PATH', self.root / '.sync.lock'), \
             mock.patch.object(multi_sync, 'configure_logging'):
            summary = multi_sync.run(job_id=job['id'])[0]
        self.assertFalse(summary['incremental'])
        self.assertEqual((summary['create'], summary['update'], summary['skip']), (0, 1, 1))
        self.assertEqual(len(fake.tables), 2)
        saved = workspace.load_state()['jobs'][job['id']]
        self.assertIn('cursor', saved)
        self.assertNotIn('reset_pending', saved)

    def test_state_write_failure_retries_without_advancing_memory_or_duplicating_rows(self):
        job = self.create_job()
        config = workspace.load_config()
        fake, state = FakeFeishu(), {'jobs': {}}
        fake.record_count = lambda *args: None
        multi_sync.run_job(fake, 'app_test', config, state, job, False)
        before = copy.deepcopy(state)
        disk_before = workspace.STATE_PATH.read_bytes()
        self.execute(f'INSERT INTO {self.table} ("名称", "UpdatedAt") VALUES ({self.marker}, {self.marker})',
                     ('状态写入失败后重试', '2026-01-02 12:00:00'))
        self.writer.commit()
        with mock.patch.object(workspace, 'save_state', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(state, before)
        self.assertEqual(workspace.STATE_PATH.read_bytes(), disk_before)
        summary = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((summary['create'], summary['update'], summary['skip']), (0, 0, 2))
        self.assertEqual(sum(len(rows) for rows in fake.records.values()), 2)
        self.assertNotEqual(state['jobs'][job['id']]['cursor'], before['jobs'][job['id']]['cursor'])

    def test_source_is_read_only(self):
        with multi_sync.sql_connection(self.source) as connection, databases.cursor_for(connection, self.source) as cursor:
            with self.assertRaises(Exception):
                cursor.execute(f'DELETE FROM {self.table}')
        self.assertEqual(self.execute(f'SELECT COUNT(*) FROM {self.table}')[0][0], 1)

    def test_running_job_uses_its_timeout_snapshot(self):
        job = self.create_job()
        config = workspace.load_config()
        config['settings']['query_timeout'] = 7
        with mock.patch.object(multi_sync, 'sql_connection', wraps=multi_sync.sql_connection) as connect:
            multi_sync.run_job(FakeFeishu(), 'app_test', config, {'jobs': {}}, job, True)
        self.assertEqual(connect.call_args.kwargs['settings']['query_timeout'], 7)

    def test_large_real_source_splits_and_recovers_partial_write(self):
        query = f'INSERT INTO {self.table} ("名称", "UpdatedAt") VALUES ({self.marker}, {self.marker})'
        with contextlib.closing(self.writer.cursor()) as cursor:
            cursor.executemany(query, [('批量中文', '2026-01-02 12:00:00')] * 40000)
        self.writer.commit()
        job = self.create_job()
        config = workspace.load_config()
        fake = FakeFeishu()
        fake.record_count = lambda *args: None
        state = {'jobs': {}}
        fake.fail_on_batch = 81
        with mock.patch.object(multi_sync, 'progress'), self.assertRaisesRegex(RuntimeError, 'response lost'):
            multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(state['jobs'], {})
        fake.fail_on_batch = None
        with mock.patch.object(multi_sync, 'progress'):
            retry = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((retry['create'], retry['skip']), (0, 40001))
        self.assertEqual(sorted(len(records) for records in fake.records.values()), [1, 40000])
        ids = [record['fields']['Id'] for records in fake.records.values() for record in records]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn('cursor', state['jobs'][job['id']])

    def test_composite_primary_and_keyless_view_generate_stable_keys(self):
        table = databases.qualified_table(self.source, self.schema, '复合订单')
        self.execute(f'CREATE TABLE {table} ("Region" VARCHAR(20), "Code" INTEGER, "名称" TEXT, PRIMARY KEY ("Region", "Code"))')
        self.execute(f'INSERT INTO {table} VALUES ({self.marker}, {self.marker}, {self.marker})', ('华南', 1, '中文'))
        self.writer.commit()
        job = multi_sync.automatic_job(self.source, self.schema, '复合订单')
        self.assertEqual(job['generated_key']['columns'], ['Region', 'Code'])
        first, _, _ = multi_sync.fetch_job_rows(self.source, job, {})
        self.execute(f'UPDATE {table} SET "名称" = {self.marker}', ('更新',))
        self.writer.commit()
        second, _, _ = multi_sync.fetch_job_rows(self.source, job, {})
        self.assertEqual(first[0][job['unique_key']], second[0][job['unique_key']])
        view_job = multi_sync.automatic_job(self.source, self.schema, '订单视图')
        self.assertEqual(view_job['generated_key'], {'columns': []})
        self.assertFalse(view_job['incremental']['enabled'])
        first, _, _ = multi_sync.fetch_job_rows(self.source, view_job, {})
        second, _, _ = multi_sync.fetch_job_rows(self.source, view_job, {})
        self.assertEqual(first, second)

    def test_quoted_percent_identifiers_through_config_preview_and_fetch(self):
        name = '计量%表"历史'
        quoted = f'"{self.schema}"."计量%表""历史"'
        self.execute(f'CREATE TABLE {quoted} ("Id" INTEGER PRIMARY KEY, "含税%金额" TEXT)')
        self.execute(f"INSERT INTO {quoted} VALUES (1, '中文值')")
        self.writer.commit()
        job = multi_sync.automatic_job(self.source, self.schema, name)
        validated = workspace.validate_config({'sources': [self.source], 'jobs': [job]})
        rows, _, _ = multi_sync.fetch_job_rows(self.source, validated['jobs'][0], {})
        self.assertEqual(rows, [{'Id': 1, '含税%金额': '中文值'}])
        preview = self.request('/api/sources/' + self.source['id'] + '/preview',
                               {'schema': self.schema, 'table': name, 'columns': ['含税%金额']})
        self.assertEqual(preview['rows'], [['中文值']])


@unittest.skipUnless(os.environ.get('SQLFEISHU_TEST_POSTGRES_DSN'), 'PostgreSQL integration DSN not configured')
class PostgreSQLWorkflowTests(SQLiteWorkflowTests):
    def setUp(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        dsn = os.environ['SQLFEISHU_TEST_POSTGRES_DSN']
        info = conninfo_to_dict(dsn)
        self.source = workspace.validate_source({
            'id': 'source_test', 'name': '中文测试库', 'type': 'postgresql',
            'tls': {'mode': 'verify-full', 'ca': os.environ['SQLFEISHU_TEST_TLS_CA']} if os.environ.get('SQLFEISHU_TEST_TLS_CA') else {},
            'server': info['host'], 'port': info.get('port', 5432), 'database': info['dbname'], 'user': info['user'],
        })
        self.password = mock.patch.dict(os.environ, {self.source['password_env']: info.get('password', '')})
        self.password.start()
        self.addCleanup(self.password.stop)
        self.writer = psycopg.connect(dsn, autocommit=True)
        self.addCleanup(self.writer.close)
        self.schema = 'test_' + uuid.uuid4().hex
        self.execute(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(lambda: self.execute(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.execute(f'CREATE TABLE "{self.schema}"."订单" ("Id" BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "名称" TEXT, "UpdatedAt" TIMESTAMP, "数据" BYTEA)')
        self.execute(f'CREATE VIEW "{self.schema}"."订单视图" AS SELECT * FROM "{self.schema}"."订单"')
        self.finish_setup()

    def test_interval_cursor_retains_day_and_microsecond_boundaries(self):
        relation = f'"{self.schema}"."时长游标"'
        self.execute(f'CREATE TABLE {relation} ("Id" int PRIMARY KEY, "时长" interval)')
        values = [timedelta(hours=-25, microseconds=-1), timedelta(microseconds=-1),
                  timedelta(), timedelta(hours=25, microseconds=1)]
        for index, value in enumerate(values):
            self.execute(f'INSERT INTO {relation} VALUES (%s,%s)', (index, value))
        job = multi_sync.automatic_job(self.source, self.schema, '时长游标')
        job['incremental'] = {'enabled': True, 'column': '时长'}
        for index, value in enumerate(values):
            for cursor in (multi_sync._encode_cursor(value), {'type': 'str', 'value': str(value)}):
                with self.subTest(value=value, cursor=cursor):
                    rows, maximum, used = multi_sync.fetch_job_rows(self.source, job, {'cursor': cursor})
                    self.assertTrue(used)
                    self.assertEqual([row['Id'] for row in rows], list(range(index, len(values))))
                    self.assertEqual(maximum, values[-1])

    def test_binary_array_preview_preserves_shape_nulls_and_hex_across_relations(self):
        relation = f'"{self.schema}"."二进制数组"'
        self.execute(f'CREATE DOMAIN "{self.schema}"."数组域" AS bytea[]')
        self.execute(f'CREATE DOMAIN "{self.schema}"."嵌套数组域" AS "{self.schema}"."数组域"')
        self.execute(f'CREATE TABLE {relation} ("Id" int PRIMARY KEY, "数据" bytea[], '
                     f'"域数据" "{self.schema}"."嵌套数组域", "内部" text)')
        values = [[b'\x00\xff', b'', None, '中文😀'.encode()], [], None,
                  [[b'\x00', None], [b'', b'\xff']]]
        for index, value in enumerate(values):
            self.execute(f'INSERT INTO {relation} VALUES (%s,%s,%s,\'内部\')', (index,value,value))
        self.execute(f'CREATE VIEW "{self.schema}"."数组视图" AS SELECT * FROM {relation}')
        self.execute(f'CREATE MATERIALIZED VIEW "{self.schema}"."数组快照" AS SELECT * FROM {relation}')
        user = 'reader_' + uuid.uuid4().hex[:16]
        self.execute(f'CREATE ROLE "{user}" LOGIN')
        self.addCleanup(lambda: self.execute(f'DROP ROLE "{user}"'))
        self.addCleanup(lambda: self.execute(f'DROP OWNED BY "{user}"'))
        self.execute(f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{user}"')
        source = {**self.source, 'user': user}
        workspace.save_config({'sources': [source], 'jobs': []})
        expected = [['00ff','',None,'中文😀'.encode().hex()], [], None, [['00',None],['','ff']]]
        for name in ('二进制数组','数组视图','数组快照'):
            self.execute(f'GRANT SELECT ("Id","数据","域数据") ON "{self.schema}"."{name}" TO "{user}"')
            metadata = self.request(f'/api/sources/{source["id"]}/columns?schema={self.schema}&table={name}')
            self.assertEqual([col['name'] for col in metadata['columns']], ['Id','数据','域数据'])
            self.assertTrue(all(col['binary_array'] for col in metadata['columns'][1:]))
            preview = self.request('/api/sources/' + source['id'] + '/preview',
                                   {'schema': self.schema, 'table': name, 'columns': ['Id','数据','域数据']})
            for row in preview['rows']:
                for value in row[1:]:
                    self.assertEqual(None if value is None else json.loads(value), expected[int(row[0])])
        tables = self.request('/api/discovery', {'source_id': source['id']})['tables']
        created = self.request('/api/jobs/bulk', {'tables': tables})
        self.assertEqual(created['errors'], [])
        config = workspace.load_config()
        fake, state = FakeFeishu(), {'jobs': {}}
        for job in config['jobs']:
            first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
            repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
            self.assertEqual((first['create'], repeated['create'], repeated['update']), (4,0,0))
            for row in fake.records[job['target']['table_id']]:
                value = row['fields'].get('数据')
                self.assertEqual(None if value is None else json.loads(value), expected[row['fields']['Id']])

    def test_binary_array_upgrade_keeps_existing_ids_across_batches_and_retry(self):
        for mode in ('primary', 'content', 'array_key', 'legacy_json'):
            with self.subTest(mode=mode):
                name = '旧数组_' + mode
                relation = f'"{self.schema}"."{name}"'
                id_column = '"Id" int PRIMARY KEY,' if mode == 'primary' else ''
                self.execute(f'CREATE TABLE {relation} ({id_column}"数据" bytea[], "内容" jsonb)')
                count = 2 if mode == 'array_key' else 1001
                for index in range(count):
                    value = [bytes([index % 2]), b'', None]
                    params = (index, value, '"中文"') if mode == 'primary' else (value, '"中文"')
                    placeholders = ','.join(['%s'] * len(params))
                    self.execute(f'INSERT INTO {relation} VALUES ({placeholders})', params)
                job = multi_sync.automatic_job(self.source, self.schema, name)
                if mode == 'array_key':
                    job.pop('generated_key')
                    job['unique_key'] = '数据'
                    job['columns'] = [col for col in job['columns'] if col['source'] != '__sqlfeishu_row_key']
                if mode == 'primary':
                    job['incremental'] = {'enabled': True, 'column': 'Id'}
                job['split']['row_limit'] = 500 if mode != 'array_key' else 1
                config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
                job = config['jobs'][0]
                metadata = multi_sync.list_columns(self.source, self.schema, name)
                for column in metadata:
                    column.pop('binary_array', None)
                    if mode == 'legacy_json' and column['name'] == '内容':
                        column['sql_type'] = 'text'
                fake, state = FakeFeishu(), {'jobs': {}}
                fake.record_count = lambda *args: None
                with mock.patch.object(multi_sync, 'list_columns', return_value=metadata):
                    multi_sync.run_job(fake, 'app_test', config, state, job, False)
                if mode == 'legacy_json':
                    state['jobs'][job['id']].pop('postgres_json_v2_table_id')
                    state['jobs'][job['id']].pop('postgres_json_v2_columns')
                    workspace.save_state(state)
                before_ids = {row['record_id'] for rows in fake.records.values() for row in rows}
                before_state = copy.deepcopy(state)
                before_disk = workspace.STATE_PATH.read_bytes()
                update = fake.batch_update
                calls = 0
                def lose_second_update(*args):
                    nonlocal calls
                    update(*args)
                    calls += 1
                    if calls == 2:
                        raise RuntimeError('accepted update response lost')
                with mock.patch.object(fake, 'batch_update', side_effect=lose_second_update):
                    with self.assertRaisesRegex(RuntimeError, 'response lost'):
                        multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual(state, before_state)
                self.assertEqual(workspace.STATE_PATH.read_bytes(), before_disk)
                retried = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual(retried['create'], 0)
                self.assertEqual(retried['source_rows'], count)
                self.assertTrue(retried['cursor_reset'])
                records = [row for rows in fake.records.values() for row in rows]
                self.assertEqual(before_ids, {row['record_id'] for row in records})
                self.assertTrue(all(json.loads(row['fields']['数据']) in [['00', '', None], ['01', '', None]] for row in records))
                self.assertTrue(all(row['fields']['内容'] == '"中文"' for row in records))
                repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual((repeated['create'], repeated['update'], repeated['skip']),
                                 (0, 0, 1 if mode == 'primary' else count))

    def test_new_binary_array_task_can_restart_after_accepted_first_write(self):
        relation = f'"{self.schema}"."数组首次重试"'
        self.execute(f'CREATE TABLE {relation} ("数据" bytea[])')
        for value in ([b'\x00\xff'], [], None, [b'\x00\xff']):
            self.execute(f'INSERT INTO {relation} VALUES (%s)', (value,))
        job = multi_sync.automatic_job(self.source, self.schema, '数组首次重试')
        job['split']['row_limit'] = 2
        config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
        job = config['jobs'][0]
        fake, state = FakeFeishu(), {'jobs': {}}
        fake.fail_on_batch = 1
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            multi_sync.run_job(fake, 'app_test', config, state, job, False)
        saved = workspace.load_state()
        self.assertEqual(saved['jobs'][job['id']]['postgres_binary_array_table_id'], job['target']['table_id'])
        self.assertNotIn('cursor', saved['jobs'][job['id']])
        fake.fail_on_batch = None
        config = workspace.load_config()
        retried = multi_sync.run_job(fake, 'app_test', config, saved, config['jobs'][0], False)
        self.assertEqual((retried['create'], retried['update'], retried['skip']), (2, 0, 2))

    def test_nested_domain_metadata_preview_and_sync_across_relations(self):
        self.execute(f'CREATE DOMAIN "{self.schema}"."金额域" AS numeric(24,4) NOT NULL')
        self.execute(f'CREATE DOMAIN "{self.schema}"."嵌套金额域" AS "{self.schema}"."金额域"')
        self.execute(f'CREATE DOMAIN "{self.schema}"."时间域" AS timestamp')
        self.execute(f'CREATE DOMAIN "{self.schema}"."布尔域" AS boolean')
        self.execute(f'CREATE DOMAIN "{self.schema}"."内容域" AS jsonb')
        self.execute(f'CREATE DOMAIN "{self.schema}"."嵌套内容域" AS "{self.schema}"."内容域"')
        relation = f'"{self.schema}"."域数据"'
        self.execute(f'CREATE TABLE {relation} ("Id" int PRIMARY KEY, '
                     f'"金额" "{self.schema}"."嵌套金额域", "UpdatedAt" "{self.schema}"."时间域", '
                     f'"内容" "{self.schema}"."嵌套内容域", "有效" "{self.schema}"."布尔域", "内部" text)')
        self.execute(f'INSERT INTO {relation} VALUES (1,123456789012345.6789,\'2026-01-01\',\'null\',true,\'隐藏\'),'
                     '(2,12.5000,\'2026-01-02\',\'"中文😀"\',false,\'隐藏\')')
        self.execute(f'CREATE VIEW "{self.schema}"."域视图" AS SELECT * FROM {relation}')
        self.execute(f'CREATE MATERIALIZED VIEW "{self.schema}"."域快照" AS SELECT * FROM {relation}')
        user = 'reader_' + uuid.uuid4().hex[:16]
        self.execute(f'CREATE ROLE "{user}" LOGIN')
        self.addCleanup(lambda: self.execute(f'DROP ROLE "{user}"'))
        self.addCleanup(lambda: self.execute(f'DROP OWNED BY "{user}"'))
        self.execute(f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{user}"')
        source = {**self.source, 'user': user}
        workspace.save_config({'sources': [source], 'jobs': []})
        for name in ('域数据', '域视图', '域快照'):
            self.execute(f'GRANT SELECT ("Id","金额","UpdatedAt","内容","有效") '
                         f'ON "{self.schema}"."{name}" TO "{user}"')
            columns = multi_sync.list_columns(source, self.schema, name)
            metadata = {col['name']: col for col in columns}
            self.assertNotIn('内部', metadata)
            self.assertEqual(metadata['金额']['sql_type'], 'numeric')
            self.assertEqual(metadata['金额']['numeric_precision'], 24)
            self.assertFalse(metadata['金额']['nullable'])
            self.assertEqual(metadata['金额']['feishu_type'], 1)
            self.assertEqual(metadata['UpdatedAt']['sql_type'], 'datetime2')
            self.assertEqual(metadata['UpdatedAt']['feishu_type'], 5)
            self.assertEqual(metadata['有效']['feishu_type'], 7)
            self.assertEqual(metadata['内容']['sql_type'], 'jsonb')
            preview = self.request('/api/sources/' + source['id'] + '/preview',
                                   {'schema': self.schema, 'table': name, 'columns': ['Id','内容','金额']})
            self.assertEqual(preview['rows'], [['1','null','123456789012345.6789'], ['2','"中文😀"','12.5000']])
        tables = self.request('/api/discovery', {'source_id': source['id']})['tables']
        created = self.request('/api/jobs/bulk', {'tables': tables})
        self.assertEqual(created['errors'], [])
        config = workspace.load_config()
        fake, state = FakeFeishu(), {'jobs': {}}
        fake.record_count = lambda *args: None
        for job in config['jobs']:
            self.assertEqual(next(col for col in job['columns'] if col['source']=='金额')['target_type'], 1)
            first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
            repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
            self.assertEqual((first['create'], repeated['create'], repeated['update']), (2,0,0))
            records = {row['fields']['Id']: row for row in fake.records[job['target']['table_id']]}
            self.assertEqual(records[1]['fields']['内容'], 'null')
            self.assertEqual(records[1]['fields']['金额'], '123456789012345.6789')
            self.assertTrue(records[1]['fields']['有效'])
            self.assertIsInstance(records[1]['fields']['UpdatedAt'], int)
        job = next(job for job in config['jobs'] if job['table']=='域数据')
        self.assertTrue(job['incremental']['enabled'])
        self.execute(f'UPDATE {relation} SET "内容"=\'"更新中文"\', "UpdatedAt"=\'2026-01-03\' WHERE "Id"=1')
        updated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(updated['incremental'])
        self.assertEqual((updated['create'], updated['update']), (0,1))

    def test_old_domain_plan_migrates_mixed_json_keys_and_retries_in_place(self):
        self.execute(f'CREATE DOMAIN "{self.schema}"."内容域" AS jsonb')
        self.execute(f'CREATE DOMAIN "{self.schema}"."嵌套域" AS "{self.schema}"."内容域"')
        relation = f'"{self.schema}"."旧域表"'
        self.execute(f'CREATE TABLE {relation} ("Id" int PRIMARY KEY, "原生内容" jsonb, '
                     f'"域内容" "{self.schema}"."嵌套域", "UpdatedAt" timestamp)')
        self.execute(f'INSERT INTO {relation} VALUES (1,\'null\',\'null\',\'2026-01-01\'),'
                     '(2,\'"中文"\',\'123456789012345.6789\',\'2026-01-02\')')
        self.execute(f'CREATE MATERIALIZED VIEW "{self.schema}"."旧域快照" AS SELECT * FROM {relation}')
        for name in ('旧域表', '旧域快照'):
            with self.subTest(name=name):
                metadata = multi_sync.list_columns(self.source, self.schema, name)
                legacy_metadata = copy.deepcopy(metadata)
                for column in legacy_metadata:
                    if column.pop('domain_type', None):
                        column['sql_type'] = column.pop('legacy_sql_type')
                projection = multi_sync.sql_column_expression
                def legacy_projection(column, source=None):
                    if column['name']=='域内容':
                        return databases.quote_identifier(source, column['name'])
                    return projection(column, source)
                with mock.patch.object(multi_sync, 'list_columns', return_value=legacy_metadata):
                    job = multi_sync.automatic_job(self.source, self.schema, name)
                    job['split']['row_limit'] = 1
                    config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
                    job = config['jobs'][0]
                    fake, state = FakeFeishu(), {'jobs': {}}
                    fake.record_count = lambda *args: None
                    with mock.patch.object(multi_sync, 'sql_column_expression', side_effect=legacy_projection):
                        multi_sync.run_job(fake, 'app_test', config, state, job, False)
                # a9fa674 tracked the target format without listing converted columns.
                state['jobs'][job['id']].pop('postgres_json_v2_columns')
                workspace.save_state(state)
                before = copy.deepcopy(state)
                before_ids = {row['record_id'] for rows in fake.records.values() for row in rows}
                old_job = copy.deepcopy(job)
                write = fake.batch_update
                def lose_response(*args):
                    write(*args)
                    raise RuntimeError('accepted update, lost response')
                with mock.patch.object(fake, 'batch_update', side_effect=lose_response):
                    with self.assertRaisesRegex(RuntimeError, 'lost response'):
                        multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual(state, before)
                summary = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertTrue(summary['cursor_reset'])
                self.assertFalse(summary['incremental'])
                self.assertEqual(summary['create'], 0)
                self.assertEqual(before_ids, {row['record_id'] for rows in fake.records.values() for row in rows})
                records = {row['fields']['Id']: row['fields'] for rows in fake.records.values() for row in rows}
                self.assertEqual(records[1]['原生内容'], 'null')
                self.assertEqual(records[1]['域内容'], 'null')
                self.assertEqual(records[2]['域内容'], '123456789012345.6789')
                self.assertEqual(job, old_job)
                repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual((repeated['create'], repeated['update']), (0,0))

    def test_unrun_legacy_domain_plan_preserves_precision_and_date_incremental(self):
        self.execute(f'CREATE DOMAIN "{self.schema}"."金额域" AS numeric(24,4)')
        self.execute(f'CREATE DOMAIN "{self.schema}"."时间域" AS timestamp')
        relation = f'"{self.schema}"."未运行域快照"'
        self.execute(f'CREATE MATERIALIZED VIEW {relation} AS SELECT 1 AS "Id", '
                     f'123456789012345.6789::"{self.schema}"."金额域" AS "金额", '
                     f'\'2026-01-01\'::"{self.schema}"."时间域" AS "UpdatedAt"')
        metadata = multi_sync.list_columns(self.source, self.schema, '未运行域快照')
        for column in metadata:
            if column.get('domain_type'):
                column['sql_type'] = column['legacy_sql_type']
        with mock.patch.object(multi_sync, 'list_columns', return_value=metadata):
            job = multi_sync.automatic_job(self.source, self.schema, '未运行域快照')
        job.pop('generated_key')
        job['unique_key'] = 'Id'
        job['columns'] = [col for col in job['columns'] if col['source'] != '__sqlfeishu_row_key']
        job['incremental'] = {'enabled': True, 'column': 'UpdatedAt'}
        config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
        job = config['jobs'][0]
        original_columns = copy.deepcopy(job['columns'])
        fake, state = FakeFeishu(), {'jobs': {}}
        fake.record_count = lambda *args: None
        multi_sync.run_job(fake, 'app_test', config, state, job, False)
        record = fake.records[job['target']['table_id']][0]['fields']
        self.assertEqual(record['金额'], '123456789012345.6789')
        self.assertIsInstance(record['UpdatedAt'], int)
        fields = {field['field_name']: field['type'] for field in fake.list_fields('app_test', job['target']['table_id'])}
        self.assertEqual(fields['金额'], 1)
        self.assertEqual(fields['UpdatedAt'], 5)
        self.assertEqual(job['columns'], original_columns)
        repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(repeated['incremental'])
        self.assertEqual((repeated['create'], repeated['skip']), (0,1))

    def test_json_text_preserves_scalar_types_precision_and_sql_null(self):
        from decimal import Decimal
        for kind in ('json', 'jsonb'):
            with self.subTest(kind=kind):
                name = '结构数据_' + kind
                table = f'"{self.schema}"."{name}"'
                self.execute(f'CREATE TABLE {table} ("Id" int PRIMARY KEY, "内容" {kind}, "UpdatedAt" timestamp)')
                values = ['null', '"中文😀"', 'true', 'false', '123456789012345.6789',
                          '{"金额":123456789012345.6789,"明细":[true,null,"中文"]}', '""', '[1,2,3]', None]
                for index, value in enumerate(values):
                    self.execute(f'INSERT INTO {table} VALUES (%s, %s, %s)', (index, value, '2026-01-01 00:00:00'))
                self.writer.commit()
                created = self.request('/api/jobs/bulk', {'tables': [
                    {'source_id': self.source['id'], 'schema': self.schema, 'name': name}]})
                self.assertEqual(created['errors'], [])
                job = next(job for job in created['config']['jobs'] if job['table'] == name)
                preview = self.request('/api/sources/' + self.source['id'] + '/preview',
                                       {'schema': self.schema, 'table': name, 'columns': ['Id', '内容']})
                by_id = {int(row[0]): row[1] for row in preview['rows']}
                for index, expected in enumerate(values):
                    if expected is None:
                        self.assertIsNone(by_id[index])
                    else:
                        self.assertEqual(json.loads(by_id[index], parse_float=Decimal), json.loads(expected, parse_float=Decimal))
                config = workspace.load_config()
                fake, state = FakeFeishu(), {'jobs': {}}
                fake.record_count = lambda *args: None
                first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual(first['create'], len(values))
                records = {row['fields']['Id']: row['fields'] for rows in fake.records.values() for row in rows}
                self.assertEqual(records[0]['内容'], 'null')
                self.assertEqual(records[1]['内容'], '"中文😀"')
                self.assertEqual(records[2]['内容'], 'true')
                self.assertEqual(records[4]['内容'], '123456789012345.6789')
                self.assertNotIn('内容', records[8])
                repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual((repeated['create'], repeated['update']), (0, 0))

    def test_json_conversion_version_invalidates_only_postgresql_json_jobs(self):
        job = self.create_job()
        settings = workspace.runtime_settings()
        original_dumps = json.dumps
        def legacy_dumps(value, *args, **kwargs):
            if isinstance(value, dict) and 'postgres_json_version' in value:
                value = {key: item for key, item in value.items() if key != 'postgres_json_version'}
            return original_dumps(value, *args, **kwargs)
        for kind, sql_type, changed in [('postgresql', 'jsonb', True), ('postgresql', 'json', True),
                                        ('postgresql', 'text', False), ('mysql', 'json', False)]:
            with self.subTest(kind=kind, sql_type=sql_type):
                source = {**self.source, 'type': kind}
                job['columns'].append({'source': 'Payload', 'target': 'Payload', 'sql_type': sql_type})
                current = multi_sync.job_fingerprint(source, job, 'app', 'table', {}, settings)
                with mock.patch.object(multi_sync.json, 'dumps', side_effect=legacy_dumps):
                    old = multi_sync.job_fingerprint(source, job, 'app', 'table', {}, settings)
                self.assertEqual(current != old, changed)
                job['columns'].pop()

    def test_json_upgrade_rescans_old_rows_and_preserves_generated_keys_on_retry(self):
        for generated in (False, True):
            with self.subTest(generated=generated):
                name = 'JSON升级_' + str(generated)
                table = f'"{self.schema}"."{name}"'
                primary = '' if generated else 'PRIMARY KEY'
                self.execute(f'CREATE TABLE {table} ("Id" int {primary}, "内容" jsonb, "UpdatedAt" timestamp)')
                values = ['null', '"中文😀"', 'true', '123456789012345.6789',
                          '{"金额":123456789012345.6789}', '[1,true,null]', None]
                for index, value in enumerate(values):
                    self.execute(f'INSERT INTO {table} VALUES (%s, %s, %s)',
                                 (index, value, f'2026-01-0{1 + index % 2} 00:00:00'))
                self.writer.commit()
                job = multi_sync.automatic_job(self.source, self.schema, name)
                job['split']['row_limit'] = 2
                config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
                job = config['jobs'][0]
                fake, state = FakeFeishu(), {'jobs': {}}
                fake.record_count = lambda *args: None
                original_projection = multi_sync.sql_column_expression

                def legacy_projection(column, source=None):
                    if column.get('sql_type') in {'json', 'jsonb'}:
                        return databases.quote_identifier(source, column['name'])
                    return original_projection(column, source)

                # Seed exact prior-release rows and state, including the old content hashes.
                with mock.patch.object(multi_sync, 'sql_column_expression', side_effect=legacy_projection):
                    rows, maximum, _ = multi_sync.fetch_job_rows(self.source, job, {})
                def fetch_legacy(source, job, saved_state, row_sink=None, settings=None):
                    row_sink(copy.deepcopy(rows))
                    return [], maximum, False
                # Exclude JSON identity migration only while constructing the legacy fixture.
                original_types = [column['sql_type'] for column in job['columns']]
                for column in job['columns']:
                    if column['sql_type'] in {'json', 'jsonb'}:
                        column['sql_type'] = 'text'
                with mock.patch.object(multi_sync, 'fetch_job_rows', side_effect=fetch_legacy), \
                     mock.patch.object(multi_sync, 'job_fingerprint', return_value='pre-json-fix'):
                    multi_sync.run_job(fake, 'app_test', config, state, job, False)
                for column, sql_type in zip(job['columns'], original_types):
                    column['sql_type'] = sql_type
                before_state = copy.deepcopy(state)
                before_ids = {row['record_id'] for rows in fake.records.values() for row in rows}
                before_disk = workspace.STATE_PATH.read_bytes()
                update = fake.batch_update
                calls = 0
                def lose_update_response(*args):
                    nonlocal calls
                    update(*args)
                    calls += 1
                    if calls == 2:
                        raise RuntimeError('response lost after accepted update')
                with mock.patch.object(fake, 'batch_update', side_effect=lose_update_response):
                    with self.assertRaisesRegex(RuntimeError, 'response lost'):
                        multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual(state, before_state)
                self.assertEqual(workspace.STATE_PATH.read_bytes(), before_disk)
                recovered = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertTrue(recovered['cursor_reset'])
                self.assertFalse(recovered['incremental'])
                self.assertEqual(recovered['create'], 0)
                after_ids = {row['record_id'] for rows in fake.records.values() for row in rows}
                self.assertEqual(before_ids, after_ids)
                records = {row['fields']['Id']: row['fields'] for rows in fake.records.values() for row in rows}
                self.assertEqual(records[0]['内容'], 'null')
                self.assertEqual(records[1]['内容'], '"中文😀"')
                self.assertEqual(records[3]['内容'], '123456789012345.6789')
                repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual((repeated['create'], repeated['update']), (0, 0))

    def test_json_key_migration_detects_ambiguity_and_stops_after_success(self):
        for generated in (False, True):
            with self.subTest(generated=generated):
                name = 'JSON标识_' + str(generated)
                table = f'"{self.schema}"."{name}"'
                self.execute(f'CREATE TABLE {table} ("内容" jsonb)')
                values = ['null', None] if generated else ['"alpha"', 'true', '0.125']
                for value in values:
                    self.execute(f'INSERT INTO {table} VALUES (%s)', (value,))
                self.writer.commit()
                job = multi_sync.automatic_job(self.source, self.schema, name)
                if not generated:
                    job.pop('generated_key')
                    job['unique_key'] = '内容'
                    job['columns'] = [col for col in job['columns'] if col['source'] == '内容']
                config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
                job = config['jobs'][0]
                fake, state = FakeFeishu(), {'jobs': {}}
                # Reconstruct the old driver's representation and automatic IDs.
                rows = [{'内容': databases.decode_value(json.loads(value) if value is not None else None, {}, self.source)}
                        for value in values]
                if generated:
                    multi_sync.add_generated_row_keys(rows, job)
                def fetch_legacy(source, job, saved_state, row_sink=None, settings=None):
                    row_sink(copy.deepcopy(rows))
                    return [], None, False
                job['columns'][0]['sql_type'] = 'text'
                with mock.patch.object(multi_sync, 'fetch_job_rows', side_effect=fetch_legacy):
                    multi_sync.run_job(fake, 'app_test', config, state, job, False)
                job['columns'][0]['sql_type'] = 'jsonb'
                before_records = copy.deepcopy(fake.records)
                before_state = copy.deepcopy(state)
                if generated:
                    with self.assertRaisesRegex(multi_sync.ConfigError, '旧 JSON 唯一键.*无法一一匹配'):
                        multi_sync.run_job(fake, 'app_test', config, state, job, False)
                    self.assertEqual(fake.records, before_records)
                    self.assertEqual(state, before_state)
                    continue
                summary = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual((summary['create'], summary['update']), (0, 2))
                self.assertEqual(state['jobs'][job['id']]['postgres_json_v2_table_id'], job['target']['table_id'])
                # Its old representation equals the already migrated "alpha" key.
                # A completed migration must never redirect this new row to it.
                self.execute(f'INSERT INTO {table} VALUES (%s)', ('"\\"alpha\\""',))
                self.writer.commit()
                added = multi_sync.run_job(fake, 'app_test', config, state, job, False)
                self.assertEqual((added['create'], added['update']), (1, 0))
                self.assertEqual(sum(len(rows) for rows in fake.records.values()), 4)

    def test_json_content_keys_distinguish_json_null_from_sql_null_for_new_tasks(self):
        table = f'"{self.schema}"."JSON新任务"'
        self.execute(f'CREATE TABLE {table} ("内容" jsonb)')
        self.execute(f'INSERT INTO {table} VALUES (\'null\'), (NULL), (\'null\')')
        job = multi_sync.automatic_job(self.source, self.schema, 'JSON新任务')
        config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
        job = config['jobs'][0]
        fake, state = FakeFeishu(), {'jobs': {}}
        first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((first['create'], repeated['create'], repeated['update'], repeated['skip']), (3, 0, 0, 3))
        records = [row['fields'] for rows in fake.records.values() for row in rows]
        self.assertEqual(len({row[job['unique_key']] for row in records}), 3)

    def test_json_column_can_be_selected_as_a_text_unique_key(self):
        table = f'"{self.schema}"."JSON业务键"'
        self.execute(f'CREATE TABLE {table} ("内容" json)')
        self.execute(f'INSERT INTO {table} VALUES (\'"中文"\'), (\'null\'), (\'true\')')
        job = multi_sync.automatic_job(self.source, self.schema, 'JSON业务键')
        job.pop('generated_key')
        job['columns'] = [col for col in job['columns'] if col['source'] == '内容']
        job['unique_key'] = '内容'
        config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
        job = config['jobs'][0]
        fake, state = FakeFeishu(), {'jobs': {}}
        first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((first['create'], repeated['create'], repeated['skip']), (3, 0, 3))

    def test_new_json_content_keys_retry_after_accepted_partial_write(self):
        table = f'"{self.schema}"."JSON首次中断"'
        self.execute(f'CREATE TABLE {table} ("内容" jsonb)')
        values = ['null', None, 'null', '"中文"', '9' * 5000]
        for value in values:
            self.execute(f'INSERT INTO {table} VALUES (%s)', (value,))
        job = multi_sync.automatic_job(self.source, self.schema, 'JSON首次中断')
        job['split']['row_limit'] = 2
        config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
        job = config['jobs'][0]
        fake, state = FakeFeishu(), {'jobs': {}}
        fake.fail_on_batch = 2
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            multi_sync.run_job(fake, 'app_test', config, state, job, False)
        saved = workspace.load_state()
        self.assertEqual(saved['jobs'][job['id']], {'postgres_json_v2_table_id': job['target']['table_id'],
                                                   'postgres_json_v2_columns': ['内容']})
        before_ids = {row['record_id'] for rows in fake.records.values() for row in rows}
        fake.fail_on_batch = None
        # Resume as a new process, using only persisted state and configuration.
        config = workspace.load_config()
        job = config['jobs'][0]
        summary = multi_sync.run_job(fake, 'app_test', config, saved, job, False)
        self.assertEqual((summary['create'], summary['update'], summary['skip']), (1, 0, 4))
        records = [row for rows in fake.records.values() for row in rows]
        self.assertTrue(before_ids <= {row['record_id'] for row in records})
        self.assertEqual(len(records), 5)
        self.assertIn('9' * 5000, [row['fields'].get('内容') for row in records])

    def test_json_key_migration_accepts_values_the_old_driver_could_not_decode(self):
        import sys
        table = f'"{self.schema}"."JSON大整数"'
        self.execute(f'CREATE TABLE {table} ("内容" jsonb)')
        self.execute(f'INSERT INTO {table} VALUES (%s)', ('9' * 5000,))
        job = multi_sync.automatic_job(self.source, self.schema, 'JSON大整数')
        config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
        job = config['jobs'][0]
        fake, state = FakeFeishu(), {'jobs': {}}
        multi_sync.ensure_target(fake, 'app_test', config, job, False)
        limit = getattr(sys, 'get_int_max_str_digits', lambda: None)()
        try:
            if limit is not None:
                sys.set_int_max_str_digits(4300)
            first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        finally:
            if limit is not None:
                sys.set_int_max_str_digits(limit)
        self.assertEqual(first['create'], 1)
        record = next(iter(fake.records.values()))[0]
        self.assertEqual(record['fields']['内容'], '9' * 5000)

    def test_json_format_marker_preserves_reset_and_precedes_record_writes(self):
        table = f'"{self.schema}"."JSON状态保存"'
        self.execute(f'CREATE TABLE {table} ("Id" int PRIMARY KEY, "内容" jsonb, "UpdatedAt" timestamp)')
        self.execute(f'INSERT INTO {table} VALUES (1, \'null\', \'2026-01-01\')')
        job = multi_sync.automatic_job(self.source, self.schema, 'JSON状态保存')
        config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
        job = config['jobs'][0]
        fake, state = FakeFeishu(), {'jobs': {}}
        multi_sync.run_job(fake, 'app_test', config, state, job, True)
        self.assertEqual(fake.tables, {})
        self.assertFalse(workspace.STATE_PATH.exists())

        with mock.patch.object(multi_sync, 'save_state', side_effect=OSError('disk write failed')):
            with self.assertRaisesRegex(OSError, 'disk write failed'):
                multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(state, {'jobs': {}})
        self.assertTrue(all(not rows for rows in fake.records.values()))
        self.assertFalse(workspace.STATE_PATH.exists())

        # Use a fresh target to exercise a reset before the format marker write.
        fake = FakeFeishu()
        fetch = multi_sync.fetch_job_rows
        def reset_after_fetch(*args, **kwargs):
            result = fetch(*args, **kwargs)
            workspace.reset_job_state(job['id'])
            return result
        with mock.patch.object(multi_sync, 'fetch_job_rows', side_effect=reset_after_fetch):
            summary = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(summary['state_reset_pending'])
        saved = workspace.load_state()['jobs'][job['id']]
        self.assertNotIn('cursor', saved)
        self.assertTrue(saved['reset_pending'])
        self.assertEqual(saved['postgres_json_v2_table_id'], job['target']['table_id'])
        repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertFalse(repeated['incremental'])
        self.assertEqual((repeated['create'], repeated['skip']), (0, 1))

    def test_json_generated_key_migration_handles_duplicates_across_fetch_batches(self):
        table = f'"{self.schema}"."JSON重复行"'
        self.execute(f'CREATE TABLE {table} ("内容" jsonb)')
        self.execute(f'INSERT INTO {table} SELECT \'true\'::jsonb FROM generate_series(1,1001)')
        job = multi_sync.automatic_job(self.source, self.schema, 'JSON重复行')
        config = workspace.save_config({'sources': [self.source], 'jobs': [job]})
        job = config['jobs'][0]
        fake, state = FakeFeishu(), {'jobs': {}}
        multi_sync.run_job(fake, 'app_test', config, state, job, False)
        before_ids = {row['record_id'] for rows in fake.records.values() for row in rows}
        legacy_rows = [{'内容': True} for _ in range(1001)]
        multi_sync.add_generated_row_keys(legacy_rows, job)
        for record, legacy in zip(next(iter(fake.records.values())), legacy_rows):
            record['fields'][job['unique_key']] = legacy[job['unique_key']]
        state['jobs'][job['id']].pop('postgres_json_v2_table_id')
        workspace.save_state(state)
        summary = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(summary['create'], 0)
        self.assertEqual(summary['update'], 1001)
        self.assertEqual({row['record_id'] for rows in fake.records.values() for row in rows}, before_ids)
        self.assertEqual(len({row['fields'][job['unique_key']] for rows in fake.records.values() for row in rows}), 1001)

    def test_materialized_view_discovery_preview_and_refresh_sync(self):
        name = '生产%日报"汇总'
        quoted = '"' + name.replace('"', '""') + '"'
        relation = f'"{self.schema}".{quoted}'
        self.execute(f'CREATE MATERIALIZED VIEW {relation} AS SELECT "Id", "名称", "UpdatedAt", '
                     f'123456789012345.6789::numeric(24,4) AS "金额", '
                     f'12.50::numeric(10,2) AS "小数", true AS "有效" FROM {self.table}')
        user = 'reader_' + uuid.uuid4().hex[:16]
        self.execute(f'CREATE ROLE "{user}" LOGIN')
        self.addCleanup(lambda: self.execute(f'DROP ROLE "{user}"'))
        self.addCleanup(lambda: self.execute(f'DROP OWNED BY "{user}"'))
        self.execute(f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{user}"')
        self.execute(f'GRANT SELECT ON {relation} TO "{user}"')
        source = {**self.source, 'user': user}
        workspace.save_config({'sources': [source], 'jobs': []})
        discovery = self.request('/api/discovery', {'source_id': source['id']})['tables']
        self.assertEqual([(t['name'], t['type']) for t in discovery], [(name, 'materialized_view')])
        columns = multi_sync.list_columns(source, self.schema, name)
        types = {column['name']: column for column in columns}
        self.assertEqual(types['Id']['sql_type'], 'bigint')
        self.assertEqual(types['UpdatedAt']['sql_type'], 'datetime2')
        self.assertEqual(types['金额']['numeric_precision'], 24)
        self.assertEqual(types['金额']['feishu_type'], 1)
        self.assertEqual(types['小数']['feishu_type'], 2)
        self.assertEqual(types['有效']['feishu_type'], 7)
        self.assertEqual(multi_sync.list_primary_key(source, self.schema, name), [])
        preview = self.request('/api/sources/' + source['id'] + '/preview',
                               {'schema': self.schema, 'table': name, 'columns': ['名称', '金额']})
        self.assertEqual(preview['rows'], [['中文商品😀', '123456789012345.6789']])
        created = self.request('/api/jobs/bulk', {'tables': [
            {'source_id': source['id'], 'schema': self.schema, 'name': name}]})
        self.assertEqual(created['errors'], [])
        job = created['config']['jobs'][0]
        self.assertEqual(job['generated_key'], {'columns': []})
        self.assertFalse(job['incremental']['enabled'])
        config = workspace.load_config()
        fake, state = FakeFeishu(), {'jobs': {}}
        first = multi_sync.run_job(fake, 'app_test', config, state, config['jobs'][0], False)
        repeated = multi_sync.run_job(fake, 'app_test', config, state, config['jobs'][0], False)
        self.assertEqual((first['create'], repeated['create'], repeated['skip']), (1, 0, 1))
        record = next(iter(fake.records.values()))[0]['fields']
        self.assertEqual(record['金额'], '123456789012345.6789')
        self.assertEqual(record['小数'], 12.5)
        self.assertTrue(record['有效'])
        # A user-confirmed stable key allows updates to the same target record.
        job = workspace.load_config()['jobs'][0]
        generated_name = job['unique_key']
        job['columns'] = [col for col in job['columns'] if col['source'] != generated_name]
        job.pop('generated_key')
        job['unique_key'] = 'Id'
        job['incremental'] = {'enabled': True, 'column': 'UpdatedAt'}
        self.request('/api/jobs', {'job': job})
        config = workspace.load_config()
        job = config['jobs'][0]
        multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.execute(f'UPDATE {self.table} SET "名称"=\'刷新后的中文\', "UpdatedAt"=\'2026-01-02 12:00:00\'')
        # The reader sees the stored snapshot until the database owner refreshes it.
        stale = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual((stale['create'], stale['update']), (0, 0))
        self.execute(f'REFRESH MATERIALIZED VIEW {relation}')
        refreshed = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(refreshed['incremental'])
        self.assertEqual((refreshed['create'], refreshed['update']), (0, 1))
        self.assertEqual(sum(len(rows) for rows in fake.records.values()), 1)
        record = next(iter(fake.records.values()))[0]['fields']
        self.assertEqual(record['名称'], '刷新后的中文')

    def test_materialized_view_metadata_respects_column_and_schema_permissions(self):
        relation = f'"{self.schema}"."受限快照"'
        self.execute(f'CREATE MATERIALIZED VIEW {relation} AS SELECT 1 AS "可读", 2 AS "内部"')
        user = 'reader_' + uuid.uuid4().hex[:16]
        self.execute(f'CREATE ROLE "{user}" LOGIN')
        self.addCleanup(lambda: self.execute(f'DROP ROLE "{user}"'))
        self.addCleanup(lambda: self.execute(f'DROP OWNED BY "{user}"'))
        source = {**self.source, 'user': user}
        self.execute(f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{user}"')
        self.assertEqual(multi_sync.list_tables(source), [])
        self.assertEqual(multi_sync.list_columns(source, self.schema, '受限快照'), [])
        self.execute(f'GRANT SELECT ("可读") ON {relation} TO "{user}"')
        self.assertEqual([table['name'] for table in multi_sync.list_tables(source)], ['受限快照'])
        self.assertEqual([col['name'] for col in multi_sync.list_columns(source, self.schema, '受限快照')], ['可读'])
        job = multi_sync.automatic_job(source, self.schema, '受限快照')
        rows, _, _ = multi_sync.fetch_job_rows(source, job, {})
        self.assertEqual(rows[0]['可读'], 1)
        self.assertNotIn('内部', rows[0])
        self.execute(f'REVOKE USAGE ON SCHEMA "{self.schema}" FROM "{user}"')
        self.assertEqual(multi_sync.list_tables(source), [])
        self.assertEqual(multi_sync.list_columns(source, self.schema, '受限快照'), [])

    def test_timestamp_timezone_type_change_rebuilds_cursor(self):
        job = self.create_job()
        rows, maximum, _ = multi_sync.fetch_job_rows(self.source, job, {})
        self.assertIsNone(maximum.tzinfo)
        self.execute(f'DROP VIEW "{self.schema}"."订单视图"')
        self.execute(f'ALTER TABLE {self.table} ALTER COLUMN "UpdatedAt" TYPE TIMESTAMPTZ USING "UpdatedAt" AT TIME ZONE \'UTC\'')
        rows, maximum, used = multi_sync.fetch_job_rows(self.source, job, {'cursor': multi_sync._encode_cursor(maximum)})
        self.assertFalse(used)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(maximum.utcoffset())
        rows, _, used = multi_sync.fetch_job_rows(self.source, job, {'cursor': multi_sync._encode_cursor(maximum)})
        self.assertTrue(used)
        self.assertEqual(len(rows), 1)

    def test_select_only_account_sees_primary_key_and_identity(self):
        user = 'reader_' + uuid.uuid4().hex[:16]
        self.execute(f'CREATE ROLE "{user}" LOGIN')
        self.addCleanup(lambda: self.execute(f'DROP ROLE "{user}"'))
        self.addCleanup(lambda: self.execute(f'DROP OWNED BY "{user}"'))
        self.execute(f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{user}"')
        self.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{self.schema}" TO "{user}"')
        source = {**self.source, 'user': user}
        self.assertTrue(multi_sync.list_tables(source))
        self.assertEqual(multi_sync.list_primary_key(source, self.schema, '订单'), ['Id'])
        self.assertTrue(multi_sync.list_columns(source, self.schema, '订单')[0]['is_identity'])
        job = multi_sync.automatic_job(source, self.schema, '订单')
        self.assertNotIn('generated_key', job)
        self.assertEqual(len(multi_sync.fetch_job_rows(source, job, {})[0]), 1)

    def test_column_only_account_builds_and_syncs_tasks_without_hidden_keys(self):
        user = 'reader_' + uuid.uuid4().hex[:16]
        self.execute(f'CREATE ROLE "{user}" LOGIN')
        self.addCleanup(lambda: self.execute(f'DROP ROLE "{user}"'))
        self.addCleanup(lambda: self.execute(f'DROP OWNED BY "{user}"'))
        self.execute(f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{user}"')
        for name, key_definition in [('单列主键', 'PRIMARY KEY ("Id")'),
                                     ('复合主键', 'PRIMARY KEY ("Id", "序号")')]:
            relation = f'"{self.schema}"."{name}"'
            self.execute(f'CREATE TABLE {relation} ("Id" int, "序号" int, "名称" text, '
                         f'"UpdatedAt" timestamp, "内部" text, {key_definition})')
            self.execute(f'INSERT INTO {relation} VALUES (1,1,\'中文商品\',\'2026-01-01\',\'内部数据\'),'
                         '(2,1,\'中文商品\',\'2026-01-01\',\'内部数据\')')
            self.execute(f'GRANT SELECT ("序号", "名称", "UpdatedAt") ON {relation} TO "{user}"')
        source = {**self.source, 'user': user}
        workspace.save_config({'sources': [source], 'jobs': []})
        tables = self.request('/api/discovery', {'source_id': source['id']})['tables']
        self.assertEqual({table['name'] for table in tables}, {'单列主键', '复合主键'})
        for name in ('单列主键', '复合主键'):
            metadata = self.request(f'/api/sources/{source["id"]}/columns?schema={self.schema}&table={name}')
            self.assertEqual([col['name'] for col in metadata['columns']], ['序号', '名称', 'UpdatedAt'])
            self.assertEqual(metadata['primary_key'], [])
            preview = self.request(f'/api/sources/{source["id"]}/preview',
                                   {'schema': self.schema, 'table': name, 'columns': ['名称']})
            self.assertEqual(preview['rows'], [['中文商品'], ['中文商品']])
        created = self.request('/api/jobs/bulk', {'tables': tables})
        self.assertEqual(created['errors'], [])
        self.assertEqual(created['created'], 2)
        config = workspace.load_config()
        fake, state = FakeFeishu(), {'jobs': {}}
        for job in config['jobs']:
            self.assertEqual(job['generated_key'], {'columns': []})
            self.assertFalse(job['incremental']['enabled'])
            first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
            repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
            self.assertEqual((first['create'], repeated['create'], repeated['skip']), (2, 0, 2))
            records = fake.records[job['target']['table_id']]
            self.assertEqual(len({row['fields'][job['unique_key']] for row in records}), 2)
            self.assertTrue(all('Id' not in row['fields'] and '内部' not in row['fields'] for row in records))

        # Access to one component must not collapse a composite key to that column.
        self.execute(f'GRANT SELECT ("Id") ON "{self.schema}"."复合主键" TO "{user}"')
        self.assertEqual(multi_sync.list_primary_key(source, self.schema, '复合主键'), ['Id', '序号'])
        refreshed = multi_sync.automatic_job(source, self.schema, '复合主键')
        self.assertEqual(refreshed['generated_key'], {'columns': ['Id', '序号']})
        self.assertEqual(refreshed['incremental'], {'enabled': True, 'column': 'UpdatedAt'})
        saved = workspace.load_config()['jobs'][1]
        self.assertEqual(saved['generated_key'], {'columns': []})

    def test_postgresql_discovery_and_metadata_require_readable_schema_and_columns(self):
        user = 'reader_' + uuid.uuid4().hex[:16]
        self.execute(f'CREATE ROLE "{user}" LOGIN')
        self.addCleanup(lambda: self.execute(f'DROP ROLE "{user}"'))
        self.addCleanup(lambda: self.execute(f'DROP OWNED BY "{user}"'))
        for name in ('只写表', '部分可读', '可读表'):
            self.execute(f'CREATE TABLE "{self.schema}"."{name}" ("Id" int PRIMARY KEY, "名称" text, "内部" text)')
        self.execute(f'GRANT INSERT ON "{self.schema}"."只写表" TO "{user}"')
        self.execute(f'GRANT SELECT ("名称"), UPDATE ("内部") ON "{self.schema}"."部分可读" TO "{user}"')
        self.execute(f'GRANT SELECT ON "{self.schema}"."可读表" TO "{user}"')
        self.execute(f'CREATE VIEW "{self.schema}"."可读视图" AS SELECT * FROM "{self.schema}"."可读表"')
        self.execute(f'GRANT SELECT ON "{self.schema}"."可读视图" TO "{user}"')
        source = {**self.source, 'user': user}
        self.assertEqual(multi_sync.list_tables(source), [])
        for name in ('只写表', '部分可读', '可读表', '可读视图'):
            self.assertEqual(multi_sync.list_columns(source, self.schema, name), [])
            self.assertEqual(multi_sync.list_primary_key(source, self.schema, name), [])
        self.execute(f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{user}"')
        self.assertEqual({table['name'] for table in multi_sync.list_tables(source)}, {'部分可读', '可读表', '可读视图'})
        self.assertEqual(multi_sync.list_columns(source, self.schema, '只写表'), [])
        self.assertEqual([col['name'] for col in multi_sync.list_columns(source, self.schema, '部分可读')], ['名称'])
        self.assertEqual(multi_sync.list_primary_key(source, self.schema, '部分可读'), [])
        self.assertEqual(multi_sync.list_primary_key(source, self.schema, '可读表'), ['Id'])


@unittest.skipUnless(os.environ.get('SQLFEISHU_TEST_MYSQL_JSON'), 'MySQL integration connection not configured')
class MySQLWorkflowTests(SQLiteWorkflowTests):
    engine = 'mysql'
    connection_env = 'SQLFEISHU_TEST_MYSQL_JSON'

    def setUp(self):
        import pymysql
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        info = json.loads(os.environ[self.connection_env])
        self.writer = pymysql.connect(**info, charset='utf8mb4', autocommit=True)
        self.addCleanup(self.writer.close)
        self.execute("SET SESSION time_zone = '+00:00'")
        self.execute("SET SESSION sql_mode = 'ANSI_QUOTES'")
        self.schema = 'test_' + uuid.uuid4().hex
        self.execute(f'CREATE DATABASE "{self.schema}" CHARACTER SET utf8mb4')
        self.addCleanup(lambda: self.execute(f'DROP DATABASE "{self.schema}"'))
        self.source = workspace.validate_source({
            'id': 'source_test', 'name': '中文测试库', 'type': self.engine,
            'tls': {'mode': 'verify-full', 'ca': os.environ['SQLFEISHU_TEST_TLS_CA']} if os.environ.get('SQLFEISHU_TEST_TLS_CA') else {},
            'server': info['host'], 'port': info.get('port', 3306), 'database': self.schema,
            'user': info['user'],
        })
        self.password = mock.patch.dict(os.environ, {self.source['password_env']: info.get('password', '')})
        self.password.start()
        self.addCleanup(self.password.stop)
        self.execute(f'CREATE TABLE "{self.schema}"."订单" ("Id" BIGINT AUTO_INCREMENT PRIMARY KEY, "名称" TEXT, "UpdatedAt" TIMESTAMP NULL, "数据" BLOB)')
        self.execute(f'CREATE VIEW "{self.schema}"."订单视图" AS SELECT * FROM "{self.schema}"."订单"')
        self.finish_setup()

    def test_timestamp_is_absolute_and_datetime_uses_selected_timezone(self):
        self.execute(f'ALTER TABLE {self.table} ADD "LocalTime" DATETIME')
        self.execute(f'UPDATE {self.table} SET "LocalTime" = "UpdatedAt"')
        columns = multi_sync.list_columns(self.source, self.schema, '订单')
        job = multi_sync.automatic_job(self.source, self.schema, '订单')
        rows, maximum, _ = multi_sync.fetch_job_rows(self.source, job, {})
        self.assertEqual(maximum.utcoffset().total_seconds(), 0)
        fields = {c['name']: c['feishu_type'] for c in columns}
        settings = workspace.runtime_settings()
        settings['timezone_offset'] = 8
        record = multi_sync.build_record(rows[0], job['columns'], fields, settings)['fields']
        self.assertEqual(record['UpdatedAt'], 1767268800000)
        self.assertEqual(record['LocalTime'], 1767268800000 - 8 * 3600 * 1000)
        incremental, _, used = multi_sync.fetch_job_rows(self.source, job, {'cursor': multi_sync._encode_cursor(maximum)})
        self.assertTrue(used)
        self.assertEqual(len(incremental), 1)

    def test_zero_timestamp_reports_the_incremental_field(self):
        job = self.create_job()
        # A legacy MySQL database can contain zero dates when strict mode is off.
        self.execute(f'UPDATE {self.table} SET "UpdatedAt" = \'0000-00-00 00:00:00\'')
        self.writer.commit()
        with self.assertRaisesRegex(multi_sync.ConfigError, '增量字段 UpdatedAt.*无效值'):
            multi_sync.fetch_job_rows(self.source, job, {})

    def test_time_cursor_preserves_negative_and_multiday_boundaries(self):
        relation = f'"{self.schema}"."时长边界"'
        self.execute(f'CREATE TABLE {relation} ("Id" int PRIMARY KEY, "时长" TIME(6), "名称" text)')
        values = [
            ('-838:59:59', timedelta(hours=-838, minutes=-59, seconds=-59)),
            ('-25:30:00.123456', timedelta(hours=-25, minutes=-30, microseconds=-123456)),
            ('-01:30:00.000001', timedelta(hours=-1, minutes=-30, microseconds=-1)),
            ('-00:00:00.000001', timedelta(microseconds=-1)),
            ('00:00:00', timedelta()),
            ('00:00:00.000001', timedelta(microseconds=1)),
            ('24:00:00.000001', timedelta(hours=24, microseconds=1)),
            ('838:59:59', timedelta(hours=838, minutes=59, seconds=59)),
        ]
        for index, (text, _) in enumerate(values):
            self.execute(f'INSERT INTO {relation} VALUES (%s,%s,\'中文时长😀\')', (index, text))
        self.execute(f'INSERT INTO {relation} VALUES (99,NULL,\'空时长\')')
        result = self.request('/api/jobs/bulk', {'tables': [
            {'source_id': self.source['id'], 'schema': self.schema, 'name': '时长边界'}]})
        self.assertEqual(result['errors'], [])
        job = result['config']['jobs'][0]
        job['incremental'] = {'enabled': True, 'column': '时长'}
        self.request('/api/jobs', {'job': job})
        preview = self.request('/api/sources/' + self.source['id'] + '/preview',
                               {'schema': self.schema, 'table': '时长边界', 'columns': ['Id', '时长', '名称']})
        self.assertEqual({int(row[0]): row[1] for row in preview['rows']},
                         {**{i: str(value) for i, (_, value) in enumerate(values)}, 99: None})
        for index, (_, value) in enumerate(values):
            rows, maximum, used = multi_sync.fetch_job_rows(
                self.source, job, {'cursor': multi_sync._encode_cursor(value)})
            self.assertTrue(used)
            self.assertEqual([row['Id'] for row in rows], [99] + list(range(index, len(values))))
            self.assertEqual(maximum, values[-1][1])
        for cursor in ({'type': 'str', 'value': str(values[2][1])},
                       {'type': 'timedelta', 'value': True},
                       {'type': 'timedelta', 'value': 10 ** 100},
                       multi_sync._encode_cursor(timedelta(hours=839))):
            rows, _, used = multi_sync.fetch_job_rows(self.source, job, {'cursor': cursor})
            self.assertFalse(used)
            self.assertEqual(len(rows), len(values) + 1)

    def test_time_cursor_upgrade_retry_and_null_rows_keep_saved_progress(self):
        relation = f'"{self.schema}"."时长同步"'
        self.execute(f'CREATE TABLE {relation} ("Id" int PRIMARY KEY, "时长" TIME(6), "名称" text)')
        self.execute(f'INSERT INTO {relation} VALUES (1,\'-25:30:00.123456\',\'旧数据\'),'
                     '(2,\'-01:30:00.000001\',\'边界\'),(3,NULL,\'空时长\')')
        result = self.request('/api/jobs/bulk', {'tables': [
            {'source_id': self.source['id'], 'schema': self.schema, 'name': '时长同步'}]})
        self.assertEqual(result['errors'], [])
        job = result['config']['jobs'][0]
        job['incremental'] = {'enabled': True, 'column': '时长'}
        job['split']['row_limit'] = 1
        self.request('/api/jobs', {'job': job})
        config = workspace.load_config()
        fake, state = FakeFeishu(), {'jobs': {}}
        fake.record_count = lambda *args: None
        first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(first['create'], 3)
        self.assertEqual(state['jobs'][job['id']]['cursor'],
                         multi_sync._encode_cursor(timedelta(hours=-1, minutes=-30, microseconds=-1)))
        repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(repeated['incremental'])
        self.assertEqual((repeated['source_rows'], repeated['skip']), (2, 2))

        # Reproduce a saved string cursor written by the previous version.
        state['jobs'][job['id']]['cursor'] = {'type': 'str', 'value': '-1 day, 22:29:59.999999'}
        workspace.save_state(state)
        self.execute(f'UPDATE {relation} SET "名称"=\'旧记录修正\' WHERE "Id"=1')
        records_before = {row['record_id'] for rows in fake.records.values() for row in rows}
        upgraded = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertFalse(upgraded['incremental'])
        self.assertEqual((upgraded['source_rows'], upgraded['update'], upgraded['create']), (3, 1, 0))
        self.assertEqual(records_before, {row['record_id'] for rows in fake.records.values() for row in rows})

        self.execute(f'INSERT INTO {relation} VALUES (4,\'-00:30:00.000001\',\'新增中文😀\')')
        before, disk_before = copy.deepcopy(state), workspace.STATE_PATH.read_bytes()
        fake.fail_on_batch = fake.batch_count + 1
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(state, before)
        self.assertEqual(workspace.STATE_PATH.read_bytes(), disk_before)
        fake.fail_on_batch = None
        retry = multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertTrue(retry['incremental'])
        self.assertEqual((retry['create'], retry['update'], retry['skip']), (0, 0, 3))
        self.assertEqual(state['jobs'][job['id']]['cursor'],
                         multi_sync._encode_cursor(timedelta(minutes=-30, microseconds=-1)))
        self.assertEqual(sum(len(rows) for rows in fake.records.values()), 4)

    def test_minimal_readonly_account_sees_primary_key_and_metadata(self):
        user = 'reader_' + uuid.uuid4().hex[:16]
        self.execute(f"CREATE USER '{user}'@'127.0.0.1' IDENTIFIED BY 'test-only-password'")
        self.addCleanup(lambda: self.execute(f"DROP USER '{user}'@'127.0.0.1'"))
        self.execute(f'GRANT SELECT ON "{self.schema}".* TO \'{user}\'@\'127.0.0.1\'')
        source = {**self.source, 'user': user}
        with mock.patch.dict(os.environ, {source['password_env']: 'test-only-password'}):
            self.assertTrue(multi_sync.list_tables(source))
            self.assertEqual(multi_sync.list_primary_key(source, self.schema, '订单'), ['Id'])
            self.assertTrue(multi_sync.list_columns(source, self.schema, '订单')[0]['is_identity'])
            job = multi_sync.automatic_job(source, self.schema, '订单')
            self.assertNotIn('generated_key', job)
            self.assertEqual(len(multi_sync.fetch_job_rows(source, job, {})[0]), 1)

    def test_column_permissions_filter_discovery_and_allow_tasks_without_full_keys(self):
        user = 'reader_' + uuid.uuid4().hex[:16]
        account = f"'{user}'@'127.0.0.1'"
        self.execute(f'CREATE USER {account}')
        self.addCleanup(lambda: self.execute(f'DROP USER {account}'))
        for name, primary in [('单列主键', 'PRIMARY KEY ("Id")'),
                              ('复合主键', 'PRIMARY KEY ("Id", "序号")')]:
            relation = f'"{self.schema}"."{name}"'
            self.execute(f'CREATE TABLE {relation} ("Id" int, "序号" int, "名称" text, '
                         f'"UpdatedAt" datetime, "内部" text, {primary})')
            self.execute(f'INSERT INTO {relation} VALUES (1,1,\'中文数据\',\'2026-01-01\',\'内部资料\'),'
                         '(2,1,\'中文数据\',\'2026-01-01\',\'内部资料\')')
            self.execute(f'GRANT SELECT ("序号", "名称", "UpdatedAt"), UPDATE ("内部") ON {relation} TO {account}')
        self.execute(f'CREATE TABLE "{self.schema}"."只写表" ("Id" int, "名称" text)')
        self.execute(f'GRANT INSERT ON "{self.schema}"."只写表" TO {account}')
        self.execute(f'CREATE VIEW "{self.schema}"."只读视图" AS SELECT "名称" FROM "{self.schema}"."单列主键"')
        self.execute(f'GRANT SELECT ON "{self.schema}"."只读视图" TO {account}')
        source = {**self.source, 'user': user}
        workspace.save_config({'sources': [source], 'jobs': []})
        tables = self.request('/api/discovery', {'source_id': source['id']})['tables']
        self.assertEqual({table['name'] for table in tables}, {'单列主键', '复合主键', '只读视图'})
        self.assertEqual(multi_sync.list_columns(source, self.schema, '只写表'), [])
        for name in ('单列主键', '复合主键'):
            metadata = self.request(f'/api/sources/{source["id"]}/columns?schema={self.schema}&table={name}')
            self.assertEqual([col['name'] for col in metadata['columns']], ['序号', '名称', 'UpdatedAt'])
            self.assertEqual(metadata['primary_key'], [])
            preview = self.request('/api/sources/' + source['id'] + '/preview',
                                   {'schema': self.schema, 'table': name, 'columns': ['名称']})
            self.assertEqual(preview['rows'], [['中文数据'], ['中文数据']])
        created = self.request('/api/jobs/bulk', {'tables': tables})
        self.assertEqual(created['errors'], [])
        self.assertEqual(created['created'], 3)
        config = workspace.load_config()
        fake, state = FakeFeishu(), {'jobs': {}}
        for job in config['jobs']:
            self.assertEqual(job['generated_key'], {'columns': []})
            self.assertFalse(job['incremental']['enabled'])
            first = multi_sync.run_job(fake, 'app_test', config, state, job, False)
            repeated = multi_sync.run_job(fake, 'app_test', config, state, job, False)
            self.assertEqual((first['create'], repeated['create'], repeated['update'], repeated['skip']), (2, 0, 0, 2))
            records = fake.records[job['target']['table_id']]
            self.assertEqual(len({row['fields'][job['unique_key']] for row in records}), 2)
            self.assertTrue(all('Id' not in row['fields'] and '内部' not in row['fields'] for row in records))

        self.execute(f'GRANT SELECT ("Id") ON "{self.schema}"."复合主键" TO {account}')
        self.assertEqual(multi_sync.list_primary_key(source, self.schema, '复合主键'), ['Id', '序号'])
        refreshed = multi_sync.automatic_job(source, self.schema, '复合主键')
        self.assertEqual(refreshed['generated_key'], {'columns': ['Id', '序号']})
        self.assertEqual(refreshed['incremental'], {'enabled': True, 'column': 'UpdatedAt'})

        # A later grant revocation must stop the saved plan before record writes.
        job = next(job for job in config['jobs'] if job['table'] == '复合主键')
        self.execute(f'REVOKE SELECT ("名称") ON "{self.schema}"."复合主键" FROM {account}')
        before = copy.deepcopy(fake.records)
        saved_state = workspace.STATE_PATH.read_bytes()
        with self.assertRaisesRegex(multi_sync.ConfigError, '字段不存在.*名称'):
            multi_sync.run_job(fake, 'app_test', config, state, job, False)
        self.assertEqual(fake.records, before)
        self.assertEqual(workspace.STATE_PATH.read_bytes(), saved_state)

    def test_role_grants_are_used_for_readable_columns_and_primary_keys(self):
        suffix = uuid.uuid4().hex[:12]
        user, role = 'reader_' + suffix, 'role_' + suffix
        account = f"'{user}'@'127.0.0.1'"
        self.execute(f'CREATE ROLE "{role}"')
        self.addCleanup(lambda: self.execute(f'DROP ROLE "{role}"'))
        self.execute(f'CREATE USER {account}')
        self.addCleanup(lambda: self.execute(f'DROP USER {account}'))
        self.execute(f'GRANT SELECT ("Id", "名称", "UpdatedAt") ON {self.table} TO "{role}"')
        self.execute(f'GRANT "{role}" TO {account}')
        if self.engine == 'mysql':
            self.execute(f'SET DEFAULT ROLE "{role}" TO {account}')
        else:
            self.execute(f'SET DEFAULT ROLE "{role}" FOR {account}')
        source = {**self.source, 'user': user}
        self.assertEqual([table['name'] for table in multi_sync.list_tables(source)], ['订单'])
        self.assertEqual([col['name'] for col in multi_sync.list_columns(source, self.schema, '订单')], ['Id', '名称', 'UpdatedAt'])
        self.assertEqual(multi_sync.list_primary_key(source, self.schema, '订单'), ['Id'])
        job = multi_sync.automatic_job(source, self.schema, '订单')
        self.assertNotIn('generated_key', job)
        self.assertTrue(job['incremental']['enabled'])
        rows, _, _ = multi_sync.fetch_job_rows(source, job, {})
        self.assertEqual(rows[0]['名称'], '中文商品😀')
        self.assertNotIn('数据', rows[0])



@unittest.skipUnless(os.environ.get('SQLFEISHU_TEST_MARIADB_JSON'), 'MariaDB integration connection not configured')
class MariaDBWorkflowTests(MySQLWorkflowTests):
    __unittest_skip__ = not bool(os.environ.get('SQLFEISHU_TEST_MARIADB_JSON'))
    engine = 'mariadb'
    connection_env = 'SQLFEISHU_TEST_MARIADB_JSON'


class DatabaseCompatibilityTests(unittest.TestCase):
    def test_timestamp_semantics_are_engine_specific(self):
        self.assertEqual(multi_sync.sql_type_to_feishu('timestamp'), 1)
        for kind in ('mysql', 'mariadb', 'postgresql', 'sqlite'):
            self.assertEqual(multi_sync.sql_type_to_feishu(databases.normalize_type(kind, 'timestamp')), 5)
        self.assertEqual(databases.normalize_type('mysql', 'bit'), 'bigint')
        self.assertEqual(databases.normalize_type('postgresql', 'bit'), 'text')
        self.assertEqual(databases.normalize_type('postgresql', 'money'), 'text')

    def test_legacy_source_and_cursor_fingerprint_survive_type_default(self):
        source = {'id': 'a', 'name': '旧库', 'server': 'localhost', 'database': 'master', 'port': 1433, 'user': ''}
        job = {'schema': 'dbo', 'table': 'Orders'}
        old = multi_sync.job_fingerprint(source, job, 'app', 'tbl', {})
        self.assertEqual(old, multi_sync.job_fingerprint(workspace.validate_source(source), job, 'app', 'tbl', {}))
        self.assertNotEqual(old, multi_sync.job_fingerprint({**source, 'type': 'postgresql'}, job, 'app', 'tbl', {}))

    def test_missing_sqlite_file_is_not_created(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'missing.db'
            source = workspace.validate_source({'name': 'missing', 'type': 'sqlite', 'database': str(path)})
            with self.assertRaisesRegex(ValueError, '不存在'):
                with multi_sync.sql_connection(source):
                    pass
            self.assertFalse(path.exists())

    def test_timezone_fix_invalidates_legacy_nondefault_conversion(self):
        import hashlib
        source = {'id': 'a', 'server': 'localhost', 'port': 1433, 'database': 'master', 'user': ''}
        job = {'schema': 'dbo', 'table': 'Orders'}
        settings = workspace.runtime_settings()
        settings['timezone_offset'] = 0
        identity = {
            'version': 3,
            'source': source,
            'job': {key: job.get(key) for key in ('schema', 'table', 'columns', 'unique_key', 'incremental', 'split')},
            'app_token': 'app', 'table_id': 'tbl', 'field_types': {},
            'timezone': 0, 'null_policy': settings['null_policy'],
        }
        old = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        with mock.patch.dict(os.environ, {'SYNC_TIMEZONE_OFFSET': '8'}):
            self.assertNotEqual(old, multi_sync.job_fingerprint(source, job, 'app', 'tbl', {}, settings))

    def test_identifier_escaping_and_limit(self):
        for kind, quote in [('mysql', '`'), ('mariadb', '`'), ('postgresql', '"'), ('sqlite', '"')]:
            source = {'type': kind}
            query = databases.select_query(source, '业务', '订单' + quote, ['1'], limit=10)
            self.assertIn(quote + '订单' + quote * 3, query)
            self.assertTrue(query.endswith(' LIMIT 10'))

class MySQLDriverTests(unittest.TestCase):
    def test_connection_uses_utf8_streaming_and_readonly_transaction(self):
        import pymysql
        for kind in ('mysql', 'mariadb'):
            source = workspace.validate_source({'name': kind, 'type': kind, 'server': 'localhost', 'database': '业务库'})
            connection = mock.MagicMock()
            with self.subTest(kind=kind), mock.patch.object(pymysql, 'connect', return_value=connection) as connect:
                with multi_sync.sql_connection(source):
                    pass
                self.assertEqual(connect.call_args.kwargs['charset'], 'utf8mb4')
                self.assertEqual(connect.call_args.kwargs['cursorclass'], pymysql.cursors.SSCursor)
                connection.cursor.return_value.__enter__.return_value.execute.assert_any_call("SET SESSION time_zone = '+00:00'")
                connection.cursor.return_value.__enter__.return_value.execute.assert_called_with('START TRANSACTION READ ONLY')
                connection.close.assert_called_once()

    def test_metadata_normalizes_dates_and_preserves_bit_values(self):
        source = {'type': 'mysql', 'database': '业务库'}
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchall.return_value = [
            ('Id', 'bigint', 'NO', 1, 'auto_increment', 1, 64),
            ('UpdatedAt', 'timestamp', 'YES', 2, '', 1, None),
            ('Flags', 'bit', 'YES', 3, '', 1, 16),
        ]
        with mock.patch.object(databases, 'connect') as connect:
            connect.return_value.__enter__.return_value = connection
            columns = multi_sync.list_columns(source, '业务库', '订单')
        self.assertEqual(columns[1]['sql_type'], 'datetime2')
        self.assertEqual(columns[1]['feishu_type'], 5)
        self.assertEqual(multi_sync.decode_sql_value(b'\x01\x00', columns[2], source), 256)
        self.assertTrue(columns[0]['is_identity'])
        self.assertEqual(cursor.execute.call_args.args[1], ('业务库', '订单'))

    def test_mysql_incremental_query_has_quoted_names_and_bound_cursor(self):
        source = {'type': 'mysql', 'database': '业务库'}
        job = {'name': '订单', 'schema': '业务库', 'table': '订单', 'unique_key': 'Id',
               'columns': [{'source': 'Id'}, {'source': 'UpdatedAt'}],
               'incremental': {'enabled': True, 'column': 'UpdatedAt'}}
        columns = [{'name': 'Id', 'sql_type': 'int'}, {'name': 'UpdatedAt', 'sql_type': 'datetime2'}]
        saved = datetime(2026, 1, 1, 12)
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchmany.side_effect = [[(1, saved)], []]
        with mock.patch.object(multi_sync, 'list_columns', return_value=columns), mock.patch.object(multi_sync, 'sql_connection') as connect:
            connect.return_value.__enter__.return_value = connection
            rows, maximum, incremental = multi_sync.fetch_job_rows(source, job, {'cursor': multi_sync._encode_cursor(saved)})
        self.assertTrue(incremental)
        self.assertEqual(maximum, saved)
        cursor.execute.assert_called_once_with('SELECT `Id`, `UpdatedAt` FROM `业务库`.`订单` WHERE (`UpdatedAt` >= %s OR `UpdatedAt` IS NULL) ORDER BY `UpdatedAt`, `Id`', (saved,))
        self.assertEqual(rows, [{'Id': 1, 'UpdatedAt': saved}])
