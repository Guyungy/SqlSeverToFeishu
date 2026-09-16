#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safely synchronize SQL Server rows to Feishu Base records."""

import argparse
import contextlib
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pymssql
import requests
from dotenv import load_dotenv

from feishu_link import parse_feishu_base_url
from workspace import runtime_settings

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
MAPPING_PATH = BASE_DIR / "mapping.json"
LOCK_PATH = BASE_DIR / ".sync.lock"
LOG_PATH = BASE_DIR / "sync.log"
FEISHU_API = "https://open.feishu.cn/open-apis"
BATCH_SIZE = 500
FETCH_SIZE = 1000
REQUEST_TIMEOUT = (5, 60)
TRANSIENT_CODES = {1254290, 1254291, 1254607}
TOKEN_EXPIRED_CODES = {99991663, 99991664, 99991668}
SECRET_NAMES = {"DB_PASSWORD", "FEISHU_APP_SECRET"}

load_dotenv(ENV_PATH, override=True)
logger = logging.getLogger("sql_to_feishu")


class ConfigError(RuntimeError):
    """Configuration cannot safely be used."""


class FeishuAPIError(RuntimeError):
    """Feishu returned a transport or application-level error."""


def configure_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def progress(phase: str, percent: int, message: str) -> None:
    print(f"PROGRESS:{phase}:{percent}:{message}", flush=True)


def _clean_identifier_part(part: str) -> str:
    value = part.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].replace("]]", "]")
    if not value or len(value) > 128:
        raise ConfigError("SQL 标识符为空或过长")
    if any(token in value for token in (";", "--", "/*", "*/", "'", '"', "\x00")):
        raise ConfigError(f"SQL 标识符包含不安全字符: {part}")
    if re.search(r"[\r\n\t()]", value):
        raise ConfigError(f"SQL 标识符格式不正确: {part}")
    return value


def parse_table_name(table_name: str) -> Tuple[str, str]:
    """Accept only table or schema.table, returning unquoted parts."""
    raw = (table_name or "").strip()
    parts = raw.split(".")
    if len(parts) == 1:
        return "dbo", _clean_identifier_part(parts[0])
    if len(parts) == 2:
        return _clean_identifier_part(parts[0]), _clean_identifier_part(parts[1])
    raise ConfigError("数据表名仅支持 table 或 schema.table 格式")


def quote_identifier(value: str) -> str:
    return f"[{_clean_identifier_part(value).replace(']', ']]')}]"


def quoted_table(table_name: str) -> str:
    schema, table = parse_table_name(table_name)
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"


def validate_mapping(data: Dict[str, Any]) -> Dict[str, Any]:
    mappings = data.get("mapping")
    unique_key = str(data.get("unique_key") or "").strip()
    if not isinstance(mappings, list) or not mappings:
        raise ConfigError("字段映射不能为空")
    cleaned: List[Dict[str, str]] = []
    sql_seen = set()
    feishu_seen = set()
    for index, item in enumerate(mappings, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"第 {index} 条字段映射格式错误")
        sql_name = str(item.get("sql") or "").strip()
        feishu_name = str(item.get("feishu") or "").strip()
        if not sql_name or not feishu_name:
            raise ConfigError(f"第 {index} 条字段映射不完整")
        _clean_identifier_part(sql_name)
        if sql_name in sql_seen:
            raise ConfigError(f"SQL 字段重复映射: {sql_name}")
        if feishu_name in feishu_seen:
            raise ConfigError(f"飞书字段被重复映射: {feishu_name}")
        sql_seen.add(sql_name)
        feishu_seen.add(feishu_name)
        cleaned.append({"sql": sql_name, "feishu": feishu_name})
    if not unique_key or unique_key not in sql_seen:
        raise ConfigError("唯一键必须选择一个已映射的 SQL 字段")
    unique_key_label = next(item["feishu"] for item in cleaned if item["sql"] == unique_key)
    return {
        "mapping": cleaned,
        "unique_key": unique_key,
        "unique_key_label": unique_key_label,
    }


def load_mapping() -> Dict[str, Any]:
    try:
        with MAPPING_PATH.open("r", encoding="utf-8") as file:
            return validate_mapping(json.load(file))
    except FileNotFoundError as exc:
        raise ConfigError("缺少 mapping.json，请先在管理后台保存字段映射") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError("mapping.json 已损坏，请重新保存字段映射") from exc


