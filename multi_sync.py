#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-database, selectable-table, incremental synchronization to Feishu."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import copy
import sys
import signal
import sqlite3
import tempfile
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Any, Dict, List, Optional, Sequence, Tuple

import pymssql
import databases
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
    config_edit_lock,
    default_split_column,
    ENV_PATH,
    get_source,
    load_config,
    load_state,
    runtime_settings,
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
    kind = column.get("target_type") or sql_type_to_feishu(column["sql_type"])
    # Dates/checkboxes are not safe business keys in the target API.
    return 1 if column["source"] == unique_key and kind not in UNIQUE_FIELD_TYPES else kind


def validate_target_fields(job: Dict[str, Any], field_types: Dict[str, int]) -> None:
    for column in job["columns"]:
        kind = field_types.get(column["target"])
        if kind not in WRITABLE_FIELD_TYPES:
            raise ConfigError(f"暂不支持写入复杂或只读飞书字段: {column['target']}（类型 {kind}）")
        if column.get("target_type") and kind != column["target_type"]:
            raise ConfigError(f"字段 {column['target']} 的飞书类型与任务设置不一致，请修改飞书字段类型，或填写新的飞书字段名后保存")
        if column["source"] == job["unique_key"] and kind not in UNIQUE_FIELD_TYPES:
            raise ConfigError("唯一键对应的飞书字段必须是文本、数字、单选或电话字段")


def job_fingerprint(source: Dict[str, Any], job: Dict[str, Any], app_token: str,
                    table_id: Optional[str], field_types: Dict[str, int],
                    settings: Optional[Dict[str, Any]] = None) -> str:
    """Bind cursors to their actual source, target and conversion semantics.

    时区与 NULL 策略参与指纹：这两项决定同一个 SQL 值怎么变成飞书值，
    改动后旧游标必须失效并全量重扫，否则边界数据会按新语义被静默跳过。
    """
    settings = settings or runtime_settings()
    identity = {
        "version": 3,
        "source": {key: source.get(key) for key in ("id", "server", "port", "database", "user")},
        "job": {key: job.get(key) for key in ("schema", "table", "columns", "unique_key", "incremental", "split")},
        "app_token": app_token, "table_id": table_id, "field_types": field_types,
        "timezone": settings["timezone_offset"],
        "null_policy": settings["null_policy"],
    }
    if databases.database_type(source) != "mssql":
        identity["source"]["type"] = databases.database_type(source)
    if databases.database_type(source) in {"mysql", "mariadb"}:
        identity["mysql_timestamp_version"] = 2
    if any(column.get("sql_type") in NUMERIC_TYPES for column in job.get("columns", [])):
        identity["number_conversion_version"] = 2
    # Earlier builds ignored the configured timezone during record conversion.
    # Invalidate those cursors when the selected timezone differs from that fallback.
    try:
        legacy_timezone = float(os.environ.get("SYNC_TIMEZONE_OFFSET") or 8)
    except ValueError:
        legacy_timezone = 8
    if settings["timezone_offset"] != legacy_timezone:
        identity["date_conversion_version"] = 2
    if "generated_key" in job:
        identity["job"]["generated_key"] = job["generated_key"]
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


def sql_connection(source: Dict[str, Any], database: Optional[str] = None,
                   settings: Optional[Dict[str, Any]] = None):
    if databases.database_type(source) != "mssql":
        return databases.connect(source, database, settings)
    settings = settings or runtime_settings()
    kwargs: Dict[str, Any] = {
        "server": source["server"],
        "port": int(source.get("port", 1433)),
        "database": database or source["database"],
        "charset": "utf8",
        "login_timeout": 10,
        "timeout": int(settings["query_timeout"]),
        **databases.tls_options(source),
    }
    if source.get("user"):
        kwargs.update(user=source["user"], password=source_password(source))
    return pymssql.connect(**kwargs)


def list_databases(source: Dict[str, Any]) -> List[str]:
    if databases.database_type(source) != "mssql":
        return databases.list_databases(source)
    with sql_connection(source, database="master") as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sys.databases "
                "WHERE state_desc = 'ONLINE' AND HAS_DBACCESS(name) = 1 "
                "AND database_id > 4 ORDER BY name"
            )
            return [row[0] for row in cursor.fetchall()]


