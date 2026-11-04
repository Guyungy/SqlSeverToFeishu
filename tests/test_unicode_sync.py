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
                    {'name': '金额(元)', 'sql_type': 'decimal', 'nullable': True}]
        connection = mock.MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchmany.side_effect = [[{'订单号': '单据甲', '产品名称(中文)': '中文商品，型号甲', '金额(元)': Decimal('12.50')}], []]
        client = mock.Mock()
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
        self.assertIn('[产品名称(中文)]', query)
        self.assertEqual(summary['create'], 1)
        fields = client.batch_create.call_args.args[2][0]['fields']
        self.assertEqual(fields['订单号'], '单据甲')
        self.assertEqual(fields['产品名称(中文)'], '中文商品，型号甲')
        self.assertEqual(fields['金额(元)'], 12.5)

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
