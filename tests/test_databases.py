"""Real database regressions. PostgreSQL runs when SQLFEISHU_TEST_POSTGRES_DSN is set."""
import copy
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
        self.writer.execute('CREATE TABLE "订单" ("Id" INTEGER PRIMARY KEY AUTOINCREMENT, "名称" TEXT, "UpdatedAt" DATETIME, "数据" BLOB)')
        self.writer.execute('CREATE VIEW "订单视图" AS SELECT * FROM "订单"')
        self.finish_setup()

    def finish_setup(self):
        self.table = databases.qualified_table(self.source, self.schema, '订单')
        self.marker = databases.placeholder(self.source)
        self.writer.execute(f'INSERT INTO {self.table} ("名称", "UpdatedAt", "数据") VALUES ({self.marker}, {self.marker}, {self.marker})',
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
        self.writer.execute(f'UPDATE {self.table} SET "名称" = {self.marker}, "UpdatedAt" = {self.marker}',
                            ('中文更新', '2026-01-02 12:00:00'))
        self.writer.execute(f'INSERT INTO {self.table} ("名称", "UpdatedAt") VALUES ({self.marker}, {self.marker})',
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

    def test_source_is_read_only(self):
        with multi_sync.sql_connection(self.source) as connection, databases.cursor_for(connection, self.source) as cursor:
            with self.assertRaises(Exception):
                cursor.execute(f'DELETE FROM {self.table}')
        self.assertEqual(self.writer.execute(f'SELECT COUNT(*) FROM {self.table}').fetchone()[0], 1)

    def test_quoted_percent_identifiers_through_config_preview_and_fetch(self):
        name = '计量%表"历史'
        quoted = f'"{self.schema}"."计量%表""历史"'
        self.writer.execute(f'CREATE TABLE {quoted} ("Id" INTEGER PRIMARY KEY, "含税%金额" TEXT)')
        self.writer.execute(f"INSERT INTO {quoted} VALUES (1, '中文值')")
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
            'server': info['host'], 'port': info.get('port', 5432), 'database': info['dbname'], 'user': info['user'],
        })
        self.password = mock.patch.dict(os.environ, {self.source['password_env']: info.get('password', '')})
        self.password.start()
        self.addCleanup(self.password.stop)
        self.writer = psycopg.connect(dsn, autocommit=True)
        self.addCleanup(self.writer.close)
        self.schema = 'test_' + uuid.uuid4().hex
        self.writer.execute(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(lambda: self.writer.execute(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.writer.execute(f'CREATE TABLE "{self.schema}"."订单" ("Id" BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "名称" TEXT, "UpdatedAt" TIMESTAMP, "数据" BYTEA)')
        self.writer.execute(f'CREATE VIEW "{self.schema}"."订单视图" AS SELECT * FROM "{self.schema}"."订单"')
        self.finish_setup()


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
                connection.cursor.return_value.__enter__.return_value.execute.assert_called_with('START TRANSACTION READ ONLY')
                connection.close.assert_called_once()

    def test_metadata_normalizes_dates_and_preserves_bit_values(self):
        source = {'type': 'mysql', 'database': '业务库'}
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchall.return_value = [
            ('Id', 'bigint', 'NO', 1, 'auto_increment', 1),
            ('UpdatedAt', 'timestamp', 'YES', 2, '', 1),
            ('Flags', 'bit', 'YES', 3, '', 1),
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
        cursor.execute.assert_called_once_with('SELECT `Id`, `UpdatedAt` FROM `业务库`.`订单` WHERE `UpdatedAt` >= %s ORDER BY `UpdatedAt`, `Id`', (saved,))
        self.assertEqual(rows, [{'Id': 1, 'UpdatedAt': saved}])
