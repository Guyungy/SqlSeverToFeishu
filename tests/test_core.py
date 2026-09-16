import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

import dashboard
import multi_sync
import sync
import workspace
from feishu_link import parse_feishu_base_url


class IdentifierTests(unittest.TestCase):
    def test_quotes_valid_schema_and_table(self):
        self.assertEqual(sync.quoted_table("dbo.Orders"), "[dbo].[Orders]")
        self.assertEqual(sync.quoted_table("订单表"), "[dbo].[订单表]")

    def test_rejects_sql_fragments(self):
        for value in ("dbo.Orders; DROP TABLE x", "dbo.Orders--", "dbo.fn()", "a.b.c"):
            with self.subTest(value=value), self.assertRaises(sync.ConfigError):
                sync.quoted_table(value)


class LegacyMappingTests(unittest.TestCase):
    def test_unique_key_label_is_derived_from_mapping(self):
        result = sync.validate_mapping({
            "mapping": [{"sql": "OrderId", "feishu": "订单号"}],
            "unique_key": "OrderId",
            "unique_key_label": "错误标签",
        })
        self.assertEqual(result["unique_key_label"], "订单号")


class WorkspaceConfigTests(unittest.TestCase):
    def sample(self):
        return {
            "version": 2,
            "sources": [{
                "id": "source_a", "name": "订单库", "server": "127.0.0.1", "port": 1433,
                "database": "Orders", "user": "reader", "password_env": "SQL_SOURCE_SOURCE_A_PASSWORD",
                "enabled": True,
            }],
            "jobs": [{
                "id": "job_orders", "name": "订单", "source_id": "source_a", "schema": "dbo",
                "table": "Orders", "enabled": True,
                "columns": [
                    {"source": "OrderId", "target": "订单号", "sql_type": "int", "nullable": False},
                    {"source": "UpdatedAt", "target": "更新时间", "sql_type": "datetime2", "nullable": False},
                ],
                "unique_key": "OrderId",
                "incremental": {"enabled": True, "column": "UpdatedAt"},
                "target": {"mode": "auto", "table_id": "", "table_name": "订单库_订单", "auto_create_fields": True},
            }],
        }

    def test_supports_multiple_sources_and_incremental_job(self):
        data = self.sample()
        second = dict(data["sources"][0], id="source_b", name="库存库", database="Stock", password_env="SQL_SOURCE_SOURCE_B_PASSWORD")
        data["sources"].append(second)
        result = workspace.validate_config(data)
        self.assertEqual(len(result["sources"]), 2)
        self.assertTrue(result["jobs"][0]["incremental"]["enabled"])

    def test_unique_and_incremental_fields_must_be_selected(self):
        data = self.sample()
        data["jobs"][0]["incremental"]["column"] = "Missing"
        with self.assertRaises(ValueError):
            workspace.validate_config(data)

    def test_duplicate_target_names_are_rejected(self):
        data = self.sample()
        data["jobs"][0]["columns"][1]["target"] = "订单号"
        with self.assertRaises(ValueError):
            workspace.validate_config(data)

    def test_blank_database_defaults_to_master_for_discovery(self):
        source = workspace.validate_source({"name": "待选择", "server": "db.local", "database": ""})
        self.assertEqual(source["database"], "master")


