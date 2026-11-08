"""Real database regressions, with optional PostgreSQL, MySQL and MariaDB fixtures."""
import copy
import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime
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