def resolve_feishu_target() -> Dict[str, str]:
    url = (os.environ.get("FEISHU_BASE_URL") or "").strip()
    parsed = parse_feishu_base_url(url) if url else {}
    return {
        "app_token": parsed.get("app_token") or (os.environ.get("FEISHU_BASE_APP_TOKEN") or "").strip(),
        "table_id": parsed.get("table_id") or (os.environ.get("FEISHU_BASE_TABLE_ID") or "").strip(),
        "view_id": parsed.get("view_id") or (os.environ.get("FEISHU_BASE_VIEW_ID") or "").strip(),
    }


def validate_environment() -> Dict[str, str]:
    required = ["DB_SERVER", "DB_NAME", "DB_TABLE", "FEISHU_APP_ID", "FEISHU_APP_SECRET"]
    missing = [key for key in required if not (os.environ.get(key) or "").strip()]
    target = resolve_feishu_target()
    if not target["app_token"]:
        missing.append("FEISHU_BASE_URL/FEISHU_BASE_APP_TOKEN")
    if not target["table_id"]:
        missing.append("FEISHU_BASE_TABLE_ID")
    if missing:
        raise ConfigError("缺少必填配置: " + ", ".join(missing))
    parse_table_name(os.environ["DB_TABLE"])
    try:
        port = int(os.environ.get("DB_PORT", "1433"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise ConfigError("DB_PORT 必须是 1-65535 之间的整数") from exc
    return target


def get_sql_connection():
    user = (os.environ.get("DB_USER") or "").strip()
    kwargs = {
        "server": os.environ["DB_SERVER"],
        "port": int(os.environ.get("DB_PORT", "1433")),
        "database": os.environ["DB_NAME"],
        "charset": "utf8",
        "login_timeout": 10,
        "timeout": int(runtime_settings()["query_timeout"]),
    }
    if user:
        kwargs.update(user=user, password=os.environ.get("DB_PASSWORD") or "")
    return pymssql.connect(**kwargs)


def get_sql_columns(conn, table_name: str) -> List[str]:
    schema, table = parse_table_name(table_name)
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (schema, table),
        )
        return [row[0] for row in cursor.fetchall()]


def fetch_rows(table_name: str, columns: Sequence[str]) -> List[Dict[str, Any]]:
    progress("sql_connect", 10, "连接 SQL Server 并校验字段...")
    with get_sql_connection() as conn:
        available = set(get_sql_columns(conn, table_name))
        missing = [column for column in columns if column not in available]
        if missing:
            raise ConfigError("SQL Server 中不存在字段: " + ", ".join(missing))
        query_columns = ", ".join(quote_identifier(column) for column in columns)
        query = f"SELECT {query_columns} FROM {quoted_table(table_name)}"
        progress("sql_fetch", 25, "正在分批读取 SQL Server 数据...")
        rows: List[Dict[str, Any]] = []
        with conn.cursor(as_dict=True) as cursor:
            cursor.execute(query)
            while True:
                chunk = cursor.fetchmany(FETCH_SIZE)
                if not chunk:
                    break
                rows.extend(chunk)
                progress("sql_fetch", min(45, 25 + len(rows) // 500), f"已读取 {len(rows)} 条记录")
    progress("sql_fetch", 45, f"SQL Server 读取完成，共 {len(rows)} 条记录")
    return rows


def normalize_key(value: Any) -> str:
    if value is None:
        raise ConfigError("唯一键存在 NULL 值")
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    text = str(value).strip()
    if not text:
        raise ConfigError("唯一键存在空值")
    return text


def validate_source_keys(rows: Sequence[Dict[str, Any]], unique_key: str) -> List[str]:
    keys: List[str] = []
    positions: Dict[str, int] = {}
    duplicates = []
    for index, row in enumerate(rows, start=1):
        key = normalize_key(row.get(unique_key))
        if key in positions:
            duplicates.append(f"{key}（第 {positions[key]}、{index} 行）")
        else:
            positions[key] = index
        keys.append(key)
    if duplicates:
        preview = "、".join(duplicates[:10])
        suffix = "…" if len(duplicates) > 10 else ""
        raise ConfigError(f"SQL 数据存在重复唯一键: {preview}{suffix}")
    return keys


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, api_base: str = FEISHU_API):
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        self.token: Optional[str] = None

    def _auth(self) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(5):
            try:
                response = self.session.post(
                    f"{self.api_base}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = FeishuAPIError(f"飞书授权接口暂时不可用（HTTP {response.status_code}）")
                    if attempt < 4:
                        time.sleep(min(8.0, 0.5 * (2 ** attempt) + random.random()))
                        continue
                    break
                response.raise_for_status()
                data = response.json()
                if data.get("code") != 0 or not data.get("tenant_access_token"):
                    raise FeishuAPIError(f"飞书授权失败: {data.get('msg') or data.get('code')}")
                self.token = data["tenant_access_token"]
                return self.token
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt) + random.random()))
                    continue
                break
            except requests.HTTPError as exc:
                raise FeishuAPIError(f"飞书授权 HTTP 错误: {exc.response.status_code}") from exc
        raise FeishuAPIError(f"飞书授权重试后仍失败: {last_error}")

    def request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        request_headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in range(5):
            if not self.token:
                self._auth()
            headers = dict(request_headers)
            headers["Authorization"] = f"Bearer {self.token}"
            try:
                response = self.session.request(
                    method,
                    f"{self.api_base}{path}",
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                    **kwargs,
                )
                data = response.json() if response.content else {}
                code = data.get("code", 0)
                if response.status_code in (401, 403) or code in TOKEN_EXPIRED_CODES:
                    if attempt < 4:
                        self.token = None
                        continue
                    raise FeishuAPIError("飞书访问令牌刷新后仍无效")
                if response.status_code == 429 or response.status_code >= 500 or code in TRANSIENT_CODES:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else min(8.0, 0.5 * (2 ** attempt) + random.random())
                    last_error = FeishuAPIError(f"飞书接口暂时不可用（HTTP {response.status_code}, code {code}）")
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                if code != 0:
                    raise FeishuAPIError(f"飞书接口错误 code={code}: {data.get('msg', '未知错误')}")
                return data
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt) + random.random()))
                    continue
                break
            except requests.HTTPError as exc:
                raise FeishuAPIError(f"飞书接口 HTTP 错误: {exc.response.status_code}") from exc
        raise FeishuAPIError(f"飞书接口重试后仍失败: {last_error}")

    def list_tables(self, app_token: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token = None
        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.request("GET", f"/bitable/v1/apps/{app_token}/tables", params=params)
            page = data.get("data", {})
            items.extend(page.get("items") or [])
            if not page.get("has_more"):
                return items
            page_token = page.get("page_token")
            if not page_token:
                raise FeishuAPIError("飞书表格分页响应缺少 page_token")

    def list_fields(self, app_token: str, table_id: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token = None
        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.request(
                "GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields", params=params
            )
            page = data.get("data", {})
            items.extend(page.get("items") or [])
            if not page.get("has_more"):
                return items
            page_token = page.get("page_token")
            if not page_token:
                raise FeishuAPIError("飞书字段分页响应缺少 page_token")

    def list_records(
        self, app_token: str, table_id: str, field_names: Sequence[str]
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token = None
        while True:
            params: Dict[str, Any] = {
                "page_size": 500,
                "field_names": json.dumps(list(field_names), ensure_ascii=False),
            }
            if page_token:
                params["page_token"] = page_token
            data = self.request(
                "GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/records", params=params
            )
            page = data.get("data", {})
            items.extend(page.get("items") or [])
            progress("feishu_check", min(70, 55 + len(items) // 500), f"已读取飞书现有记录 {len(items)} 条")
            if not page.get("has_more"):
                return items
            page_token = page.get("page_token")
            if not page_token:
                raise FeishuAPIError("飞书记录分页响应缺少 page_token")

    def batch_create(self, app_token: str, table_id: str, records: Sequence[Dict[str, Any]]) -> None:
        payload = {"records": list(records)}
        client_token = str(uuid.uuid4())
        self.request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            params={"client_token": client_token},
            json=payload,
        )

    def batch_update(self, app_token: str, table_id: str, records: Sequence[Dict[str, Any]]) -> None:
        self.request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            json={"records": list(records)},
        )


def get_tenant_access_token() -> str:
    """Compatibility helper used by older callers."""
    client = FeishuClient(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"])
    return client._auth()


def _date_to_milliseconds(value: Any, timezone_offset: Optional[float] = None) -> int:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float, Decimal)):
        number = int(value)
        return number if number > 10_000_000_000 else number * 1000
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigError(f"无法转换日期值: {value}") from exc
    else:
        raise ConfigError(f"不支持的日期值类型: {type(value).__name__}")
    if dt.tzinfo is None:
        # 逐行调用，所以这里不做配置文件 I/O：内部调用方一律显式传入时区偏移，
        # 只有外部直接调用才退回环境变量（与改造前行为一致）。
        raw_offset = timezone_offset if timezone_offset is not None else (os.environ.get("SYNC_TIMEZONE_OFFSET") or 8)
        try:
            offset = float(raw_offset)
        except (TypeError, ValueError) as exc:
            raise ConfigError("时区偏移必须是数字，例如中国标准时间填写 8") from exc
        if not -12 <= offset <= 14:
            raise ConfigError("时区偏移必须在 -12 到 14 之间")
        dt = dt.replace(tzinfo=timezone(timedelta(hours=offset)))
    return int(dt.timestamp() * 1000)


def convert_value(value: Any, field_type: int, timezone_offset: Optional[float] = None) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if field_type == 2:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (int, float)):
            return value
        text = str(value).strip()
        return float(text) if "." in text else int(text)
    if field_type == 5:
        return _date_to_milliseconds(value, timezone_offset)
    if field_type == 7:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}
    if field_type == 4:
        if isinstance(value, list):
            return value
        return [part.strip() for part in str(value).split(",") if part.strip()]
    if field_type == 15:
        if isinstance(value, dict):
            return value
        text = str(value)
        return {"text": text, "link": text}
    if isinstance(value, (dict, list, int, float, bool)) and field_type not in {1, 3}:
        return value
    return str(value)


