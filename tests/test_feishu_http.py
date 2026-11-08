"""Exercise the production HTTP client against a loopback Feishu fixture."""
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import multi_sync
import sync
import workspace
from test_split_sync import FakeFeishu


class LocalFeishu:
    def __init__(self, respond):
        self.calls = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                request = {'method': self.command, 'url': urlparse(self.path),
                           'body': json.loads(body) if body else None,
                           'authorization': self.headers.get('Authorization')}
                fixture.calls.append(request)
                status, payload, headers = respond(request)
                data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Type', 'application/json')
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = handle_request
            do_POST = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = sync.FeishuClient('test-app', 'test-secret',
                                         f'http://127.0.0.1:{self.server.server_port}/open-apis')
        self.client.session.trust_env = False

    def close(self):
        self.client.session.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class FeishuHttpTests(unittest.TestCase):
    def scripted(self, responses):
        pending = deque(responses)
        fixture = LocalFeishu(lambda request: pending.popleft())
        fixture.client.token = 'local-fixture-' + uuid.uuid4().hex
        self.addCleanup(fixture.close)
        return fixture

    def test_html_gateway_and_rate_limit_retry_before_json_parsing(self):
        fixture = self.scripted([(502, b'<html>gateway</html>', {}),
                                 (429, b'<html>busy</html>', {'Retry-After': '0'}),
                                 (200, {'code': 0, 'data': {'items': []}}, {})])
        with mock.patch.object(sync.time, 'sleep') as sleep:
            self.assertEqual(fixture.client.list_tables('app'), [])
        self.assertEqual(len(fixture.calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_retry_budget_and_client_token_are_stable_on_writes(self):
        fixture = self.scripted([(503, b'busy', {'Retry-After': 'invalid'})] * 5)
        with mock.patch.object(sync.time, 'sleep') as sleep, self.assertRaisesRegex(sync.FeishuAPIError, '重试后仍失败'):
            fixture.client.batch_create('app', 'tbl', [{'fields': {'名称': '中文'}}])
        self.assertEqual(sleep.call_count, 4)
        tokens = [parse_qs(call['url'].query)['client_token'][0] for call in fixture.calls]
        self.assertEqual(len(set(tokens)), 1)
        self.assertTrue(all(call['body'] == fixture.calls[0]['body'] for call in fixture.calls))

    def test_empty_invalid_or_missing_status_success_response_fails(self):
        for payload in (b'', b'<html>login</html>', [], {}, {'code': '0'}, {'code': False}):
            with self.subTest(payload=payload):
                fixture = self.scripted([(200, payload, {})])
                with self.assertRaises(sync.FeishuAPIError):
                    fixture.client.batch_update('app', 'tbl', [])
                self.assertEqual(len(fixture.calls), 1)

    def test_permission_denial_is_not_repeated_as_token_refresh(self):
        fixture = self.scripted([(403, {'code': 1254302, 'msg': 'permission denied'}, {})])
        with self.assertRaisesRegex(sync.FeishuAPIError, '1254302'):
            fixture.client.list_tables('app')
        self.assertEqual(len(fixture.calls), 1)

    def test_token_refresh_and_auth_gateway_retry(self):
        fixture = self.scripted([
            (200, {'code': 99991663, 'msg': 'expired'}, {}),
            (502, b'<html>auth gateway</html>', {}),
            (200, {'code': 0, 'tenant_access_token': 'new-token'}, {}),
            (200, {'code': 0, 'data': {'items': []}}, {}),
        ])
        with mock.patch.object(sync.time, 'sleep'):
            self.assertEqual(fixture.client.list_tables('app'), [])
        self.assertEqual(fixture.calls[-1]['authorization'], 'Bearer new-token')
        self.assertTrue(fixture.calls[1]['url'].path.endswith('/tenant_access_token/internal'))

    def test_retry_after_accepts_http_date_and_bounds_invalid_values(self):
        now = datetime.now(timezone.utc)
        for header in ('-1', 'NaN', 'inf', 'bad', '999999999', format_datetime(now + timedelta(seconds=30), usegmt=True)):
            with self.subTest(header=header):
                delay = sync._retry_delay(mock.Mock(headers={'Retry-After': header}), 0)
                self.assertGreaterEqual(delay, 0)
                self.assertLessEqual(delay, 60)

    def test_all_readers_reject_missing_and_cyclic_pagination(self):
        for reader in ('tables', 'fields', 'records', 'search'):
            for tokens in (['a', 'b', 'a'], [None], [123]):
                pages = [(200, {'code': 0, 'data': {'items': [], 'has_more': True, 'page_token': token}}, {})
                         for token in tokens]
                fixture = self.scripted(pages)
                with self.subTest(reader=reader, tokens=tokens), self.assertRaises(sync.FeishuAPIError):
                    if reader == 'tables':
                        fixture.client.list_tables('app')
                    elif reader == 'fields':
                        fixture.client.list_fields('app', 'tbl')
                    elif reader == 'records':
                        list(fixture.client.iter_records('app', 'tbl', ['Id']))
                    else:
                        list(fixture.client.iter_records_by_keys('app', 'tbl', ['Id'], 'Id', ['1']))
                self.assertEqual(len(fixture.calls), len(tokens))

    def test_valid_multipage_results_and_invalid_page_shapes(self):
        fixture = self.scripted([
            (200, {'code': 0, 'data': {'items': [{'table_id': 'one'}], 'has_more': True, 'page_token': 'next'}}, {}),
            (200, {'code': 0, 'data': {'items': [{'table_id': 'two'}], 'has_more': False}}, {}),
        ])
        self.assertEqual(fixture.client.list_tables('app'), [{'table_id': 'one'}, {'table_id': 'two'}])
        self.assertEqual(parse_qs(fixture.calls[-1]['url'].query)['page_token'], ['next'])
        for page in (None, [], {'items': {}}, {'items': [], 'has_more': 'false'}):
            fixture = self.scripted([(200, {'code': 0, 'data': page}, {})])
            with self.subTest(page=page), self.assertRaises(sync.FeishuAPIError):
                fixture.client.list_tables('app')


class DatabaseToHttpTests(unittest.TestCase):
    def test_actual_sqlite_to_http_write_retry_and_incremental_read(self):
        fake = FakeFeishu()
        accepted = {}
        gateway_sent = False
        invalid_update_response = False

        def respond(request):
            nonlocal gateway_sent
            path = request['url'].path
            query = parse_qs(request['url'].query)
            body = request['body']
            if path.endswith('/internal'):
                return 200, {'code': 0, 'tenant_access_token': 'local-fixture-' + uuid.uuid4().hex}, {}
            if path.endswith('/tables'):
                if request['method'] == 'GET':
                    data = {'items': fake.list_tables('app'), 'has_more': False}
                else:
                    data = fake.request('POST', path, json=body)['data']
            else:
                table_id = path.split('/tables/')[1].split('/')[0]
                if path.endswith('/fields'):
                    data = {'items': fake.list_fields('app', table_id), 'has_more': False}
                elif path.endswith('/batch_create'):
                    token = query['client_token'][0]
                    if token not in accepted:
                        fake.batch_create('app', table_id, body['records'])
                        accepted[token] = copy.deepcopy(body)
                    if not gateway_sent:
                        gateway_sent = True
                        return 502, b'<html>response lost</html>', {}
                    data = {}
                elif path.endswith('/batch_update'):
                    if invalid_update_response:
                        return 200, {}, {}
                    fake.batch_update('app', table_id, body['records'])
                    data = {}
                elif path.endswith('/search'):
                    keys = {condition['value'][0] for condition in body['filter']['conditions']}
                    data = {'items': [r for r in fake.records[table_id] if str(r['fields']['Id']) in keys], 'has_more': False}
                else:
                    data = {'items': fake.records[table_id], 'total': len(fake.records[table_id]), 'has_more': False}
            return 200, {'code': 0, 'data': data}, {}

        fixture = LocalFeishu(respond)
        self.addCleanup(fixture.close)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = workspace.validate_source({'id': 'source', 'name': 'HTTP 测试', 'type': 'sqlite', 'database': str(root / 'source.db')})
            with sqlite3.connect(source['database']) as writer:
                writer.execute('CREATE TABLE Orders (Id INTEGER PRIMARY KEY AUTOINCREMENT, Name TEXT, UpdatedAt DATETIME)')
                writer.execute("INSERT INTO Orders (Name, UpdatedAt) VALUES ('中文初始😀', '2026-01-01 00:00:00')")
                writer.commit()
                with mock.patch.multiple(workspace, CONFIG_PATH=root / 'config.json', STATE_PATH=root / 'state.json'), \
                     mock.patch.object(sync.time, 'sleep'):
                    job = multi_sync.automatic_job(source, 'main', 'Orders')
                    # Exercise explicitly numeric keys and the key-filter HTTP path.
                    job['columns'][0]['target_type'] = 2
                    config = workspace.save_config({'sources': [source], 'jobs': [job]})
                    state = {'jobs': {}}
                    first = multi_sync.run_job(fixture.client, 'app', config, state, config['jobs'][0], False)
                    self.assertEqual(first['create'], 1)
                    table_id = config['jobs'][0]['target']['table_id']
                    self.assertEqual(len(fake.records[table_id]), 1)
                    self.assertEqual(fake.records[table_id][0]['fields']['Name'], '中文初始😀')
                    writer.execute("UPDATE Orders SET Name='中文更新', UpdatedAt='2026-01-02 00:00:00'")
                    writer.commit()
                    second = multi_sync.run_job(fixture.client, 'app', config, state, config['jobs'][0], False)
                    self.assertEqual(second['update'], 1)
                    self.assertEqual(second['lookup_mode'], 'keys')
                    third = multi_sync.run_job(fixture.client, 'app', config, state, config['jobs'][0], False)
                    self.assertEqual(third['skip'], 1)
                    self.assertEqual(len(fake.records[table_id]), 1)
                    self.assertTrue((root / 'state.json').exists())
                    previous_state = copy.deepcopy(state)
                    saved_state = (root / 'state.json').read_bytes()
                    writer.execute("UPDATE Orders SET Name='待重试', UpdatedAt='2026-01-03 00:00:00'")
                    writer.commit()
                    invalid_update_response = True
                    with self.assertRaises(sync.FeishuAPIError):
                        multi_sync.run_job(fixture.client, 'app', config, state, config['jobs'][0], False)
                    self.assertEqual(state, previous_state)
                    self.assertEqual((root / 'state.json').read_bytes(), saved_state)
                    invalid_update_response = False
                    retried = multi_sync.run_job(fixture.client, 'app', config, state, config['jobs'][0], False)
                    self.assertEqual(retried['update'], 1)
                    self.assertEqual(fake.records[table_id][0]['fields']['Name'], '待重试')
        writes = [call for call in fixture.calls if call['url'].path.endswith('/batch_create')]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0]['url'].query, writes[1]['url'].query)
