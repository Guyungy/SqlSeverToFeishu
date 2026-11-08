import unittest
from decimal import Decimal
from unittest import mock

import dashboard
import multi_sync
import sync
import workspace


class UnicodeSyncTests(unittest.TestCase):
    def source(self):
        return workspace.validate_source({'id': 'source_cn', 'name': '业务数据库',
                                          'server': 'localhost', 'database': '订单库'})

    def test_chinese_identifiers_with_units_and_parentheses_are_quoted(self):
        for name in ['订单表(历史)', '金额(元)', '产品名称(中文)', '说明（含税）', '规格]名称(中文)']:
            with self.subTest(name=name):
                self.assertEqual(sync.quote_identifier(name), '[' + name.replace(']', ']]') + ']')
        self.assertEqual(sync.quoted_table('业务.订单表(历史)'), '[业务].[订单表(历史)]')
        with self.assertRaises(sync.ConfigError):
            sync.quoted_table('dbo.fn()')

    def test_automatic_task_and_staging_preserve_chinese_names_and_values(self):
        source = self.source()
        metadata = [{'name': '订单号', 'sql_type': 'nvarchar', 'nullable': False},
                    {'name': '产品名称(中文)', 'sql_type': 'nvarchar', 'nullable': True},
                    {'name': '金额(元)', 'sql_type': 'decimal', 'nullable': True, 'numeric_precision': 12}]
        connection = mock.MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchmany.side_effect = [[('单据甲', '中文商品，型号甲', Decimal('12.50'))], []]
        client = mock.Mock()
        client.list_tables.return_value = []
        client.iter_records.return_value = iter([])
        with mock.patch.object(multi_sync, 'list_columns', return_value=metadata), \
             mock.patch.object(multi_sync, 'list_primary_key', return_value=['订单号']), \
             mock.patch.object(multi_sync, 'sql_connection', return_value=connection), \
             mock.patch.object(multi_sync, 'ensure_target', return_value=('tbl', {'订单号': 1, '产品名称(中文)': 1, '金额(元)': 2}, 'existing')), \
             mock.patch.object(multi_sync, 'save_state'):
            job = multi_sync.automatic_job(source, '业务', '订单表(历史)')
            config = workspace.validate_config({'sources': [source], 'jobs': [job]})
            summary = multi_sync.run_job(client, 'app', config, {'jobs': {}}, config['jobs'][0], False)
        query = cursor.execute.call_args.args[0]
        self.assertIn('[业务].[订单表(历史)]', query)
        self.assertIn('CAST([产品名称(中文)] AS NVARCHAR(MAX)) AS [产品名称(中文)]', query)
        connection.__enter__.return_value.cursor.assert_called_once_with()
        self.assertEqual(summary['create'], 1)
        fields = client.batch_create.call_args.args[2][0]['fields']
        self.assertEqual(fields['订单号'], '单据甲')
        self.assertEqual(fields['产品名称(中文)'], '中文商品，型号甲')
        self.assertEqual(fields['金额(元)'], 12.5)

    def test_text_projection_preserves_full_length_and_numeric_types(self):
        for sql_type in ('char', 'varchar', 'text', 'nchar', 'nvarchar', 'ntext'):
            with self.subTest(sql_type=sql_type):
                self.assertEqual(multi_sync.sql_column_expression({'name': '说明', 'sql_type': sql_type}),
                                 'CAST([说明] AS NVARCHAR(MAX)) AS [说明]')
        self.assertEqual(multi_sync.sql_column_expression({'name': '金额', 'sql_type': 'decimal'}), '[金额]')

    def test_invalid_text_bytes_raise_instead_of_silently_replacing_chinese(self):
        column = {'name': '说明', 'sql_type': 'varchar'}
        self.assertEqual(multi_sync.decode_sql_value('中文'.encode('utf-8'), column), '中文')
        with self.assertRaisesRegex(sync.ConfigError, '无法按 UTF-8 解码'):
            multi_sync.decode_sql_value('中文'.encode('gbk'), column)
        self.assertIsNone(multi_sync.decode_sql_value(None, column))
        self.assertEqual(multi_sync.decode_sql_value('', column), '')

    def test_preview_uses_unicode_projection_and_decodes_text(self):
        connection = mock.MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchmany.return_value = [('中文内容'.encode('utf-8'),)]
        with mock.patch.object(dashboard, 'load_config', return_value={'sources': [self.source()]}), \
             mock.patch.object(dashboard, 'list_columns', return_value=[{'name': '说明', 'sql_type': 'text'}]), \
             mock.patch.object(dashboard, 'sql_connection', return_value=connection):
            response = dashboard.app.test_client().post('/api/sources/source_cn/preview',
                json={'schema': 'dbo', 'table': '订单', 'columns': ['说明']},
                headers={'Host': '127.0.0.1:5001', 'X-CSRF-Token': dashboard.CSRF_TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['rows'], [['中文内容']])
        cursor.execute.assert_any_call('SET TEXTSIZE 2147483647')
        self.assertIn('CAST([说明] AS NVARCHAR(MAX)) AS [说明]', cursor.execute.call_args.args[0])

    def test_database_connection_check_reports_chinese_success(self):
        response = self.check_connection('中文编码校验')
        self.assertEqual(response.status_code, 200)
        self.assertIn('中文读取校验通过', response.get_json()['message'])

    def test_database_connection_check_detects_garbled_chinese(self):
        response = self.check_connection('??????')
        self.assertEqual(response.status_code, 400)
        self.assertIn('中文读取校验失败', response.get_json()['message'])

    def check_connection(self, sample):
        connection = mock.MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [('订单库', 'SQL_SERVER'), (sample,)]
        with mock.patch.object(dashboard, 'load_config', return_value={'sources': [self.source()]}), \
             mock.patch.object(dashboard, 'sql_connection', return_value=connection):
            return dashboard.app.test_client().post('/api/sources/source_cn/test', json={},
                headers={'Host': '127.0.0.1:5001', 'X-CSRF-Token': dashboard.CSRF_TOKEN})
