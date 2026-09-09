#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sync order data from local SQL Server to Feishu Base (多维表格).

Before running:
1. cp .env.example .env
2. Fill in SQL Server connection and Feishu app/table IDs.
3. pip install -r requirements.txt
4. python sync.py
"""
import os
import sys
import json
import logging
from typing import List, Dict, Any

import requests
import pymssql
from dotenv import load_dotenv

from feishu_link import parse_feishu_base_url

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAPPING_PATH = os.path.join(BASE_DIR, "mapping.json")


def load_mapping():
    with open(MAPPING_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


MAPPING = load_mapping()
COLUMN_MAP = {m["sql"]: m["feishu"] for m in MAPPING["mapping"]}
SQL_COLUMNS = list(COLUMN_MAP.keys())
FEISHU_FIELDS = list(COLUMN_MAP.values())
UNIQUE_KEY = MAPPING["unique_key"]
UNIQUE_KEY_LABEL = MAPPING["unique_key_label"]

FEISHU_API = "https://open.feishu.cn/open-apis"


def resolve_feishu_target() -> Dict[str, str]:
    """解析出飞书多维表格目标 {app_token, table_id, view_id}。

    优先级：1) FEISHU_BASE_URL(完整链接，自动解析) 2) 旧的三个独立字段(APP_TOKEN/TABLE_ID/VIEW_ID)。
    链接里解析出的值会覆盖独立字段中仍为占位的同名项。
    """
    url = (os.environ.get("FEISHU_BASE_URL") or "").strip()
    parsed = parse_feishu_base_url(url) if url else {}
    app_token = parsed.get("app_token") or (os.environ.get("FEISHU_BASE_APP_TOKEN") or "").strip()
    table_id = parsed.get("table_id") or (os.environ.get("FEISHU_BASE_TABLE_ID") or "").strip()
    view_id = parsed.get("view_id") or (os.environ.get("FEISHU_BASE_VIEW_ID") or "").strip()
    return {"app_token": app_token, "table_id": table_id, "view_id": view_id}


def base_app_token() -> str:
    """获取飞书多维表格的 app_token（bascn/Bak 等开头，不是应用 App ID cli_ 开头）。"""
    v = resolve_feishu_target()["app_token"]
    if not v:
        raise RuntimeError(
            "缺少配置：请在「飞书配置」粘贴多维表格完整链接(FEISHU_BASE_URL)，"
            "或填写多维表格 App Token(FEISHU_BASE_APP_TOKEN)"
        )
    return v


def base_table_id() -> str:
    v = resolve_feishu_target()["table_id"]
    if not v:
        raise RuntimeError(
            "缺少配置：无法从链接解析到 Table ID。请在多维表格链接 URL 中带上 ?table=tblXXX，"
            "或单独填写多维表格 Table ID(FEISHU_BASE_TABLE_ID)"
        )
    return v


def get_sql_connection():
    """Return a pymssql connection based on environment variables."""
    server = os.environ["DB_SERVER"]
    port = int(os.environ.get("DB_PORT", 1433))
    database = os.environ["DB_NAME"]
    user = os.environ.get("DB_USER")
    password = os.environ.get("DB_PASSWORD")

    if user:
        return pymssql.connect(
            server=server,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8",
        )
    return pymssql.connect(
        server=server,
        port=port,
        database=database,
        charset="utf8",
    )


def count_rows(table_name: str) -> int:
    query = f"SELECT COUNT(*) FROM {table_name}"
    with get_sql_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchone()[0]


def fetch_orders(table_name: str) -> List[Dict[str, Any]]:
    """Fetch rows from the configured SQL Server table/view."""
    columns = ", ".join(f"[{c}]" for c in SQL_COLUMNS)
    query = f"SELECT {columns} FROM {table_name}"
    print("PROGRESS:sql_fetch:30:正在 SQL Server 中查询数据...")
    with get_sql_connection() as conn:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(query)
            rows = cur.fetchall()
    print(f"PROGRESS:sql_fetch:50:从 SQL Server 读取到 {len(rows)} 条记录")
    return rows


# ---------------------------------------------------------------------------
# Feishu helpers
# ---------------------------------------------------------------------------


def get_tenant_access_token() -> str:
    resp = requests.post(
        f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
        json={
            "app_id": os.environ["FEISHU_APP_ID"],
            "app_secret": os.environ["FEISHU_APP_SECRET"],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu auth error: {data}")
    return data["tenant_access_token"]


def list_existing_records(token: str, unique_values: List[str]) -> Dict[str, str]:
    """Return a mapping from business key -> Feishu record_id for existing records."""
    table_id = base_table_id()
    app_token = base_app_token()
    headers = {"Authorization": f"Bearer {token}"}
    existing: Dict[str, str] = {}
    for value in unique_values:
        filter_expr = f'CurrentValue[{UNIQUE_KEY_LABEL}] = "{value}"'
        resp = requests.get(
            f"{FEISHU_API}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=headers,
            params={"filter": filter_expr, "page_size": 1},
        )
        if resp.status_code == 200:
            items = resp.json().get("data", {}).get("items", [])
            if items:
                existing[value] = items[0]["record_id"]
    return existing


def batch_create_records(token: str, records: List[Dict[str, Any]]) -> None:
    table_id = base_table_id()
    app_token = base_app_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{FEISHU_API}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
    resp = requests.post(url, headers=headers, json={"records": records})
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"batch_create failed: {result}")


def batch_update_records(token: str, records: List[Dict[str, Any]]) -> None:
    table_id = base_table_id()
    app_token = base_app_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{FEISHU_API}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    resp = requests.post(url, headers=headers, json={"records": records})
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"batch_update failed: {result}")


def build_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a SQL row dict into a Feishu record body."""
    fields = {}
    for sql_col, feishu_col in COLUMN_MAP.items():
        value = row.get(sql_col)
        if value is None:
            value = ""
        elif isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="replace")
        else:
            value = str(value)
        fields[feishu_col] = value
    return {"fields": fields}


