"""Cursor recovery and engine-specific types at incremental boundaries."""
import unittest
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import multi_sync
import databases
from sync import ConfigError


class CursorBoundaryTests(unittest.TestCase):
    def test_binary_cursors_round_trip_and_reject_malformed_hex(self):
        for value in (b'', b'\x00\xff', '中文'.encode('utf-8'), b'\x80',
                      bytearray(b'\x00\x01'), memoryview(b'\xff\x00')):
            with self.subTest(value=value):
                encoded = multi_sync._encode_cursor(value)
                self.assertEqual(encoded, {'type': 'bytes', 'value': bytes(value).hex()})
                self.assertEqual(multi_sync._decode_cursor(encoded), bytes(value))
        for value in ('xyz', '0', '00 ff', '\\x00ff', None, 123, True, ['00']):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '二进制增量游标格式错误'):
                multi_sync._decode_cursor({'type': 'bytes', 'value': value})
        for engine, sql_type in [('mssql','timestamp'), ('mssql','rowversion'), ('mssql','image'),
                                 ('postgresql','bytea'), ('sqlite','blob'), ('mysql','varbinary'),
                                 ('mariadb','longblob')]:
            source = {'type': engine}
            self.assertTrue(multi_sync._cursor_fits_sql_type(b'\x00\xff', sql_type, source))
            self.assertFalse(multi_sync._cursor_fits_sql_type('00ff', sql_type, source))
        self.assertFalse(databases.is_binary_column({'sql_type': 'timestamp'}, {'type': 'mysql'}))
        self.assertFalse(databases.is_binary_column({'sql_type': 'bigint', 'native_type': 'bit'}, {'type': 'mysql'}))

    def test_sql_server_rowversion_cursor_binds_bytes_and_keeps_row_values(self):
        source = {'type': 'mssql'}
        job = {'name': '版本进度', 'schema': 'dbo', 'table': 'Orders', 'unique_key': 'Id',
               'columns': [{'source': 'Id'}, {'source': '版本'}],
               'incremental': {'enabled': True, 'column': '版本'}}
        for sql_type in ('timestamp', 'rowversion', 'binary', 'varbinary', 'image'):
            metadata = [{'name': 'Id', 'sql_type': 'int'}, {'name': '版本', 'sql_type': sql_type}]
            connection = mock.MagicMock()
            cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchmany.side_effect = [[(1, None), (2, b'\x00\x01')], [(3, b'\x00\xff')], []]
            with self.subTest(sql_type=sql_type), \
                 mock.patch.object(multi_sync, 'sql_connection', return_value=connection), \
                 mock.patch.object(multi_sync, 'list_columns', return_value=metadata):
                rows, maximum, used = multi_sync.fetch_job_rows(source, job, {'cursor': {'type': 'bytes', 'value': '0001'}})
            self.assertTrue(used)
            self.assertEqual(maximum, b'\x00\xff')
            self.assertEqual(rows[-1]['版本'], b'\x00\xff')
            expression = 'CAST([版本] AS VARBINARY(MAX))' if sql_type == 'image' else '[版本]'
            cursor.execute.assert_called_with(
                f'SELECT [Id], [版本] FROM [dbo].[Orders] WHERE ({expression} >= %s OR {expression} IS NULL) '
                f'ORDER BY {expression}, [Id]', (b'\x00\x01',))
            cursor.fetchmany.side_effect = [[(1, b'\x00\xff')], []]
            with mock.patch.object(multi_sync, 'sql_connection', return_value=connection), \
                 mock.patch.object(multi_sync, 'list_columns', return_value=metadata):
                _, _, used = multi_sync.fetch_job_rows(source, job, {'cursor': {'type': 'str', 'value': "b'\\x00\\xff'"}})
            self.assertFalse(used)
            self.assertNotIn('WHERE', cursor.execute.call_args.args[0])

    def test_non_numeric_incremental_plans_invalidate_old_comparison_fingerprints(self):
        dumps = json.dumps
        for engine in ('mssql', 'sqlite', 'postgresql', 'mysql', 'mariadb'):
            source = {'type': engine}
            for sql_type in ('varchar', 'uuid', 'json', 'enum', 'time', 'datetime2', 'int'):
                job = {'columns': [{'source': '进度', 'sql_type': sql_type}],
                       'incremental': {'enabled': True, 'column': '进度'}}
                current = multi_sync.job_fingerprint(source, job, 'app', 'table', {})
                with mock.patch.object(multi_sync.json, 'dumps', side_effect=lambda value, **kwargs:
                                       dumps({k: v for k, v in value.items() if k != 'cursor_comparison_version'}, **kwargs)):
                    old = multi_sync.job_fingerprint(source, job, 'app', 'table', {})
                with self.subTest(engine=engine, sql_type=sql_type):
                    self.assertEqual(current == old, sql_type in {'datetime2', 'int'})

    def test_sql_server_text_cursor_uses_database_order_across_batches(self):
        source = {'type': 'mssql'}
        job = {'name': '文本进度', 'schema': 'dbo', 'table': 'Orders', 'unique_key': 'Id',
               'columns': [{'source': 'Id'}, {'source': '进度'}],
               'incremental': {'enabled': True, 'column': '进度'}}
        metadata = [{'name': 'Id', 'sql_type': 'int'}, {'name': '进度', 'sql_type': 'ntext'}]
        connection = mock.MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchmany.side_effect = [[(1, None), (2, 'a')], [(3, 'Z')], []]
        with mock.patch.object(multi_sync, 'sql_connection', return_value=connection), \
             mock.patch.object(multi_sync, 'list_columns', return_value=metadata):
            rows, maximum, used = multi_sync.fetch_job_rows(source, job, {'cursor': {'type': 'str', 'value': 'a'}})
        self.assertTrue(used)
        self.assertEqual(len(rows), 3)
        self.assertEqual(maximum, 'Z')
        cursor.execute.assert_called_with(
            'SELECT [Id], CAST([进度] AS NVARCHAR(MAX)) AS [进度] FROM [dbo].[Orders] '
            'WHERE (CAST([进度] AS NVARCHAR(MAX)) >= %s OR CAST([进度] AS NVARCHAR(MAX)) IS NULL) '
            'ORDER BY CAST([进度] AS NVARCHAR(MAX)), [Id]', ('a',))

    def test_mysql_time_cursor_preserves_signed_microseconds(self):
        values = [
            (timedelta(hours=-838, minutes=-59, seconds=-59), '-838:59:59.000000'),
            (timedelta(hours=-25, minutes=-30, microseconds=-123456), '-25:30:00.123456'),
            (timedelta(microseconds=-1), '-00:00:00.000001'),
            (timedelta(), '00:00:00.000000'),
            (timedelta(microseconds=1), '00:00:00.000001'),
            (timedelta(hours=25, microseconds=123456), '25:00:00.123456'),
            (timedelta(hours=838, minutes=59, seconds=59), '838:59:59.000000'),
        ]
        for engine in ('mysql', 'mariadb'):
            source = {'type': engine}
            for value, text in values:
                with self.subTest(engine=engine, value=value):
                    saved = multi_sync._encode_cursor(value)
                    self.assertEqual(saved['type'], 'timedelta')
                    self.assertEqual(multi_sync._decode_cursor(saved), value)
                    self.assertTrue(multi_sync._cursor_fits_sql_type(value, 'time', source))
                    self.assertEqual(databases.cursor_parameter(value, {'native_type': 'time'}, source), text)
            for value in ('-1 day, 23:59:59', '24:00:00', timedelta(hours=839), timedelta(hours=-839), 0):
                self.assertFalse(multi_sync._cursor_fits_sql_type(value, 'time', source))
        for value in (True, 1.5, '123', None):
            with self.assertRaisesRegex(ValueError, '时长增量游标格式错误'):
                multi_sync._decode_cursor({'type': 'timedelta', 'value': value})

    def test_dates_and_timezone_changes_force_full_scan(self):
        naive = datetime(2026, 1, 1)
        aware = naive.replace(tzinfo=timezone.utc)
        self.assertTrue(multi_sync._cursor_fits_sql_type(date(2026, 1, 1), 'date'))
        self.assertFalse(multi_sync._cursor_fits_sql_type(naive, 'date'))
        self.assertFalse(multi_sync._cursor_fits_sql_type(date(2026, 1, 1), 'datetime2'))
        self.assertTrue(multi_sync._cursor_fits_sql_type(aware, 'datetimeoffset'))
        self.assertFalse(multi_sync._cursor_fits_sql_type(naive, 'datetimeoffset'))
        self.assertFalse(multi_sync._cursor_fits_sql_type(aware, 'datetime2'))
        for engine in ('mysql', 'mariadb'):
            self.assertTrue(multi_sync._cursor_fits_sql_type(aware, 'datetime2', {'type': engine}, {'native_type': 'timestamp'}))
            self.assertFalse(multi_sync._cursor_fits_sql_type(naive, 'datetime2', {'type': engine}, {'native_type': 'timestamp'}))
            self.assertTrue(multi_sync._cursor_fits_sql_type(naive, 'datetime2', {'type': engine}, {'native_type': 'datetime'}))

    def test_invalid_and_nonfinite_cursors_are_rejected(self):
        for cursor in ([], 'bad', {}, {'type': 'unknown', 'value': 'x'},
                       {'type': 'datetime', 'value': 'broken'}, {'type': 'int', 'value': 1.5},
                       {'type': 'int', 'value': True}, {'type': 'str', 'value': 100},
                       {'type': 'decimal', 'value': 1.5}, {'type': 'float', 'value': True}):
            with self.subTest(cursor=cursor), self.assertRaises((ValueError, TypeError)):
                multi_sync._decode_cursor(cursor)
        for value in (float('nan'), float('inf'), Decimal('NaN'), Decimal('Infinity')):
            self.assertFalse(multi_sync._cursor_fits_sql_type(value, 'decimal'))
        self.assertFalse(multi_sync._cursor_fits_sql_type(Decimal('1.9'), 'int'))
        self.assertFalse(multi_sync._cursor_fits_sql_type(1.9, 'bigint'))
        self.assertTrue(multi_sync._cursor_fits_sql_type(9007199254740993, 'bigint'))
        self.assertFalse(multi_sync._cursor_fits_sql_type('0000-00-00', 'datetime2', {'type': 'sqlite'}))
        self.assertFalse(multi_sync._cursor_fits_sql_type('invalid-date', 'datetime2', {'type': 'sqlite'}))
        self.assertTrue(multi_sync._cursor_fits_sql_type('2026-01-01 12:00:00.123456', 'datetime2', {'type': 'sqlite'}))
        self.assertIs(multi_sync._decode_cursor(multi_sync._encode_cursor(True)), True)

    def test_sql_server_null_filter_and_invalid_source_values(self):
        source = {'type': 'mssql'}
        job = {'name': '订单', 'schema': 'dbo', 'table': 'Orders', 'unique_key': 'Id',
               'columns': [{'source': 'Id'}, {'source': 'UpdatedAt'}],
               'incremental': {'enabled': True, 'column': 'UpdatedAt'}}
        metadata = [{'name': 'Id', 'sql_type': 'int'}, {'name': 'UpdatedAt', 'sql_type': 'datetime2', 'nullable': True}]
        connection = mock.MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        saved = datetime(2026, 1, 1)
        cursor.fetchmany.side_effect = [[(1, saved), (2, None)], []]
        with mock.patch.object(multi_sync, 'sql_connection', return_value=connection), \
             mock.patch.object(multi_sync, 'list_columns', return_value=metadata):
            rows, maximum, used = multi_sync.fetch_job_rows(source, job, {'cursor': multi_sync._encode_cursor(saved)})
        self.assertTrue(used)
        self.assertEqual(len(rows), 2)
        self.assertEqual(maximum, saved)
        cursor.execute.assert_called_with('SELECT [Id], [UpdatedAt] FROM [dbo].[Orders] WHERE ([UpdatedAt] >= %s OR [UpdatedAt] IS NULL) ORDER BY [UpdatedAt], [Id]', (saved,))
        metadata[1]['sql_type'] = 'decimal'
        cursor.fetchmany.side_effect = [[(1, Decimal('NaN'))], []]
        sink = mock.Mock()
        with mock.patch.object(multi_sync, 'sql_connection', return_value=connection), \
             mock.patch.object(multi_sync, 'list_columns', return_value=metadata), \
             self.assertRaisesRegex(ConfigError, '增量字段 UpdatedAt.*无效值'):
            multi_sync.fetch_job_rows(source, job, None, row_sink=sink)
        sink.assert_not_called()
