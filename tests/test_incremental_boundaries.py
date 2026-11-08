"""Cursor recovery and engine-specific types at incremental boundaries."""
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

import multi_sync
from sync import ConfigError


class CursorBoundaryTests(unittest.TestCase):
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