def sync():
    print("PROGRESS:init:0:准备同步...")
    print("PROGRESS:sql_connect:10:连接 SQL Server...")
    table_name = os.environ.get("DB_TABLE", "dbo.Orders")
    total = count_rows(table_name)
    print(f"PROGRESS:sql_count:20:SQL Server 表中共 {total} 条记录")

    rows = fetch_orders(table_name)
    if not rows:
        logger.warning("No rows returned from SQL Server")
        print("PROGRESS:done:100:没有需要同步的数据")
        return

    print("PROGRESS:feishu_auth:60:连接飞书并获取 Token...")
    token = get_tenant_access_token()
    print("PROGRESS:feishu_check:70:检查飞书已有记录...")
    unique_values = [row[UNIQUE_KEY] for row in rows if row.get(UNIQUE_KEY)]
    existing = list_existing_records(token, unique_values)
    print(f"PROGRESS:feishu_check:80:飞书中已存在 {len(existing)} 条记录")

    to_create: List[Dict[str, Any]] = []
    to_update: List[Dict[str, Any]] = []

    for row in rows:
        record = build_record(row)
        key = row.get(UNIQUE_KEY)
        if key and key in existing:
            record["record_id"] = existing[key]
            to_update.append(record)
        else:
            to_create.append(record)

    print(f"PROGRESS:sync_start:85:开始同步，新增 {len(to_create)} 条，更新 {len(to_update)} 条")

    if to_create:
        for i in range(0, len(to_create), 500):
            batch_create_records(token, to_create[i : i + 500])
            print(f"PROGRESS:sync:90:已创建 {min(i+500, len(to_create))}/{len(to_create)} 条")
    if to_update:
        for i in range(0, len(to_update), 500):
            batch_update_records(token, to_update[i : i + 500])
            print(f"PROGRESS:sync:95:已更新 {min(i+500, len(to_update))}/{len(to_update)} 条")

    print("PROGRESS:done:100:同步完成")


if __name__ == "__main__":
    try:
        sync()
    except Exception as exc:
        logger.exception("Sync failed: %s", exc)
        print(f"PROGRESS:error:0:同步失败: {exc}")
        sys.exit(1)