def list_tables(source: Dict[str, Any]) -> List[Dict[str, str]]:
    if databases.database_type(source) != "mssql":
        return databases.list_tables(source)
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
    if databases.database_type(source) != "mssql":
        return [{**column, "feishu_type": 1 if databases.needs_exact_text(column, source) else sql_type_to_feishu(column["sql_type"])}
                for column in databases.list_columns(source, schema, table)]
    with sql_connection(source) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION, "
                "COLUMNPROPERTY(OBJECT_ID(QUOTENAME(TABLE_SCHEMA)+N'.'+QUOTENAME(TABLE_NAME)), COLUMN_NAME, 'IsIdentity'), "
                "IDENT_INCR(QUOTENAME(TABLE_SCHEMA)+N'.'+QUOTENAME(TABLE_NAME)), NUMERIC_PRECISION "
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
                    "is_identity": bool(row[4]),
                    "identity_increment": float(row[5] or 0),
                    "numeric_precision": row[6],
                    "feishu_type": 1 if databases.needs_exact_text({"sql_type": str(row[1]).lower(), "numeric_precision": row[6]}, source)
                                   else sql_type_to_feishu(str(row[1]).lower()),
                }
                for row in cursor.fetchall()
            ]


def list_primary_key(source: Dict[str, Any], schema: str, table: str) -> List[str]:
    if databases.database_type(source) != "mssql":
        return databases.list_primary_key(source, schema, table)
    with sql_connection(source) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT k.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS t "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k ON "
                "t.CONSTRAINT_CATALOG = k.CONSTRAINT_CATALOG AND "
                "t.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND t.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
                "WHERE t.CONSTRAINT_TYPE = 'PRIMARY KEY' AND t.TABLE_SCHEMA = %s AND t.TABLE_NAME = %s "
                "ORDER BY k.ORDINAL_POSITION", (schema, table),
            )
            return [row[0] for row in cursor.fetchall()]


def default_incremental_column(columns: List[Dict[str, Any]], unique_key: str) -> str:
    names = {str(col.get("source", col.get("name", ""))).lower().replace("_", "").replace(" ", ""):
             col for col in columns if col.get("sql_type") in DATE_TYPES}
    for preferred in ("更新时间", "修改时间", "最后修改时间", "更新日期", "最后更新时间",
                      "updatedat", "updatedtime", "updatetime", "updatedate", "lastupdated",
                      "modifiedat", "modifiedtime", "modifytime", "modifydate", "lastmodified", "lastupdatetime", "lastupdatedat"):
        if preferred in names:
            return names[preferred].get("source", names[preferred].get("name", ""))
    for column in columns:
        name = column.get("source", column.get("name", ""))
        if name == unique_key and column.get("sql_type") in {"int", "bigint", "smallint", "tinyint"} and column.get("is_identity") and column.get("identity_increment", 0) > 0:
            return name
    return ""


def automatic_job(source: Dict[str, Any], schema: str, table: str) -> Dict[str, Any]:
    columns = list_columns(source, schema, table)
    if not columns:
        raise ConfigError(f"未找到字段: {schema}.{table}")
    primary = list_primary_key(source, schema, table)
    mapped = [{"source": col["name"], "target": col["name"],
               "sql_type": col["sql_type"], "nullable": col["nullable"]} for col in columns]
    for column, metadata in zip(mapped, columns):
        if databases.needs_exact_text(metadata, source):
            column["target_type"] = 1
    generated = len(primary) != 1
    key = primary[0] if not generated else "__sqlfeishu_row_key"
    names = {col["source"] for col in mapped}
    while generated and key in names:
        key += "_"
    if generated:
        mapped.append({"source": key, "target": key, "sql_type": "nvarchar", "nullable": False})
    identity = json.dumps([source["id"], schema, table], ensure_ascii=False)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    target_name = f"{source['name']}_{schema}_{table}"
    # Keep distinct tables distinct even when their names exceed the target limit.
    if len(target_name) > 100:
        target_name = target_name[:74] + "_" + digest
    split_column = default_split_column(mapped)
    incremental_column = default_incremental_column(columns, key) if (not generated or primary) else ""
    job = {
        "id": f"job_{digest}", "name": f"{source['name']} / {schema}.{table}",
        "source_id": source["id"], "schema": schema, "table": table, "enabled": True,
        "columns": mapped, "unique_key": key,
        "split": {"enabled": True, "row_limit": 40000, "column": split_column},
        "incremental": {"enabled": bool(incremental_column), "column": incremental_column},
        "target": {"mode": "auto", "table_id": "", "table_name": target_name, "auto_create_fields": True},
    }
    if generated:
        job["generated_key"] = {"columns": primary}
    return job


