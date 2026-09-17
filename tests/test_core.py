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
        # 显式传入设置，避免测试隐式依赖本机 sync_config.json 里的运行设置。
        settings = {"timezone_offset": 8, "null_policy": "skip", "query_timeout": 60, "dashboard_port": 5001}
        return multi_sync.job_fingerprint(
            self.source(), job, "app_token", table_id or job["target"]["table_id"], types, settings
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


class RuntimeSettingsTests(unittest.TestCase):
    """运行设置从界面保存到 sync_config.json，且必须能覆盖 .env 的旧写法。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "sync_config.json"
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        for key in ("DB_QUERY_TIMEOUT", "SYNC_NULL_POLICY", "SYNC_TIMEZONE_OFFSET", "DASHBOARD_PORT", "SYNC_SCHEDULE_ENABLED", "SYNC_SCHEDULE_INTERVAL_MINUTES"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def test_defaults_when_nothing_is_configured(self):
        with mock.patch.object(workspace, "CONFIG_PATH", self.config_path):
            self.assertEqual(workspace.runtime_settings(workspace.empty_config()), {
                "query_timeout": 60,
                "null_policy": "skip",
                "timezone_offset": 8,
                "dashboard_port": 5001,
                "schedule_enabled": False,
                "schedule_interval_minutes": 60,
            })

    def test_env_is_used_when_interface_has_no_value(self):
        os.environ["DB_QUERY_TIMEOUT"] = "30"
        os.environ["SYNC_NULL_POLICY"] = "overwrite"
        config = {**workspace.empty_config(), "settings": {}}
        settings = workspace.runtime_settings(config)
        self.assertEqual(settings["query_timeout"], 30)
        self.assertEqual(settings["null_policy"], "overwrite")

    def test_interface_value_beats_env(self):
        os.environ["DB_QUERY_TIMEOUT"] = "30"
        config = {**workspace.empty_config(), "settings": {"query_timeout": 120}}
        self.assertEqual(workspace.runtime_settings(config)["query_timeout"], 120)

    def test_port_prefers_explicit_env_over_interface(self):
        """端口是启动参数：命令行 DASHBOARD_PORT 的意图强于界面里保存的值。"""
        os.environ["DASHBOARD_PORT"] = "5099"
        config = {**workspace.empty_config(), "settings": {"dashboard_port": 5001}}
        self.assertEqual(workspace.runtime_settings(config)["dashboard_port"], 5099)

    def test_invalid_env_falls_back_to_default_instead_of_crashing(self):
        os.environ["DB_QUERY_TIMEOUT"] = "abc"
        os.environ["SYNC_TIMEZONE_OFFSET"] = "99"
        settings = workspace.runtime_settings({**workspace.empty_config(), "settings": {}})
        self.assertEqual(settings["query_timeout"], 60)
        self.assertEqual(settings["timezone_offset"], 8)

    def test_legacy_clear_alias_still_means_overwrite(self):
        """旧文档让用户写 SYNC_NULL_POLICY=clear，不能静默降级成 skip。"""
        self.assertEqual(workspace.validate_settings({"null_policy": "clear"})["null_policy"], "overwrite")
        os.environ["SYNC_NULL_POLICY"] = "clear"
        settings = workspace.runtime_settings({**workspace.empty_config(), "settings": {}})
        self.assertEqual(settings["null_policy"], "overwrite")

    def test_validation_rejects_out_of_range_and_unknown_values(self):
        cases = {
            "查询超时越界": {"query_timeout": 0},
            "查询超时非数字": {"query_timeout": "很快"},
            "NULL 策略非法": {"null_policy": "maybe"},
            "时区越界": {"timezone_offset": 20},
            "端口越界": {"dashboard_port": 80},
            "同步间隔过短": {"schedule_interval_minutes": 0},
            "同步间隔过长": {"schedule_interval_minutes": 10081},
            "定时开关非法": {"schedule_enabled": "sometimes"},
            "未知设置项": {"unknown_key": 1},
        }
        for label, payload in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                workspace.validate_settings(payload)

    def test_config_round_trip_persists_settings(self):
        with mock.patch.object(workspace, "CONFIG_PATH", self.config_path):
            workspace.save_config({**workspace.empty_config(), "settings": {
                "query_timeout": 45, "null_policy": "overwrite", "timezone_offset": 8, "dashboard_port": 5099,
                "schedule_enabled": True, "schedule_interval_minutes": 15,
            }})
            self.assertEqual(workspace.runtime_settings()["dashboard_port"], 5099)
            self.assertEqual(workspace.runtime_settings()["null_policy"], "overwrite")
            self.assertTrue(workspace.runtime_settings()["schedule_enabled"])
            self.assertEqual(workspace.runtime_settings()["schedule_interval_minutes"], 15)

    def test_version_two_config_without_settings_still_loads(self):
        """老配置文件没有 settings 段，读取时必须补默认值而不是报错。"""
        legacy = {"version": 2, "sources": [], "jobs": []}
        validated = workspace.validate_config(legacy)
        self.assertEqual(validated["settings"], {})
        self.assertEqual(validated["version"], workspace.CONFIG_VERSION)


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "sync_config.json"
        self.client = dashboard.app.test_client()
        self.headers = {"Host": "127.0.0.1:5001", "X-CSRF-Token": dashboard.CSRF_TOKEN}
        self.patcher = mock.patch.object(workspace, "CONFIG_PATH", self.config_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def test_workspace_api_exposes_settings_and_origins(self):
        data = self.client.get("/api/workspace", headers={"Host": "127.0.0.1:5001"}).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["settings"]["effective"]["query_timeout"], 60)
        self.assertEqual(data["settings"]["origins"]["query_timeout"], "default")
        self.assertFalse(data["schedule"]["enabled"])

    def test_saving_settings_persists_and_shows_interface_origin(self):
        response = self.client.post("/api/settings", headers=self.headers, json={
            "query_timeout": 15, "null_policy": "overwrite", "timezone_offset": 8, "dashboard_port": 5001,
            "schedule_enabled": True, "schedule_interval_minutes": 30,
        })
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["settings"]["effective"]["query_timeout"], 15)
        self.assertEqual(payload["settings"]["origins"]["null_policy"], "settings")
        self.assertTrue(payload["schedule"]["enabled"])
        self.assertEqual(payload["schedule"]["interval_minutes"], 30)

        reloaded = self.client.get("/api/workspace", headers={"Host": "127.0.0.1:5001"}).get_json()
        self.assertEqual(reloaded["settings"]["effective"]["null_policy"], "overwrite")

    def test_invalid_settings_are_rejected_without_writing(self):
        response = self.client.post("/api/settings", headers=self.headers, json={"dashboard_port": 80})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertFalse(self.config_path.exists())


class ScheduleManagerTests(unittest.TestCase):
    def test_scheduler_starts_all_enabled_jobs_after_interval(self):
        starts = []

        class FakeJobs:
            def start(self, dry_run, sync_job_id=""):
                starts.append((dry_run, sync_job_id))
                return {"ok": True}

        class StopLoop(Exception):
            pass

        class FakeEvent:
            calls = 0

            def wait(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    return False
                raise StopLoop

            def clear(self):
                pass

            def set(self):
                pass

        manager = dashboard.ScheduleManager(FakeJobs())
        manager.wakeup = FakeEvent()
        settings = {
            "schedule_enabled": True, "schedule_interval_minutes": 1,
            "query_timeout": 60, "null_policy": "skip", "timezone_offset": 8, "dashboard_port": 5001,
        }
        config = {"jobs": [{"id": "job_1", "enabled": True}]}
        with mock.patch.object(dashboard, "runtime_settings", return_value=settings), \
             mock.patch.object(dashboard, "load_config", return_value=config), \
             mock.patch.object(dashboard, "resolve_app_token", return_value="app_token"), \
             self.assertRaises(StopLoop):
            manager._loop()
        self.assertEqual(starts, [(False, "")])
        self.assertIsNotNone(manager.last_attempt_at)

    def test_scheduler_skips_when_job_manager_is_busy(self):
        class BusyJobs:
            def start(self, dry_run, sync_job_id=""):
                return {"ok": False, "message": "已有同步任务正在运行"}

        manager = dashboard.ScheduleManager(BusyJobs())
        manager._set_status(next_run_at=None, message="已有同步任务正在运行", attempted=True)
        self.assertEqual(manager.snapshot()["message"], "已有同步任务正在运行")


class SettingsFingerprintTests(unittest.TestCase):
    """时区与 NULL 策略决定 SQL 值怎么变成飞书值，改动后必须作废旧游标。"""

    def source(self):
        return {"id": "source_a", "server": "127.0.0.1", "port": 1433, "database": "Orders", "user": "reader"}

    def job(self):
        return {
            "id": "job_1", "name": "订单", "source_id": "source_a", "schema": "dbo", "table": "Orders",
            "enabled": True,
            "columns": [{"source": "OrderId", "target": "订单号", "sql_type": "int", "nullable": False}],
            "unique_key": "OrderId",
            "incremental": {"enabled": True, "column": "UpdatedAt"},
            "target": {"mode": "auto", "table_id": "tbl_1", "table_name": "订单", "auto_create_fields": True},
        }

    def fingerprint(self, settings):
        return multi_sync.job_fingerprint(self.source(), self.job(), "app_token", "tbl_1", {"订单号": 2}, settings)

    def test_timezone_and_null_policy_change_the_fingerprint(self):
        base = {"query_timeout": 60, "null_policy": "skip", "timezone_offset": 8, "dashboard_port": 5001}
        baseline = self.fingerprint(base)
        self.assertNotEqual(self.fingerprint({**base, "timezone_offset": 0}), baseline)
        self.assertNotEqual(self.fingerprint({**base, "null_policy": "overwrite"}), baseline)

    def test_query_timeout_does_not_invalidate_cursors(self):
        """超时只影响失败重试，不改变值语义，不应触发全量重扫。"""
        base = {"query_timeout": 60, "null_policy": "skip", "timezone_offset": 8, "dashboard_port": 5001}
        self.assertEqual(self.fingerprint({**base, "query_timeout": 600}), self.fingerprint(base))

    def test_timezone_offset_is_applied_to_naive_datetimes(self):
        # 墙上时间 2026-01-01 08:00 按 UTC+8 解释 == 2026-01-01T00:00Z
        naive = datetime(2026, 1, 1, 8, 0)
        utc8 = sync.convert_value(naive, 5, 8)
        utc0 = sync.convert_value(naive, 5, 0)
        self.assertEqual(utc8, 1767225600000)
        self.assertEqual(utc0 - utc8, 8 * 60 * 60 * 1000)


if __name__ == "__main__":
    unittest.main()