def build_record(
    row: Dict[str, Any], mapping: Sequence[Dict[str, str]], field_types: Dict[str, int],
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = settings or runtime_settings()
    null_policy = str(settings["null_policy"]).lower()
    timezone_offset = settings["timezone_offset"]
    fields: Dict[str, Any] = {}
    for item in mapping:
        value = row.get(item["sql"])
        if value is None and null_policy == "skip":
            continue
        fields[item["feishu"]] = convert_value(
            value, field_types.get(item["feishu"], 1), timezone_offset
        )
    return {"fields": fields}


def _target_record_map(
    records: Iterable[Dict[str, Any]], unique_key_label: str
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    duplicates = []
    for item in records:
        fields = item.get("fields") or {}
        raw_key = fields.get(unique_key_label)
        if raw_key is None or raw_key == "":
            continue
        key = normalize_key(raw_key)
        if key in result:
            duplicates.append(key)
        else:
            result[key] = item
    if duplicates:
        raise ConfigError("飞书目标表存在重复唯一键: " + "、".join(sorted(set(duplicates))[:10]))
    return result


def _records_equal(source_fields: Dict[str, Any], target_fields: Dict[str, Any]) -> bool:
    return all(target_fields.get(name) == value for name, value in source_fields.items())


@contextlib.contextmanager
def sync_lock():
    if LOCK_PATH.exists():
        try:
            pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            raise RuntimeError(f"已有同步任务正在运行（PID {pid}）")
        except ProcessLookupError:
            LOCK_PATH.unlink(missing_ok=True)
        except (ValueError, PermissionError):
            raise RuntimeError("检测到同步锁文件，请确认没有其他同步任务后删除 .sync.lock")
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        LOCK_PATH.unlink(missing_ok=True)


def prepare_sync() -> Tuple[FeishuClient, Dict[str, str], List[Dict[str, Any]], List[Dict[str, Any]], int]:
    target = validate_environment()
    settings = runtime_settings()
    mapping_config = load_mapping()
    mapping = mapping_config["mapping"]
    unique_key = mapping_config["unique_key"]
    unique_key_label = mapping_config["unique_key_label"]
    rows = fetch_rows(os.environ["DB_TABLE"], [item["sql"] for item in mapping])
    keys = validate_source_keys(rows, unique_key)
    if not rows:
        return FeishuClient(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"]), target, [], [], 0

    progress("feishu_auth", 50, "连接飞书并校验目标字段...")
    client = FeishuClient(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"])
    fields = client.list_fields(target["app_token"], target["table_id"])
    field_types = {str(item.get("field_name")): int(item.get("type", 1)) for item in fields}
    missing_fields = [item["feishu"] for item in mapping if item["feishu"] not in field_types]
    if missing_fields:
        raise ConfigError("飞书目标表中不存在字段: " + ", ".join(missing_fields))
    supported_types = {1, 2, 3, 4, 5, 7, 13, 15}
    unsupported_fields = [
        f"{item['feishu']}（类型 {field_types[item['feishu']]}）"
        for item in mapping
        if field_types[item["feishu"]] not in supported_types
    ]
    if unsupported_fields:
        raise ConfigError(
            "暂不支持写入以下复杂或只读飞书字段: " + ", ".join(unsupported_fields)
            + "。请改映射到文本、数字、单选、多选、日期、复选框、电话或超链接字段"
        )
    if field_types.get(unique_key_label) not in {1, 2, 3, 13}:
        raise ConfigError("唯一键对应的飞书字段必须是文本、数字、单选或电话字段")

    progress("feishu_check", 55, "分页读取飞书现有记录...")
    target_records = client.list_records(
        target["app_token"], target["table_id"], [item["feishu"] for item in mapping]
    )
    existing = _target_record_map(target_records, unique_key_label)

    to_create: List[Dict[str, Any]] = []
    to_update: List[Dict[str, Any]] = []
    skipped = 0
    for row, key in zip(rows, keys):
        record = build_record(row, mapping, field_types, settings)
        current = existing.get(key)
        if current:
            if _records_equal(record["fields"], current.get("fields") or {}):
                skipped += 1
                continue
            record["record_id"] = current["record_id"]
            to_update.append(record)
        else:
            to_create.append(record)
    return client, target, to_create, to_update, skipped


def sync(dry_run: bool = False) -> Dict[str, int]:
    progress("init", 0, "准备同步并执行安全预检...")
    with sync_lock():
        client, target, to_create, to_update, skipped = prepare_sync()
        summary = {"create": len(to_create), "update": len(to_update), "skip": skipped}
        progress(
            "preflight",
            80,
            f"预检完成：新增 {summary['create']}，更新 {summary['update']}，跳过未变化 {summary['skip']}",
        )
        if dry_run:
            progress("done", 100, "预检完成，未写入任何数据")
            print("RESULT:" + json.dumps({"ok": True, "dry_run": True, **summary}, ensure_ascii=False), flush=True)
            return summary
        total_batches = (len(to_create) + BATCH_SIZE - 1) // BATCH_SIZE + (len(to_update) + BATCH_SIZE - 1) // BATCH_SIZE
        completed_batches = 0
        for start in range(0, len(to_create), BATCH_SIZE):
            client.batch_create(target["app_token"], target["table_id"], to_create[start:start + BATCH_SIZE])
            completed_batches += 1
            percent = 80 + int(19 * completed_batches / max(total_batches, 1))
            progress("sync", percent, f"已创建 {min(start + BATCH_SIZE, len(to_create))}/{len(to_create)} 条")
        for start in range(0, len(to_update), BATCH_SIZE):
            client.batch_update(target["app_token"], target["table_id"], to_update[start:start + BATCH_SIZE])
            completed_batches += 1
            percent = 80 + int(19 * completed_batches / max(total_batches, 1))
            progress("sync", percent, f"已更新 {min(start + BATCH_SIZE, len(to_update))}/{len(to_update)} 条")
        progress("done", 100, "同步完成")
        print("RESULT:" + json.dumps({"ok": True, "dry_run": False, **summary}, ensure_ascii=False), flush=True)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="SQL Server 到飞书多维表格同步")
    parser.add_argument("--dry-run", action="store_true", help="仅预检，不写入飞书")
    args = parser.parse_args()
    configure_logging()
    try:
        sync(dry_run=args.dry_run)
        return 0
    except Exception as exc:
        logger.exception("Sync failed: %s", exc)
        progress("error", 0, f"同步失败: {exc}")
        print("RESULT:" + json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