def add_generated_row_keys(rows: List[Dict[str, Any]], job: Dict[str, Any],
                           occurrences: Optional[Dict[str, int]] = None) -> None:
    identity_columns = job["generated_key"]["columns"] or [
        col["source"] for col in job["columns"] if col["source"] != job["unique_key"]
    ]
    if occurrences is None:
        occurrences = {}
    for row in rows:
        identity = json.dumps([(col, row[col]) for col in identity_columns],
                              ensure_ascii=False, default=str, separators=(",", ":"))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        occurrences[digest] = occurrences.get(digest, 0) + 1
        row[job["unique_key"]] = f"{digest}_{occurrences[digest]}"


SQL_TEXT_TYPES = {"char", "varchar", "text", "nchar", "nvarchar", "ntext"}


def sql_column_expression(column: Dict[str, Any], source: Optional[Dict[str, Any]] = None) -> str:
    if source and databases.database_type(source) != "mssql":
        return databases.quote_identifier(source, column["name"])
    name = quote_identifier(column["name"])
    if column.get("sql_type", "").lower() in SQL_TEXT_TYPES:
        return f"CAST({name} AS NVARCHAR(MAX)) AS {name}"
    return name


def decode_sql_value(value: Any, column: Dict[str, Any], source: Optional[Dict[str, Any]] = None) -> Any:
    if isinstance(value, bytes) and column.get("sql_type", "").lower() in SQL_TEXT_TYPES:
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"字段 {column['name']} 返回了无法按 UTF-8 解码的文本，请运行数据源中文连接测试") from exc
    if source and databases.database_type(source) != "mssql":
        return databases.decode_value(value, column, source)
    return value


def sql_type_to_feishu(sql_type: str) -> int:
    value = sql_type.lower()
    if value in NUMERIC_TYPES:
        return 2
    if value in DATE_TYPES:
        return 5
    if value == "bit":
        return 7
    return 1


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
    source: Dict[str, Any], job: Dict[str, Any], job_state: Optional[Dict[str, Any]],
    row_sink: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Any], bool]:
    metadata = {item["name"]: item for item in list_columns(source, job["schema"], job["table"])}
    if not metadata:
        raise ConfigError(f"未找到 SQL 表或视图: {job['schema']}.{job['table']}")
    selected = [item["source"] for item in job["columns"]
                if "generated_key" not in job or item["source"] != job["unique_key"]]
    missing = [name for name in selected if name not in metadata]
    if missing:
        raise ConfigError(f"任务 {job['name']} 的 SQL 字段不存在: {', '.join(missing)}")
    unique_key = job["unique_key"]
    incremental = job["incremental"]
    incremental_column = incremental.get("column") if incremental.get("enabled") else ""
    query = databases.select_query(source, job["schema"], job["table"],
                                   [sql_column_expression(metadata[name], source) for name in selected])
    params: Tuple[Any, ...] = ()
    incremental_run = bool(incremental_column and job_state and job_state.get("cursor"))
    if incremental_run:
        cursor_value = _decode_cursor(job_state["cursor"])
        declared_type = str(metadata.get(incremental_column, {}).get("sql_type") or "")
        if _cursor_fits_sql_type(cursor_value, declared_type, source):
            query += f" WHERE {databases.quote_identifier(source, incremental_column)} >= {databases.placeholder(source)}"
            params = (databases.cursor_parameter(cursor_value, metadata[incremental_column], source),)
        else:
            incremental_run = False
            progress(
                "job_plan", 65,
                f"{job['name']}：已保存的增量游标与字段 {incremental_column}（{declared_type or '未知类型'}）不匹配，本次改为全量扫描",
            )
    order_columns = [name for name in (incremental_column, unique_key) if name and name in metadata]
    if order_columns:
        query += " ORDER BY " + ", ".join(databases.quote_identifier(source, name) for name in dict.fromkeys(order_columns))

    rows: List[Dict[str, Any]] = []
    max_cursor = None
    count = 0
    occurrences: Dict[str, int] = {}
    connection = sql_connection(source, settings=settings) if settings is not None else sql_connection(source)
    with connection as conn:
        with databases.cursor_for(conn, source, streaming=True) as cursor:
            databases.prepare_read(cursor, source)
            cursor.execute(query, params)
            while True:
                values = cursor.fetchmany(1000)
                if not values:
                    break
                chunk = []
                for values_row in values:
                    if len(values_row) != len(selected):
                        raise ConfigError(f"任务 {job['name']} 返回的列数与所选字段数不一致")
                    row = {name: decode_sql_value(value, metadata[name], source) for name, value in zip(selected, values_row)}
                    chunk.append(row)
                if "generated_key" in job:
                    add_generated_row_keys(chunk, job, occurrences)
                if incremental_column:
                    values = [row[incremental_column] for row in chunk if row.get(incremental_column) is not None]
                    if values:
                        chunk_max = max(values)
                        max_cursor = chunk_max if max_cursor is None else max(max_cursor, chunk_max)
                if row_sink is not None:
                    row_sink(chunk)
                else:
                    rows.extend(chunk)
                count += len(chunk)
                progress("sql_read", 50, f"{job['name']}：已读取 {count} 行")
    return rows, max_cursor, incremental_run