class IncrementalTests(unittest.TestCase):
    def test_cursor_round_trip(self):
        values = [datetime(2026, 1, 1, 8, 30), Decimal("12.50"), 8, 2.5, "A"]
        for value in values:
            with self.subTest(value=value):
                encoded = multi_sync._encode_cursor(value)
                self.assertEqual(str(multi_sync._decode_cursor(encoded)), str(value))

    def test_sql_types_map_to_feishu_types(self):
        self.assertEqual(multi_sync.sql_type_to_feishu("bigint"), 2)
        self.assertEqual(multi_sync.sql_type_to_feishu("datetime2"), 5)
        self.assertEqual(multi_sync.sql_type_to_feishu("bit"), 7)
        self.assertEqual(multi_sync.sql_type_to_feishu("nvarchar"), 1)

    def test_numeric_business_keys_are_canonical(self):
        self.assertEqual(multi_sync.normalize_business_key(1, 2), "1")
        self.assertEqual(multi_sync.normalize_business_key(1.0, 2), "1")
        self.assertEqual(multi_sync.normalize_business_key(Decimal("1.00"), 2), "1")

    def test_duplicate_source_keys_are_rejected(self):
        with self.assertRaises(sync.ConfigError):
            multi_sync.validate_row_keys([{"id": 1}, {"id": 1.0}], "id", "测试任务", 2)

    def test_duplicate_target_keys_are_rejected(self):
        records = [
            {"record_id": "r1", "fields": {"订单号": 1}},
            {"record_id": "r2", "fields": {"订单号": 1.0}},
        ]
        with self.assertRaises(sync.ConfigError):
            multi_sync.target_record_map(records, "订单号", 2)


class FeishuProvisioningTests(unittest.TestCase):
    def test_create_table_uses_selected_fields_and_unique_key_first(self):
        calls = []

        class FakeClient:
            def request(self, method, path, **kwargs):
                calls.append((method, path, kwargs))
                return {"data": {"table": {"table_id": "tbl_new"}}}

        job = {
            "unique_key": "OrderId",
            "columns": [
                {"source": "Name", "target": "名称", "sql_type": "nvarchar"},
                {"source": "OrderId", "target": "订单号", "sql_type": "int"},
                {"source": "UpdatedAt", "target": "更新时间", "sql_type": "datetime2"},
            ],
            "target": {"table_name": "订单归档"},
        }
        table_id = multi_sync.create_table(FakeClient(), "app_token", job)
        self.assertEqual(table_id, "tbl_new")
        payload = calls[0][2]["json"]["table"]
        self.assertEqual(payload["name"], "订单归档")
        self.assertEqual(payload["fields"][0]["field_name"], "订单号")
        self.assertEqual([field["type"] for field in payload["fields"]], [2, 1, 5])


class ConversionTests(unittest.TestCase):
    def test_number_boolean_and_date_conversion(self):
        self.assertEqual(sync.convert_value("12", 2), 12)
        self.assertEqual(sync.convert_value("12.5", 2), 12.5)
        self.assertTrue(sync.convert_value("是", 7))
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(sync.convert_value(dt, 5), 1767225600000)

    def test_multi_record_null_fields_are_skipped(self):
        with mock.patch.dict(os.environ, {"SYNC_NULL_POLICY": "skip"}):
            record = multi_sync.build_record(
                {"A": None, "B": 3},
                [{"source": "A", "target": "空"}, {"source": "B", "target": "数字"}],
                {"空": 1, "数字": 2},
            )
        self.assertEqual(record, {"fields": {"数字": 3}})


class LinkParsingTests(unittest.TestCase):
    def test_parses_percent_encoded_query(self):
        result = parse_feishu_base_url(
            "https://acme.feishu.cn/base/BakExampleAppToken001?table=tblABC%2D123&view=vew1"
        )
        self.assertEqual(result["table_id"], "tblABC-123")

    def test_rejects_malicious_host_and_wiki_link(self):
        with self.assertRaises(ValueError):
            parse_feishu_base_url("https://evil.example/base/BakExampleAppToken001?table=tbl1")
        with self.assertRaises(ValueError):
            parse_feishu_base_url("https://acme.feishu.cn/wiki/wikcn123456")


class SecretPersistenceTests(unittest.TestCase):
    def test_partial_update_preserves_other_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text('FEISHU_APP_SECRET="real-secret"\nSQL_SOURCE_A_PASSWORD="db-secret"\n', encoding="utf-8")
            with mock.patch.object(workspace, "ENV_PATH", env_path), mock.patch.object(workspace, "BASE_DIR", Path(temp_dir)):
                workspace.update_env({"FEISHU_APP_ID": "cli_new"})
            content = env_path.read_text(encoding="utf-8")
            self.assertIn('FEISHU_APP_SECRET="real-secret"', content)
            self.assertIn('SQL_SOURCE_A_PASSWORD="db-secret"', content)
            self.assertIn('FEISHU_APP_ID="cli_new"', content)


