#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-source, selectable-table, incremental SQL Server to Feishu sync engine."""

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymssql
from dotenv import load_dotenv

from sync import (
    BATCH_SIZE,
    ConfigError,
    FeishuClient,
    _records_equal,
    configure_logging,
    convert_value,
    progress,
    quote_identifier,
    sync_lock,
)
from workspace import (
    ENV_PATH,
    get_source,
    load_config,
    load_state,
    save_config,
    save_state,
    source_password,
)
from feishu_link import parse_feishu_base_url

load_dotenv(ENV_PATH, override=True)

NUMERIC_TYPES = {
    "bigint", "decimal", "float", "int", "money", "numeric", "real", "smallint", "smallmoney", "tinyint"
}
DATE_TYPES = {"date", "datetime", "datetime2", "datetimeoffset", "smalldatetime"}
TEXT_TYPES = {
    "char", "nchar", "ntext", "nvarchar", "text", "varchar", "uniqueidentifier", "xml",
    "binary", "image", "varbinary", "timestamp", "rowversion",
}
WRITABLE_FIELD_TYPES = {1, 2, 3, 4, 5, 7, 13, 15}
UNIQUE_FIELD_TYPES = {1, 2, 3, 13}


def column_field_type(column: Dict[str, Any], unique_key: str) -> int:
    kind = sql_type_to_feishu(column["sql_type"])
    # Dates/checkboxes are not safe business keys in the target API.
    return 1 if column["source"] == unique_key and kind not in UNIQUE_FIELD_TYPES else kind


def validate_target_fields(job: Dict[str, Any], field_types: Dict[str, int]) -> None:
    for column in job["columns"]:
        kind = field_types.get(column["target"])
        if kind not in WRITABLE_FIELD_TYPES:
            raise ConfigError(f"暂不支持写入复杂或只读飞书字段: {column['target']}（类型 {kind}）")
        if column["source"] == job["unique_key"] and kind not in UNIQUE_FIELD_TYPES:
            raise ConfigError("唯一键对应的飞书字段必须是文本、数字、单选或电话字段")


def job_fingerprint(source: Dict[str, Any], job: Dict[str, Any], app_token: str,
                    table_id: Optional[str], field_types: Dict[str, int]) -> str:
    """Bind cursors to their actual source, target and conversion semantics."""
    identity = {
        "version": 1,
        "source": {key: source.get(key) for key in ("id", "server", "port", "database", "user")},
        "job": {key: job.get(key) for key in ("schema", "table", "columns", "unique_key", "incremental")},
        "app_token": app_token, "table_id": table_id, "field_types": field_types,
        "timezone": os.environ.get("SYNC_TIMEZONE_OFFSET", "8"),
        "null_policy": os.environ.get("SYNC_NULL_POLICY", "skip"),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def resolve_app_token() -> str:
    base_url = (os.environ.get("FEISHU_BASE_URL") or "").strip()
    parsed = parse_feishu_base_url(base_url) if base_url else {}
    token = parsed.get("app_token") or (os.environ.get("FEISHU_BASE_APP_TOKEN") or "").strip()
    if not token:
        raise ConfigError("请先配置飞书多维表格链接或 App Token")
    return token


def feishu_client() -> FeishuClient:
    app_id = (os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = (os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise ConfigError("请先配置飞书 App ID 和 App Secret")
    return FeishuClient(app_id, app_secret)


def sql_connection(source: Dict[str, Any], database: Optional[str] = None):
    kwargs: Dict[str, Any] = {
        "server": source["server"],
        "port": int(source.get("port", 1433)),
        "database": database or source["database"],
        "charset": "utf8",
        "login_timeout": 10,
        "timeout": int(os.environ.get("DB_QUERY_TIMEOUT", "60")),
    }
    if source.get("user"):
        kwargs.update(user=source["user"], password=source_password(source))
    return pymssql.connect(**kwargs)


def list_databases(source: Dict[str, Any]) -> List[str]:
    with sql_connection(source, database="master") as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sys.databases "
                "WHERE state_desc = 'ONLINE' AND HAS_DBACCESS(name) = 1 "
                "AND database_id > 4 ORDER BY name"
            )
            return [row[0] for row in cursor.fetchall()]


def list_tables(source: Dict[str, Any]) -> List[Dict[str, str]]:
    with sql_connection(source) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') ORDER BY TABLE_SCHEMA, TABLE_NAME"
            )
            return [
                {"schema": row[0], "name": row[1], "type": "view" if row[2] == "VIEW" else "table"}
                for row in cursor.fetchall()
            ]


def list_columns(source: Dict[str, Any], schema: str, table: str) -> List[Dict[str, Any]]:
    with sql_connection(source) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION "
                "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (schema, table),
            )
            return [
                {
                    "name": row[0],
                    "sql_type": str(row[1]).lower(),
                    "nullable": row[2] == "YES",
                    "ordinal": int(row[3]),
                    "feishu_type": sql_type_to_feishu(str(row[1]).lower()),
                }
                for row in cursor.fetchall()
            ]