def _cursor_fits_sql_type(value: Any, sql_type: str, source: Optional[Dict[str, Any]] = None) -> bool:
    """Detect a cursor that no longer matches its incremental column type."""
    kind = (sql_type or "").lower()
    if kind in DATE_TYPES:
        if source and databases.database_type(source) == "sqlite":
            return isinstance(value, str)
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
    rows: Sequence[Dict[str, Any]], unique_key: str, job_name: str, field_type: int,
    timezone_offset: Optional[float] = None,
) -> List[str]:
    keys: List[str] = []
    seen = set()
    duplicates = set()
    for row in rows:
        try:
            raw = row.get(unique_key)
            key = normalize_business_key(convert_value(raw, field_type, timezone_offset), field_type)
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
    client: FeishuClient, app_token: str, job: Dict[str, Any],
    expected_types: Optional[Dict[str, int]] = None,
) -> str:
    unique_key = job["unique_key"]
    ordered = sorted(job["columns"], key=lambda item: item["source"] != unique_key)
    fields = []
    for column in ordered:
        field_type = (expected_types or {}).get(column["target"], column_field_type(column, unique_key))
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
    client: FeishuClient, app_token: str, table_id: str, column: Dict[str, Any], unique_key: str,
    expected_type: Optional[int] = None,
) -> None:
    field_type = expected_type if expected_type is not None else column_field_type(column, unique_key)
    payload: Dict[str, Any] = {"field_name": column["target"], "type": field_type}
    if field_type == 5:
        payload["property"] = {"date_formatter": "yyyy-MM-dd HH:mm"}
    client.request(
        "POST",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        params={"client_token": str(uuid.uuid4())},
        json=payload,
    )


def save_target_binding(job: Dict[str, Any], table_id: str) -> None:
    # Running workers retain their execution snapshot. Only merge the discovered
    # table binding back, so a plan edited in the dashboard remains intact.
    with config_edit_lock():
        latest = load_config()
        current = next((item for item in latest["jobs"] if item["id"] == job["id"]), None)
        if current is None or any(current.get(key) != job.get(key) for key in ("source_id", "schema", "table")):
            return
        if any(current["target"].get(key) != job["target"].get(key)
               for key in ("mode", "table_name", "table_id")):
            return
        current["target"]["table_id"] = table_id
        save_config(latest)