class DashboardSecurityTests(unittest.TestCase):
    def test_csrf_and_host_are_enforced(self):
        client = dashboard.app.test_client()
        self.assertEqual(client.get("/", headers={"Host": "127.0.0.1:5001"}).status_code, 200)
        self.assertEqual(client.post("/api/run", headers={"Host": "127.0.0.1:5001"}, json={}).status_code, 403)
        self.assertEqual(client.get("/api/workspace", headers={"Host": "evil.example"}).status_code, 403)


class FingerprintAndCursorTests(unittest.TestCase):
    """配置变更后必须作废旧增量游标，否则会崩溃或静默漏数据。"""

    def base_job(self):
        return {
            "id": "job_1", "name": "订单", "source_id": "source_a", "schema": "dbo",
            "table": "Orders", "enabled": True,
            "columns": [
                {"source": "OrderId", "target": "订单号", "sql_type": "int", "nullable": False},
                {"source": "Amount", "target": "金额", "sql_type": "decimal", "nullable": True},
                {"source": "UpdatedAt", "target": "更新时间", "sql_type": "datetime2", "nullable": False},
            ],
            "unique_key": "OrderId",
            "incremental": {"enabled": True, "column": "UpdatedAt"},
            "target": {"mode": "auto", "table_id": "tbl_1", "table_name": "订单", "auto_create_fields": True},
        }

    def source(self):
        return {"id": "source_a", "server": "127.0.0.1", "port": 1433, "database": "Orders", "user": "reader"}

    def fingerprint(self, job, table_id=None):
        types = {c["target"]: multi_sync.column_field_type(c, job["unique_key"]) for c in job["columns"]}
        return multi_sync.job_fingerprint(
            self.source(), job, "app_token", table_id or job["target"]["table_id"], types
        )

    def test_fingerprint_changes_when_sync_semantics_change(self):
        job = self.base_job()
        baseline = self.fingerprint(job)
        cases = {
            "换增量字段": lambda j: j["incremental"].update(column="OrderId"),
            "换唯一键": lambda j: j.update(unique_key="OrderId", columns=[
                {"source": "Amount", "target": "金额", "sql_type": "decimal", "nullable": True},
                {"source": "OrderId", "target": "订单号", "sql_type": "int", "nullable": False},
            ]),
            "改字段名": lambda j: j["columns"][1].update(target="成交金额"),
            "换目标表": lambda j: j["target"].update(table_id="tbl_2"),
            "关闭再打开增量": lambda j: j["incremental"].update(enabled=False),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                changed = self.base_job()
                mutate(changed)
                self.assertNotEqual(self.fingerprint(changed), baseline, f"{label} 未改变指纹")

    def test_cursor_type_mismatch_is_detected(self):
        self.assertTrue(multi_sync._cursor_fits_sql_type(datetime(2026, 1, 1), "datetime2"))
        self.assertTrue(multi_sync._cursor_fits_sql_type(Decimal("1.5"), "decimal"))
        self.assertTrue(multi_sync._cursor_fits_sql_type("abc", "nvarchar"))
        self.assertFalse(multi_sync._cursor_fits_sql_type(datetime(2026, 1, 1), "int"))
        self.assertFalse(multi_sync._cursor_fits_sql_type(2, "datetime2"))
        self.assertFalse(multi_sync._cursor_fits_sql_type("2", "bigint"))

    def test_stale_cursor_forces_full_scan_instead_of_crashing(self):
        """旧游标是 datetime，改用整型增量字段后必须回退全量而不是抛类型错误。"""
        job = self.base_job()
        job["incremental"]["column"] = "OrderId"
        columns = [
            {"name": "OrderId", "sql_type": "int", "nullable": False, "ordinal": 1},
            {"name": "Amount", "sql_type": "decimal", "nullable": True, "ordinal": 2},
            {"name": "UpdatedAt", "sql_type": "datetime2", "nullable": False, "ordinal": 3},
        ]
        executed = {}

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, params=()):
                executed["query"] = query
                executed["params"] = params

            def fetchmany(self, size):
                return []

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self, as_dict=False):
                return FakeCursor()

        state = {"cursor": multi_sync._encode_cursor(datetime(2026, 1, 1, 8, 0))}
        with mock.patch.object(multi_sync, "list_columns", lambda s, sc, t: columns), \
             mock.patch.object(multi_sync, "sql_connection", lambda source, database=None: FakeConn()):
            rows, max_cursor, incremental_run = multi_sync.fetch_job_rows(self.source(), job, state)
        self.assertFalse(incremental_run)
        self.assertIsNone(max_cursor)
        self.assertNotIn("WHERE", executed["query"])
        self.assertEqual(executed["params"], ())

    def test_matching_cursor_still_uses_incremental_query(self):
        job = self.base_job()
        columns = [
            {"name": "OrderId", "sql_type": "int", "nullable": False, "ordinal": 1},
            {"name": "Amount", "sql_type": "decimal", "nullable": True, "ordinal": 2},
            {"name": "UpdatedAt", "sql_type": "datetime2", "nullable": False, "ordinal": 3},
        ]
        executed = {}

        class FakeCursor:
            """只返回一批数据，避免 mock 无限循环。"""

            def __init__(self, batches):
                self.batches = list(batches)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, params=()):
                executed["query"] = query
                executed["params"] = params

            def fetchmany(self, size):
                return self.batches.pop(0) if self.batches else []

        class FakeConn:
            def __init__(self, batches):
                self.batches = batches

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self, as_dict=False):
                return FakeCursor(self.batches)

        boundary = datetime(2026, 1, 1, 8, 0)
        state = {"cursor": multi_sync._encode_cursor(boundary)}
        batches = [[{"OrderId": 3, "Amount": Decimal("5"), "UpdatedAt": datetime(2026, 2, 1, 9, 0)}]]
        with mock.patch.object(multi_sync, "list_columns", lambda s, sc, t: columns), \
             mock.patch.object(multi_sync, "sql_connection", lambda source, database=None: FakeConn(batches)):
            rows, max_cursor, incremental_run = multi_sync.fetch_job_rows(self.source(), job, state)
        self.assertTrue(incremental_run)
        self.assertEqual(executed["params"], (boundary,))
        self.assertIn("WHERE", executed["query"])
        self.assertEqual(max_cursor, datetime(2026, 2, 1, 9, 0))