def sql_type_to_feishu(sql_type: str) -> int:
    value = sql_type.lower()
    if value in NUMERIC_TYPES:
        return 2
    if value in DATE_TYPES:
        return 5
    if value == "bit":
        return 7
    return 1


def _qualified_table(job: Dict[str, Any]) -> str:
    return f"{quote_identifier(job['schema'])}.{quote_identifier(job['table'])}"


def _decode_cursor(cursor: Dict[str, Any]) -> Any:
    value = cursor.get("value")
    value_type = cursor.get("type")
    if value_type == "datetime":
        return datetime.fromisoformat(value)
    if value_type == "date":
        return date.fromisoformat(value)
    if value_type == "decimal":
        return Decimal(value)
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    return value


def _encode_cursor(value: Any) -> Dict[str, Any]:
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, bool):
        return {"type": "int", "value": int(value)}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "str", "value": str(value)}


def fetch_job_rows(
    source: Dict[str, Any], job: Dict[str, Any], job_state: Optional[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Optional[Any], bool]:
    metadata = {item["name"]: item for item in list_columns(source, job["schema"], job["table"])}
    if not metadata:
        raise ConfigError(f"未找到 SQL 表或视图: {job['schema']}.{job['table']}")
    selected = [item["source"] for item in job["columns"]]
    missing = [name for name in selected if name not in metadata]
    if missing:
        raise ConfigError(f"任务 {job['name']} 的 SQL 字段不存在: {', '.join(missing)}")
    unique_key = job["unique_key"]
    incremental = job["incremental"]
    incremental_column = incremental.get("column") if incremental.get("enabled") else ""
    query = f"SELECT {', '.join(quote_identifier(name) for name in selected)} FROM {_qualified_table(job)}"
    params: Tuple[Any, ...] = ()
    incremental_run = bool(incremental_column and job_state and job_state.get("cursor"))
    if incremental_run:
        cursor_value = _decode_cursor(job_state["cursor"])
        declared_type = str(metadata.get(incremental_column, {}).get("sql_type") or "")
        if _cursor_fits_sql_type(cursor_value, declared_type):
            query += f" WHERE {quote_identifier(incremental_column)} >= %s"
            params = (cursor_value,)
        else:
            incremental_run = False
            progress(
                "job_plan", 65,
                f"{job['name']}：已保存的增量游标与字段 {incremental_column}（{declared_type or '未知类型'}）不匹配，本次改为全量扫描",
            )
    order_columns = [name for name in (incremental_column, unique_key) if name]
    query += " ORDER BY " + ", ".join(quote_identifier(name) for name in dict.fromkeys(order_columns))

    rows: List[Dict[str, Any]] = []
    with sql_connection(source) as conn:
        with conn.cursor(as_dict=True) as cursor:
            cursor.execute(query, params)
            while True:
                chunk = cursor.fetchmany(1000)
                if not chunk:
                    break
                rows.extend(chunk)
    max_cursor = None
    if incremental_column and rows:
        cursor_values = [row.get(incremental_column) for row in rows if row.get(incremental_column) is not None]
        if cursor_values:
            max_cursor = max(cursor_values)
    return rows, max_cursor, incremental_run


def _cursor_fits_sql_type(value: Any, sql_type: str) -> bool:
    """Detect a cursor that no longer matches its incremental column type."""
    kind = (sql_type or "").lower()
    if kind in DATE_TYPES:
        return isinstance(value, (datetime, date))
    if kind == "bit":
        return isinstance(value, bool)
    if kind in NUMERIC_TYPES:
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    return isinstance(value, str)


def normalize_business_key(value: Any, field_type: int) -> str:
    if field_type not in UNIQUE_FIELD_TYPES:
        raise ConfigError("唯一键对应的飞书字段必须是文本、数字、单选或电话字段")
    # Feishu can return text as rich-text fragments rather than a string.
    if isinstance(value, list) and all(isinstance(part, dict) and isinstance(part.get("text"), str) for part in value):
        value = "".join(part["text"] for part in value)
    if value is None or not str(value).strip():
        raise ConfigError("唯一键存在空值")
    if isinstance(value, (dict, list)):
        raise ConfigError("唯一键字段值格式不正确")
    if field_type == 2:
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError("not finite")
            if number == number.to_integral_value() and abs(number) > 2 ** 53 - 1:
                raise ConfigError("数字唯一键超出安全整数范围，请改用文本字段")
            text = format(number, "f")
            return "0" if number == 0 else (text.rstrip("0").rstrip(".") if "." in text else text)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError("数字唯一键格式不正确") from exc
    return str(value).strip()


def validate_row_keys(
    rows: Sequence[Dict[str, Any]], unique_key: str, job_name: str, field_type: int
) -> List[str]:
    keys: List[str] = []
    seen = set()
    duplicates = set()
    for row in rows:
        try:
            raw = row.get(unique_key)
            key = normalize_business_key(convert_value(raw, field_type), field_type)
        except ConfigError as exc:
            raise ConfigError(f"任务 {job_name} 的唯一键 {unique_key}: {exc}") from exc
        if key in seen:
            duplicates.add(key)
        seen.add(key)
        keys.append(key)
    if duplicates:
        raise ConfigError(f"任务 {job_name} 的 SQL 增量结果存在重复唯一键: {', '.join(sorted(duplicates)[:10])}")
    return keys


def target_record_map(
    records: Sequence[Dict[str, Any]], unique_key_label: str, field_type: int
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    duplicates = set()
    for item in records:
        raw = (item.get("fields") or {}).get(unique_key_label)
        if raw is None or raw == "":
            continue
        key = normalize_business_key(raw, field_type)
        if key in result:
            duplicates.add(key)
        result[key] = item
    if duplicates:
        raise ConfigError("飞书目标表存在重复唯一键: " + ", ".join(sorted(duplicates)[:10]))
    return result


def _table_id_from_response(data: Dict[str, Any]) -> str:
    payload = data.get("data") or {}
    table = payload.get("table") or {}
    return str(table.get("table_id") or payload.get("table_id") or "")


def create_table(
    client: FeishuClient, app_token: str, job: Dict[str, Any]
) -> str:
    unique_key = job["unique_key"]
    ordered = sorted(job["columns"], key=lambda item: item["source"] != unique_key)
    fields = []
    for column in ordered:
        field_type = column_field_type(column, unique_key)
        field: Dict[str, Any] = {"field_name": column["target"], "type": field_type}
        if field_type == 5:
            field["property"] = {"date_formatter": "yyyy-MM-dd HH:mm"}
        fields.append(field)
    data = client.request(
        "POST",
        f"/bitable/v1/apps/{app_token}/tables",
        params={"client_token": str(uuid.uuid4())},
        json={
            "table": {
                "name": job["target"]["table_name"],
                "default_view_name": "全部数据",
                "fields": fields,
            }
        },
    )
    table_id = _table_id_from_response(data)
    if not table_id:
        raise RuntimeError("飞书已返回成功，但响应中没有 table_id")
    return table_id


def create_field(
    client: FeishuClient, app_token: str, table_id: str, column: Dict[str, Any], unique_key: str
) -> None:
    field_type = column_field_type(column, unique_key)
    payload: Dict[str, Any] = {"field_name": column["target"], "type": field_type}
    if field_type == 5:
        payload["property"] = {"date_formatter": "yyyy-MM-dd HH:mm"}
    client.request(
        "POST",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        params={"client_token": str(uuid.uuid4())},
        json=payload,
    )


def ensure_target(
    client: FeishuClient,
    app_token: str,
    config: Dict[str, Any],
    job: Dict[str, Any],
    dry_run: bool,
) -> Tuple[Optional[str], Dict[str, int], str]:
    table_id = job["target"].get("table_id") or ""
    action = "existing"
    tables = client.list_tables(app_token)
    if table_id and not any(item.get("table_id") == table_id for item in tables):
        if job["target"]["mode"] == "existing":
            raise ConfigError(f"任务 {job['name']} 指定的飞书 Table ID 不存在")
        table_id = ""
    if not table_id:
        matched = [item for item in tables if item.get("name") == job["target"]["table_name"]]
        if len(matched) > 1:
            raise ConfigError(f"飞书中存在多个同名表: {job['target']['table_name']}")
        if matched:
            table_id = matched[0]["table_id"]
            action = "bind_existing"
            if not dry_run and job["target"].get("table_id") != table_id:
                job["target"]["table_id"] = table_id
                save_config(config)
        elif dry_run:
            field_types = {
                column["target"]: column_field_type(column, job["unique_key"]) for column in job["columns"]
            }
            return None, field_types, "create"
        else:
            table_id = create_table(client, app_token, job)
            job["target"]["table_id"] = table_id
            save_config(config)
            action = "created"

    fields = client.list_fields(app_token, table_id)
    field_types = {str(item.get("field_name")): int(item.get("type", 1)) for item in fields}
    missing = [column for column in job["columns"] if column["target"] not in field_types]
    if missing and not job["target"].get("auto_create_fields"):
        raise ConfigError(f"任务 {job['name']} 的飞书目标表缺少字段: {', '.join(item['target'] for item in missing)}")
    if missing and dry_run:
        action = "add_fields"
        for column in missing:
            field_types[column["target"]] = column_field_type(column, job["unique_key"])
    elif missing:
        for column in missing:
            create_field(client, app_token, table_id, column, job["unique_key"])
        fields = client.list_fields(app_token, table_id)
        field_types = {str(item.get("field_name")): int(item.get("type", 1)) for item in fields}
        action = "fields_created" if action == "existing" else action
    return table_id, field_types, action


def build_record(row: Dict[str, Any], columns: Sequence[Dict[str, Any]], field_types: Dict[str, int]) -> Dict[str, Any]:
    fields = {}
    null_policy = (os.environ.get("SYNC_NULL_POLICY") or "skip").lower()
    for column in columns:
        value = row.get(column["source"])
        if value is None and null_policy == "skip":
            continue
        fields[column["target"]] = convert_value(value, field_types[column["target"]])
    return {"fields": fields}


def run_job(
    client: FeishuClient,
    app_token: str,
    config: Dict[str, Any],
    state: Dict[str, Any],
    job: Dict[str, Any],
    dry_run: bool,
) -> Dict[str, Any]:
    source = get_source(config, job["source_id"])
    table_id, field_types, target_action = ensure_target(client, app_token, config, job, dry_run)
    validate_target_fields(job, field_types)
    fingerprint = job_fingerprint(source, job, app_token, table_id, field_types)
    job_state = state["jobs"].get(job["id"]) or {}
    # A saved cursor is only reusable while the source, fields, key and target table stay identical.
    cursor_reset = bool(job_state.get("fingerprint")) and job_state["fingerprint"] != fingerprint
    if cursor_reset:
        progress(
            "job_plan", 68,
            f"{job['name']}：数据源、字段、唯一键、增量字段或目标飞书表已变更，本次自动执行全量重新扫描",
        )
        job_state = {}
    rows, max_cursor, incremental_run = fetch_job_rows(source, job, job_state or None)
    unique_column = next(item for item in job["columns"] if item["source"] == job["unique_key"])
    unique_field_type = field_types.get(
        unique_column["target"], column_field_type(unique_column, job["unique_key"])
    )
    keys = validate_row_keys(rows, job["unique_key"], job["name"], unique_field_type)

    existing: Dict[str, Dict[str, Any]] = {}
    if table_id:
        records = client.list_records(app_token, table_id, [item["target"] for item in job["columns"]])
        unique_target = unique_column["target"]
        existing = target_record_map(records, unique_target, field_types[unique_target])

    creates: List[Dict[str, Any]] = []
    updates: List[Dict[str, Any]] = []
    skipped = 0
    for row, key in zip(rows, keys):
        record = build_record(row, job["columns"], field_types)
        current = existing.get(key)
        if current:
            if _records_equal(record["fields"], current.get("fields") or {}):
                skipped += 1
            else:
                record["record_id"] = current["record_id"]
                updates.append(record)
        else:
            creates.append(record)

    summary = {
        "job_id": job["id"],
        "job_name": job["name"],
        "source_rows": len(rows),
        "create": len(creates),
        "update": len(updates),
        "skip": skipped,
        "incremental": incremental_run,
        "cursor_reset": cursor_reset,
        "target_action": target_action,
        "table_id": table_id or "",
    }
    progress(
        "job_plan", 70,
        f"{job['name']}：读取 {len(rows)}，新增 {len(creates)}，更新 {len(updates)}，跳过 {skipped}"
        + ("（已重置增量游标）" if cursor_reset else ""),
    )
    if dry_run:
        return summary
    if not table_id:
        raise RuntimeError("正式同步时飞书目标表未创建")
    for start in range(0, len(creates), BATCH_SIZE):
        client.batch_create(app_token, table_id, creates[start:start + BATCH_SIZE])
    for start in range(0, len(updates), BATCH_SIZE):
        client.batch_update(app_token, table_id, updates[start:start + BATCH_SIZE])
    entry: Dict[str, Any] = {
        "last_success_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "table_id": table_id,
        "fingerprint": fingerprint,
    }
    if max_cursor is not None:
        entry["cursor"] = _encode_cursor(max_cursor)
    state["jobs"][job["id"]] = entry
    save_state(state)
    return summary


def run(dry_run: bool = False, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    configure_logging()
    config = load_config()
    state = load_state()
    jobs = [job for job in config["jobs"] if job.get("enabled")]
    if job_id:
        jobs = [job for job in jobs if job["id"] == job_id]
    if not jobs:
        raise ConfigError("没有启用的同步任务，请先在管理后台勾选表和字段")
    client = feishu_client()
    app_token = resolve_app_token()
    results = []
    with sync_lock():
        total = len(jobs)
        for index, job in enumerate(jobs, start=1):
            progress("job_start", max(1, int((index - 1) * 90 / total)), f"开始处理 {job['name']}（{index}/{total}）")
            results.append(run_job(client, app_token, config, state, job, dry_run))
    totals = {
        "create": sum(item["create"] for item in results),
        "update": sum(item["update"] for item in results),
        "skip": sum(item["skip"] for item in results),
        "cursor_reset": sum(1 for item in results if item.get("cursor_reset")),
    }
    progress("done", 100, f"{'预检' if dry_run else '同步'}完成：新增 {totals['create']}，更新 {totals['update']}，跳过 {totals['skip']}")
    print(json.dumps({"type": "result", "ok": True, "dry_run": dry_run, "jobs": results, **totals}, ensure_ascii=False), flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="多数据库增量同步到飞书多维表格")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--job-id")
    args = parser.parse_args()
    try:
        run(dry_run=args.dry_run, job_id=args.job_id)
        return 0
    except Exception as exc:
        progress("error", 0, f"执行失败: {exc}")
        print(json.dumps({"type": "result", "ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
