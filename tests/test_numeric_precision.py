"""Prevent silent decimal rounding and preserve exact numeric task mappings."""
import copy
import hashlib
import json
import unittest
from decimal import Decimal
from unittest import mock

import databases
import multi_sync
import sync
import workspace


class NumericConversionTests(unittest.TestCase):
    def test_safe_numbers_and_scientific_notation(self):
        for value, expected in [('1e3', 1000), ('1.25e-3', .00125), (Decimal('12.50'), 12.5),
                                (Decimal('1.000'), 1), (9007199254740991, 9007199254740991), (True, 1)]:
            with self.subTest(value=value):
                self.assertEqual(sync.convert_value(value, 2), expected)

    def test_inexact_and_nonfinite_numbers_are_rejected(self):
        values = [9007199254740993, -9007199254740993, Decimal('1.0000000000000001'),
                  Decimal('123456789012345.6789'), Decimal('1e-400'), Decimal('NaN'),
                  Decimal('sNaN'), float('inf'), float('-inf'), 'not a number']
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(sync.ConfigError):
                    sync.convert_value(value, 2)
                self.assertEqual(sync.convert_value(value, 1), str(value))

    def test_inexact_decimal_key_cannot_match_another_record(self):
        with self.assertRaisesRegex(sync.ConfigError, '唯一键 Id.*精度'):
            multi_sync.validate_row_keys([{'Id': Decimal('1.0000000000000001')}], 'Id', '订单', 2)
        self.assertEqual(multi_sync.validate_row_keys([{'Id': Decimal('1.0000000000000001')}], 'Id', '订单', 1),
                         ['1.0000000000000001'])

    def test_conversion_error_names_the_field_in_both_entrypoints(self):
        row = {'amount': Decimal('123456789012345.6789')}
        with self.assertRaisesRegex(sync.ConfigError, 'amount → 金额.*精度'):
            multi_sync.build_record(row, [{'source': 'amount', 'target': '金额'}], {'金额': 2})
        with self.assertRaisesRegex(sync.ConfigError, 'amount → 金额.*精度'):
            sync.build_record(row, [{'sql': 'amount', 'feishu': '金额'}], {'金额': 2})


class NumericMappingTests(unittest.TestCase):
    def job(self):
        return {'id': 'job', 'source_id': 'source', 'table': 'Orders', 'unique_key': 'Id',
                'columns': [{'source': 'Id', 'target': 'Id', 'sql_type': 'bigint', 'target_type': 1},
                            {'source': 'Amount', 'target': 'Amount', 'sql_type': 'decimal', 'target_type': 2}]}

    def test_override_roundtrip_and_validation(self):
        job = workspace.validate_job(self.job(), ['source'])
        self.assertEqual([column['target_type'] for column in job['columns']], [1, 2])
        self.assertEqual([multi_sync.column_field_type(column, 'Id') for column in job['columns']], [1, 2])
        for value in (True, '1', None, 5, 3, -1):
            bad = self.job()
            bad['columns'][0]['target_type'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '字段类型'):
                workspace.validate_job(bad, ['source'])
        legacy = self.job()
        for column in legacy['columns']:
            column.pop('target_type')
        self.assertNotIn('target_type', workspace.validate_job(legacy, ['source'])['columns'][0])

    def test_existing_target_type_must_match_explicit_setting(self):
        with self.assertRaisesRegex(sync.ConfigError, 'Id.*类型.*不一致'):
            multi_sync.validate_target_fields(self.job(), {'Id': 2, 'Amount': 2})
        multi_sync.validate_target_fields(self.job(), {'Id': 1, 'Amount': 2})

    def test_automatic_jobs_choose_lossless_types_and_keep_incremental_id(self):
        for kind in ('mssql', 'postgresql', 'mysql', 'mariadb', 'sqlite'):
            source = {'id': 'source', 'name': '客户库', 'type': kind}
            columns = [{'name': 'Id', 'sql_type': 'bigint', 'nullable': False, 'is_identity': True, 'identity_increment': 1},
                       {'name': 'Amount', 'sql_type': 'decimal', 'nullable': True, 'numeric_precision': 30},
                       {'name': 'Small', 'sql_type': 'decimal', 'nullable': True, 'numeric_precision': 10}]
            with self.subTest(engine=kind), mock.patch.object(multi_sync, 'list_columns', return_value=columns), \
                 mock.patch.object(multi_sync, 'list_primary_key', return_value=['Id']):
                job = multi_sync.automatic_job(source, 'dbo', 'Orders')
                self.assertEqual([c.get('target_type') for c in job['columns']], [1, 1, None])
                self.assertEqual(job['incremental'], {'enabled': True, 'column': 'Id'})

    def test_sql_server_precision_metadata_drives_recommended_types(self):
        connection = mock.MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [('Id', 'bigint', 'NO', 1, 1, 1, 19),
                                       ('Amount', 'decimal', 'YES', 2, 0, 0, 30),
                                       ('Small', 'decimal', 'YES', 3, 0, 0, 10)]
        with mock.patch.object(multi_sync, 'sql_connection', return_value=connection):
            columns = multi_sync.list_columns({'type': 'mssql'}, 'dbo', 'Orders')
        self.assertEqual([column['feishu_type'] for column in columns], [1, 1, 2])
        self.assertEqual([column['numeric_precision'] for column in columns], [19, 30, 10])

    def test_numeric_conversion_version_resets_legacy_cursor(self):
        source = {'id': 'source', 'server': 'db', 'port': 1433, 'database': 'Business', 'user': 'reader'}
        job = self.job()
        settings = workspace.runtime_settings()
        settings['timezone_offset'] = 8
        identity = {'version': 3, 'source': source,
                    'job': {key: job.get(key) for key in ('schema', 'table', 'columns', 'unique_key', 'incremental', 'split')},
                    'app_token': 'app', 'table_id': 'tbl', 'field_types': {'Id': 1, 'Amount': 2},
                    'timezone': 8, 'null_policy': settings['null_policy']}
        legacy = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with mock.patch.dict('os.environ', {'SYNC_TIMEZONE_OFFSET': '8'}):
            self.assertNotEqual(multi_sync.job_fingerprint(source, job, 'app', 'tbl', identity['field_types'], settings), legacy)
