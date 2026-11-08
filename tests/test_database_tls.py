"""TLS settings, authentication boundary and opt-in real database handshakes."""
import copy
import datetime
import json
import os
import socketserver
import ssl
import struct
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import dashboard
import databases
import multi_sync
import workspace


def write_ca(path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test-only unrelated CA')])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .sign(key, hashes.SHA256()))
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


class TLSConfigurationTests(unittest.TestCase):
    def source(self, kind='mysql', **tls):
        return workspace.validate_source({'id': 'tls_source', 'name': 'TLS 测试', 'type': kind,
                                          'server': 'localhost', 'database': 'test', 'user': 'reader', 'tls': tls})

    def test_legacy_defaults_and_explicit_modes(self):
        for kind in ('mssql', 'mysql', 'mariadb', 'postgresql'):
            with self.subTest(kind=kind):
                self.assertEqual(databases.tls_options(self.source(kind)), {})
        self.assertEqual(databases.tls_options(self.source('mssql', mode='require')), {'encryption': 'require'})
        self.assertEqual(databases.tls_options(self.source('mssql', mode='disable')), {'encryption': 'off'})
        self.assertEqual(databases.tls_options(self.source(mode='disable')), {'ssl_disabled': True})
        options = databases.tls_options(self.source('postgresql', mode='verify-full'))
        self.assertEqual(options, {'sslmode': 'verify-full', 'sslrootcert': 'system', 'gssencmode': 'disable'})
        context = databases.tls_options(self.source(mode='verify-full'))['ssl']
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        context = databases.tls_options(self.source(mode='require'))['ssl']
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)
        with mock.patch.object(multi_sync.pymssql, 'connect') as connect:
            multi_sync.sql_connection(self.source('mssql', mode='require'))
        self.assertEqual(connect.call_args.kwargs['encryption'], 'require')

    def test_invalid_settings_and_missing_files(self):
        for tls in ({'mode': []}, {'mode': 'unknown'}, {'mode': 'verify-full', 'ca': 'relative.pem'},
                    {'mode': 'require', 'ca': '/tmp/ca.pem'}, {'mode': 'require', 'cert': '/tmp/cert.pem'},
                    {'mode': 'default', 'ca': '/tmp/ca.pem'}, {'key': None}):
            with self.subTest(tls=tls), self.assertRaises(ValueError):
                self.source(**tls)
        with self.assertRaisesRegex(ValueError, '不支持'):
            self.source('mssql', mode='verify-full')
        with tempfile.TemporaryDirectory() as folder:
            # Save paths even when unavailable so users can edit moved certificates.
            source = self.source(mode='verify-full', ca=str(Path(folder) / 'missing.pem'))
            with self.assertRaisesRegex(ValueError, 'CA 证书文件不存在'):
                databases.tls_options(source)
            for name in ('cert.pem', 'key.pem'):
                (Path(folder) / name).write_text('invalid PEM')
            for kind in ('postgresql', 'mysql'):
                source = self.source(kind, mode='require', cert=str(Path(folder) / 'cert.pem'), key=str(Path(folder) / 'key.pem'))
                with self.subTest(engine=kind), self.assertRaisesRegex(ValueError, '证书加载失败'):
                    databases.tls_options(source)
        source = self.source('postgresql', mode='require')
        source['server'] = '/tmp'
        with self.assertRaisesRegex(ValueError, 'TCP'):
            workspace.validate_source(source)

    def test_old_api_edit_preserves_tls_and_cursor_identity(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.multiple(
            workspace, CONFIG_PATH=Path(folder) / 'config.json', ENV_PATH=Path(folder) / '.env'
        ):
            original = self.source('postgresql', mode='verify-full')
            workspace.save_config({'sources': [original], 'jobs': []})
            payload = copy.deepcopy(original)
            del payload['tls']
            payload['name'] = '重命名'
            headers = {'Host': '127.0.0.1:5001', 'X-CSRF-Token': dashboard.CSRF_TOKEN}
            response = dashboard.app.test_client().post('/api/sources', json={'source': payload}, headers=headers)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(workspace.load_config()['sources'][0]['tls'], original['tls'])
            job = {'schema': 'public', 'table': 'Orders'}
            self.assertEqual(multi_sync.job_fingerprint(original, job, 'app', 'tbl', {}),
                             multi_sync.job_fingerprint(payload, job, 'app', 'tbl', {}))
            payload['tls'] = {'mode': 'disable'}
            response = dashboard.app.test_client().post('/api/sources', json={'source': payload}, headers=headers)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(workspace.load_config()['sources'][0]['tls']['mode'], 'disable')

    def test_mysql_server_without_tls_receives_no_authentication(self):
        received = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                # A real MySQL greeting without CLIENT.SSL, using the production driver parser.
                flags = databases.pymysql.constants.CLIENT.PROTOCOL_41 | databases.pymysql.constants.CLIENT.SECURE_CONNECTION
                greeting = (b'\x0a8.0.0\x00' + struct.pack('<I', 1) + b'12345678\x00'
                            + struct.pack('<H', flags) + b'\x2d' + struct.pack('<HHB', 2, 0, 21)
                            + b'\x00' * 10 + b'123456789012\x00')
                self.request.sendall(struct.pack('<I', len(greeting))[:3] + b'\x00' + greeting)
                self.request.settimeout(5)
                received.append(self.request.recv(4096))

        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for mode in ('require', 'verify-full'):
                    source = self.source(mode=mode)
                    source.update(server='127.0.0.1', port=server.server_address[1])
                    with self.assertRaisesRegex(ValueError, '发送认证信息前'):
                        with multi_sync.sql_connection(source):
                            self.fail('TLS downgrade')
            finally:
                server.shutdown()
                worker.join()
        self.assertEqual(received, [b'', b''])


@unittest.skipUnless(os.environ.get('SQLFEISHU_TEST_TLS_SOURCES'), 'Real TLS sources not configured')
class RealTLSHandshakeTests(unittest.TestCase):
    def test_encrypted_verified_and_rejected_connections(self):
        with tempfile.TemporaryDirectory() as folder:
            wrong_ca = Path(folder) / 'wrong-ca.pem'
            write_ca(wrong_ca)
            for raw in json.loads(os.environ['SQLFEISHU_TEST_TLS_SOURCES']):
                source = workspace.validate_source(raw)
                for mode in ('require', 'verify-full', 'disable'):
                    tls = {**source['tls'], 'mode': mode}
                    if mode != 'verify-full':
                        tls['ca'] = ''
                    if mode == 'disable':
                        tls.update(cert='', key='')
                    with self.subTest(engine=source['type'], mode=mode):
                        with multi_sync.sql_connection({**source, 'tls': tls}) as connection:
                            if source['type'] == 'postgresql':
                                with connection.cursor() as cursor:
                                    cursor.execute('SELECT ssl, client_dn FROM pg_stat_ssl WHERE pid = pg_backend_pid()')
                                    encrypted, client_dn = cursor.fetchone()
                                    self.assertEqual(encrypted, mode != 'disable')
                                    if mode != 'disable' and tls['cert']:
                                        self.assertTrue(client_dn)
                            else:
                                with connection.cursor() as cursor:
                                    cursor.execute("SHOW STATUS LIKE 'Ssl_cipher'")
                                    self.assertEqual(bool(cursor.fetchone()[1]), mode != 'disable')
                            with databases.cursor_for(connection, source) as cursor:
                                cursor.execute('SELECT %s', ('中文 TLS 😀',))
                                self.assertEqual(cursor.fetchone()[0], '中文 TLS 😀')
                for broken in (
                    {**source, 'tls': {**source['tls'], 'ca': str(wrong_ca)}},
                    {**source, 'server': '127.0.0.1'},  # Fixture certificate only covers localhost.
                ):
                    with self.subTest(engine=source['type'], broken_host=broken['server']):
                        with self.assertRaises(Exception) as error:
                            with multi_sync.sql_connection(broken):
                                pass
                        self.assertIn('certificate', str(error.exception).lower())
                        with mock.patch.object(dashboard.logger, 'exception'):
                            self.assertIn('TLS 证书校验失败', dashboard.safe_error(error.exception, '连接失败'))

    def test_mysql_client_certificate_required_account(self):
        for raw in json.loads(os.environ['SQLFEISHU_TEST_TLS_SOURCES']):
            if raw['type'] not in {'mysql', 'mariadb'} or not raw['tls'].get('cert'):
                continue
            source = workspace.validate_source(raw)
            username = 'tls_' + uuid.uuid4().hex[:16]
            with self.subTest(engine=source['type']), databases.pymysql.connect(
                host=source['server'], port=source['port'], user=source['user'],
                password=workspace.source_password(source), autocommit=True,
            ) as admin:
                with admin.cursor() as cursor:
                    cursor.execute(f"CREATE USER '{username}'@'%' REQUIRE X509")
                try:
                    reader = {**source, 'user': username, 'database': ''}
                    with mock.patch.dict(os.environ, {source['password_env']: ''}):
                        with multi_sync.sql_connection(reader) as connection:
                            with connection.cursor() as cursor:
                                cursor.execute('SELECT 1')
                                self.assertEqual(cursor.fetchone(), (1,))
                        reader['tls'] = {**source['tls'], 'cert': '', 'key': ''}
                        with self.assertRaises(databases.pymysql.err.OperationalError):
                            with multi_sync.sql_connection(reader):
                                pass
                finally:
                    with admin.cursor() as cursor:
                        cursor.execute(f"DROP USER '{username}'@'%'")


@unittest.skipUnless(os.environ.get('SQLFEISHU_TEST_NO_TLS_SOURCES'), 'Plaintext-only sources not configured')
class RealTLSRequiredTests(unittest.TestCase):
    def test_plaintext_servers_reject_required_tls(self):
        for raw in json.loads(os.environ['SQLFEISHU_TEST_NO_TLS_SOURCES']):
            source = workspace.validate_source({**raw, 'tls': {'mode': 'require'}})
            with self.subTest(engine=source['type']):
                with multi_sync.sql_connection({**source, 'tls': {'mode': 'disable'}}):
                    pass
                with self.assertRaises(Exception) as error:
                    with multi_sync.sql_connection(source):
                        pass
                expected = 'server does not support SSL' if source['type'] == 'postgresql' else '未启用 TLS'
                self.assertIn(expected, str(error.exception))