def ensure_target(
    client: FeishuClient,
    app_token: str,
    config: Dict[str, Any],
    job: Dict[str, Any],
    dry_run: bool,
    expected_types: Optional[Dict[str, int]] = None,
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
                save_target_binding(job, table_id)
                job["target"]["table_id"] = table_id
        elif dry_run:
            field_types = {
                column["target"]: (expected_types or {}).get(column["target"], column_field_type(column, job["unique_key"])) for column in job["columns"]
            }
            return None, field_types, "create"
        else:
            table_id = create_table(client, app_token, job, expected_types)
            save_target_binding(job, table_id)
            job["target"]["table_id"] = table_id
            action = "created"

    fields = client.list_fields(app_token, table_id)
    field_types = {str(item.get("field_name")): int(item.get("type", 1)) for item in fields}
    missing = [column for column in job["columns"] if column["target"] not in field_types]
    if missing and not job["target"].get("auto_create_fields"):
        raise ConfigError(f"任务 {job['name']} 的飞书目标表缺少字段: {', '.join(item['target'] for item in missing)}")
    if missing and dry_run:
        action = "add_fields"
        for column in missing:
            field_types[column["target"]] = (expected_types or {}).get(column["target"], column_field_type(column, job["unique_key"]))
    elif missing:
        for column in missing:
            create_field(client, app_token, table_id, column, job["unique_key"], (expected_types or {}).get(column["target"]))
        fields = client.list_fields(app_token, table_id)
        field_types = {str(item.get("field_name")): int(item.get("type", 1)) for item in fields}
        action = "fields_created" if action == "existing" else action
    return table_id, field_types, action


def build_record(row: Dict[str, Any], columns: Sequence[Dict[str, Any]], field_types: Dict[str, int],
                 settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fields = {}
    settings = settings or runtime_settings()
    null_policy = str(settings["null_policy"]).lower()
    for column in columns:
        value = row.get(column["source"])
        if value is None and null_policy == "skip":
            continue
        try:
            fields[column["target"]] = convert_value(value, field_types[column["target"]], settings["timezone_offset"])
        except ConfigError as exc:
            raise ConfigError(f"字段 {column['source']} → {column['target']}，{exc}") from exc
    return {"fields": fields}


def partition_name(job: Dict[str, Any], number: int) -> str:
    suffix = "__" + hashlib.sha256(job["id"].encode("utf-8")).hexdigest()[:8] + "_"
    return job["target"]["table_name"][:83] + suffix + f"{number:06d}"


def partition_job(job: Dict[str, Any], partition: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(job)
    result["target"].update(mode="auto", table_id=partition["table_id"] or "", table_name=partition["table_name"])
    return result


def sync_partitions(client, app_token, config, job, table_id, field_types, dry_run, known_partitions=()):
    partitions = [{"number": 1, "table_id": table_id, "table_name": job["target"]["table_name"], "rows": 0}]
    prefix = partition_name(job, 2)[:-6]
    numbers = set()
    known_ids = {part["table_id"]: part["number"] for part in known_partitions
                 if part.get("table_id") and isinstance(part.get("number"), int) and part["number"] > 1}
    tables = client.list_tables(app_token)
    for table in tables:
        if table.get("table_id") == table_id:
            partitions[0]["table_name"] = table["name"]
    for table in tables:
        name = table.get("name", "")
        number = known_ids.get(table.get("table_id"))
        if number is None:
            if not name.startswith(prefix) or not re.fullmatch(r"[0-9]{6}", name[len(prefix):]):
                continue
            number = int(name[len(prefix):])
        if number < 2:
            continue
        if number in numbers:
            raise ConfigError(f"存在重复分表序号: {name}")
        numbers.add(number)
        partition = {"number": number, "table_id": table["table_id"], "table_name": name, "rows": 0}
        _, types, _ = ensure_target(client, app_token, config, partition_job(job, partition), dry_run,
                                     expected_types=field_types)
        if any(types[col["target"]] != field_types[col["target"]] for col in job["columns"]):
            raise ConfigError(f"分表 {name} 的字段类型与主表不一致")
        partitions.append(partition)
    return sorted(partitions, key=lambda part: part["number"])


def run_job(
    client: FeishuClient,
    app_token: str,
    config: Dict[str, Any],
    state: Dict[str, Any],
    job: Dict[str, Any],
    dry_run: bool,
) -> Dict[str, Any]:
    source = get_source(config, job["source_id"])
    settings = runtime_settings(config)
    table_id, field_types, target_action = ensure_target(client, app_token, config, job, dry_run)
    validate_target_fields(job, field_types)
    fingerprint = job_fingerprint(source, job, app_token, table_id, field_types, settings)
    job_state = state["jobs"].get(job["id"]) or {}
    # A saved cursor is only reusable while the source, fields, key and target table stay identical.
    cursor_reset = bool(job_state.get("fingerprint")) and job_state["fingerprint"] != fingerprint
    if cursor_reset:
        progress(
            "job_plan", 68,
            f"{job['name']}：数据源、字段、唯一键、增量字段或目标飞书表已变更，本次自动执行全量重新扫描",
        )
        job_state = {}
    unique_column = next(item for item in job["columns"] if item["source"] == job["unique_key"])
    unique_field_type = field_types[unique_column["target"]]
    split = job.get("split", {"enabled": True, "row_limit": 40000, "column": ""})
    split_column = split.get("column") if split.get("enabled") else ""
    previous_state = state["jobs"].get(job["id"]) or {}
    known_partitions = previous_state.get("partitions", []) if previous_state.get("table_id") == table_id else []
    partitions = sync_partitions(client, app_token, config, job, table_id, field_types, dry_run, known_partitions)
    partition_map = {part["number"]: part for part in partitions}
    with tempfile.TemporaryDirectory(prefix="sqlfeishu-") as staging_dir:
        with contextlib.closing(sqlite3.connect(os.path.join(staging_dir, "records.sqlite"))) as staging:
            staging.execute("CREATE TABLE source_rows (key TEXT PRIMARY KEY, record TEXT NOT NULL, sort_value INTEGER)")
            staging.execute("CREATE TABLE target_rows (key TEXT PRIMARY KEY, record TEXT NOT NULL, partition INTEGER)")
            staging.execute("CREATE TABLE operations (kind TEXT, record TEXT NOT NULL, partition INTEGER)")
            counts = {"source_rows": 0, "create": 0, "update": 0, "skip": 0}

            def stage_rows(chunk):
                keys = validate_row_keys(chunk, job["unique_key"], job["name"], unique_field_type,
                                         settings["timezone_offset"])
                for row, key in zip(chunk, keys):
                    record = build_record(row, job["columns"], field_types, settings)
                    try:
                        sort_value = 0
                        if split_column:
                            sort_value = convert_value(row[split_column], 5, settings["timezone_offset"])
                            if sort_value is None:
                                sort_value = 9223372036854775807
                        staging.execute("INSERT INTO source_rows VALUES (?, ?, ?)",
                                        (key, json.dumps(record, ensure_ascii=False), sort_value))
                    except sqlite3.IntegrityError as exc:
                        raise ConfigError(f"任务 {job['name']} 的 SQL 结果存在重复唯一键: {key}") from exc
                counts["source_rows"] += len(chunk)
                staging.commit()

            _, max_cursor, incremental_run = fetch_job_rows(source, job, job_state or None, row_sink=stage_rows, settings=settings)
            lookup_mode = "full"
            if not counts["source_rows"]:
                lookup_mode = "empty"
            # Text keys are trimmed by our comparison rules. An exact API filter
            # cannot find legacy values with surrounding spaces, so retain the
            # full comparison for those fields to avoid duplicate inserts.
            elif incremental_run and unique_field_type == 2 and counts["source_rows"] <= 1000:
                lookup_mode = "keys"
            for partition in partitions:
                if not partition["table_id"] or lookup_mode == "empty":
                    continue
                total = client.record_count(app_token, partition["table_id"], unique_column["target"]) if lookup_mode == "keys" else None
                count_known = type(total) is int and total >= 0
                if count_known:
                    partition["rows"] = total
                    keys = [item[0] for item in staging.execute("SELECT key FROM source_rows")]
                    records = client.iter_records_by_keys(app_token, partition["table_id"],
                        [col["target"] for col in job["columns"]], unique_column["target"], keys)
                    progress("feishu_check", 65, f"{partition['table_name']}：按唯一键核对 {len(keys)} 行增量，现有 {total} 行")
                else:
                    records = client.iter_records(app_token, partition["table_id"], [col["target"] for col in job["columns"]])
                for record in records:
                    if not count_known:
                        partition["rows"] += 1
                    raw = (record.get("fields") or {}).get(unique_column["target"])
                    if raw is None or raw == "":
                        continue
                    key = normalize_business_key(raw, unique_field_type)
                    try:
                        staging.execute("INSERT INTO target_rows VALUES (?, ?, ?)",
                                        (key, json.dumps(record, ensure_ascii=False), partition["number"]))
                    except sqlite3.IntegrityError as exc:
                        raise ConfigError(f"飞书目标表或分表存在重复唯一键: {key}") from exc
            if lookup_mode == "empty":
                previous_counts = {part["table_id"]: part.get("rows", 0) for part in known_partitions}
                for partition in partitions:
                    partition["rows"] = previous_counts.get(partition["table_id"], 0)
            staging.commit()
            for key, payload in staging.execute("SELECT key, record FROM source_rows ORDER BY sort_value, key"):
                record = json.loads(payload)
                current = staging.execute("SELECT record, partition FROM target_rows WHERE key = ?", (key,)).fetchone()
                kind = "create"
                destination = partitions[-1]
                if current:
                    destination = partition_map[current[1]]
                    current = json.loads(current[0])
                    if _records_equal(record["fields"], current.get("fields") or {}):
                        counts["skip"] += 1
                        continue
                    kind = "update"
                    record["record_id"] = current["record_id"]
                if kind == "create":
                    if split.get("enabled") and destination["rows"] >= split["row_limit"]:
                        number = destination["number"] + 1
                        destination = {"number": number, "table_id": None,
                                       "table_name": partition_name(job, number), "rows": 0}
                        partitions.append(destination)
                        partition_map[number] = destination
                    destination["rows"] += 1
                counts[kind] += 1
                staging.execute("INSERT INTO operations VALUES (?, ?, ?)",
                                (kind, json.dumps(record, ensure_ascii=False), destination["number"]))
            staging.commit()
            summary = {
                "job_id": job["id"], "job_name": job["name"], **counts,
                "incremental": incremental_run, "cursor_reset": cursor_reset,
                "target_action": target_action, "table_id": table_id or "",
                "partitions": partitions, "split_column": split_column or "", "lookup_mode": lookup_mode,
            }
            progress("job_plan", 70, f"{job['name']}：读取 {counts['source_rows']}，新增 {counts['create']}，"
                     f"更新 {counts['update']}，跳过 {counts['skip']}，目标表 {len(partitions)} 张")
            if dry_run:
                return summary
            if not table_id:
                raise RuntimeError("正式同步时飞书目标表未创建")
            for partition in partitions:
                if not partition["table_id"]:
                    partition["table_id"], types, _ = ensure_target(
                        client, app_token, config, partition_job(job, partition), False, expected_types=field_types)
                    if any(types[col["target"]] != field_types[col["target"]] for col in job["columns"]):
                        raise ConfigError(f"分表 {partition['table_name']} 的字段类型与主表不一致")
                for kind, write in (("create", client.batch_create), ("update", client.batch_update)):
                    operations = staging.execute("SELECT record FROM operations WHERE kind = ? AND partition = ?",
                                                 (kind, partition["number"]))
                    written = 0
                    while True:
                        batch = operations.fetchmany(BATCH_SIZE)
                        if not batch:
                            break
                        write(app_token, partition["table_id"], [json.loads(item[0]) for item in batch])
                        written += len(batch)
                        progress("feishu_write", 85, f"{partition['table_name']}：{kind} 已写入 {written} 条")
    entry: Dict[str, Any] = {
        "last_success_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "table_id": table_id,
        "fingerprint": fingerprint,
        "partitions": partitions,
    }
    if max_cursor is not None:
        entry["cursor"] = _encode_cursor(max_cursor)
    elif incremental_run and job_state.get("cursor"):
        entry["cursor"] = job_state["cursor"]
    state["jobs"][job["id"]] = entry
    save_state(state)
    return summary


def run(dry_run: bool = False, job_id: Optional[str] = None, job_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    configure_logging()
    if job_ids is not None and (not job_ids or job_id):
        raise ConfigError("请选择明确且非空的任务范围")
    config = load_config()
    state = load_state()
    jobs = [job for job in config["jobs"] if job.get("enabled")]
    selected_ids = set(job_ids or ([job_id] if job_id else []))
    if selected_ids:
        jobs = [job for job in jobs if job["id"] in selected_ids]
        if {job["id"] for job in jobs} != selected_ids:
            raise ConfigError("选择的任务不存在或已停用，请刷新任务列表")
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
    parser.add_argument("--job-id", action="append")
    args = parser.parse_args()
    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(130))
    try:
        run(dry_run=args.dry_run, job_ids=args.job_id)
        return 0
    except Exception as exc:
        progress("error", 0, f"执行失败: {exc}")
        print(json.dumps({"type": "result", "ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