class TargetFieldValidationTests(unittest.TestCase):
    def job(self):
        return {
            "unique_key": "OrderId",
            "columns": [
                {"source": "OrderId", "target": "订单号", "sql_type": "int"},
                {"source": "UpdatedAt", "target": "更新时间", "sql_type": "datetime2"},
            ],
        }

    def test_readonly_or_complex_target_field_is_rejected_before_writing(self):
        with self.assertRaises(sync.ConfigError):
            multi_sync.validate_target_fields(self.job(), {"订单号": 1, "更新时间": 1005})

    def test_date_unique_key_is_provisioned_as_text(self):
        job = self.job()
        job["unique_key"] = "UpdatedAt"
        self.assertEqual(multi_sync.column_field_type(job["columns"][1], "UpdatedAt"), 1)
        self.assertEqual(multi_sync.column_field_type(job["columns"][0], "UpdatedAt"), 2)

    def test_writable_fields_pass(self):
        multi_sync.validate_target_fields(self.job(), {"订单号": 2, "更新时间": 5})


class FieldCreationTests(unittest.TestCase):
    def test_missing_field_creation_passes_job_unique_key(self):
        calls = []

        class FakeClient:
            def request(self, method, path, **kwargs):
                calls.append((path, kwargs.get("json")))
                return {"code": 0}

        job = {
            "unique_key": "UpdatedAt",
            "columns": [{"source": "UpdatedAt", "target": "更新时间", "sql_type": "datetime2"}],
        }
        multi_sync.create_field(FakeClient(), "app_token", "tbl_1", job["columns"][0], job["unique_key"])
        self.assertEqual(calls[0][1]["type"], 1)


if __name__ == "__main__":
    unittest.main()
