"""Cursor recovery and engine-specific types at incremental boundaries."""
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import multi_sync
import databases
from sync import ConfigError


class CursorBoundaryTests(unittest.TestCase):
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
