#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web configuration and monitoring panel for the SQL Server -> Feishu sync script.
Run: python dashboard.py
Open: http://127.0.0.1:5000
"""
import os
import sys
import json
import shutil
import subprocess
import threading
from pathlib import Path
from datetime import datetime

import requests
from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv, set_key

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOG_PATH = BASE_DIR / "sync.log"
MAPPING_PATH = BASE_DIR / "mapping.json"
FEISHU_API = "https://open.feishu.cn/open-apis"

if not ENV_PATH.exists() and (BASE_DIR / ".env.example").exists():
    shutil.copy(BASE_DIR / ".env.example", ENV_PATH)

load_dotenv(ENV_PATH)

CONFIG_KEYS = [
    ("DB_SERVER", "SQL Server 地址", True),
    ("DB_PORT", "SQL Server 端口", True),
    ("DB_NAME", "数据库名", True),
    ("DB_USER", "用户名", False),
    ("DB_PASSWORD", "密码", False),
    ("DB_TABLE", "数据表名", True),
    ("FEISHU_APP_ID", "飞书 App ID", True),
    ("FEISHU_APP_SECRET", "飞书 App Secret", True),
    ("FEISHU_BASE_URL", "飞书多维表格链接(推荐)", False),
    ("FEISHU_BASE_APP_TOKEN", "多维表格 App Token(可留空)", False),
    ("FEISHU_BASE_TABLE_ID", "多维表格 Table ID(可留空)", False),
    ("FEISHU_BASE_VIEW_ID", "飞书多维表格 View ID(可留空)", False),
]

def _feishu_target():
    """复用 sync 的解析逻辑：FEISHU_BASE_URL 链接优先，其次独立字段。"""
    from sync import resolve_feishu_target
    return resolve_feishu_target()

def _base_app_token():
    """获取飞书多维表格的 app_token（bascn/Bak 等开头，不是应用 App ID cli_ 开头）。"""
    from sync import base_app_token
    return base_app_token()

_running_lock = threading.Lock()
_running = False

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/config", methods=["GET"])
def get_config():
    data = {}
    for key, label, required in CONFIG_KEYS:
        value = os.environ.get(key, "")
        if key in ("DB_PASSWORD", "FEISHU_APP_SECRET") and value:
            display = "*" * min(len(value), 12)
        else:
            display = value
        data[key] = {"value": display, "label": label, "required": required}
    return jsonify(data)

@app.route("/api/config", methods=["POST"])
def update_config():
    payload = request.get_json(force=True) or {}
    for key, _, _ in CONFIG_KEYS:
        if key in payload:
            set_key(str(ENV_PATH), key, payload[key] or "")
    load_dotenv(str(ENV_PATH), override=True)
    return jsonify({"success": True})

@app.route("/api/test/sql", methods=["POST"])
def test_sql():
    try:
        from sync import get_sql_connection
        table = os.environ.get("DB_TABLE", "dbo.Orders")
        conn = get_sql_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        count = cur.fetchone()[0]
        conn.close()
        return jsonify({
            "ok": True,
            "count": count,
            "message": f"SQL Server 连接成功，表 {table} 共有 {count} 条记录",
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})

@app.route("/api/test/feishu", methods=["POST"])
def test_feishu():
    try:
        from sync import get_tenant_access_token
        token = get_tenant_access_token()
        target = _feishu_target()
        app_token = target["app_token"]
        if not app_token:
            return jsonify({"ok": False, "message": "缺少多维表格链接/App Token，请先在「飞书配置」填写"})
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(
            f"{FEISHU_API}/bitable/v1/apps/{app_token}/tables",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            return jsonify({"ok": False, "message": data.get("msg", "飞书接口返回错误")})
        items = data.get("data", {}).get("items", [])
        tables = [t.get("name") for t in items]
        want_id = target["table_id"]
        has_table = bool(want_id) and want_id in [t.get("table_id") for t in items]
        hint = "已找到" if has_table else ("未找到，链接可能未带 table，请在地址栏把当前数据表打开后复制" if want_id else "链接未解析出 table_id，请在地址栏打开具体数据表后复制链接")
        return jsonify({
            "ok": True,
            "message": f"飞书连接成功，共 {len(tables)} 个表格，目标表格{hint}",
            "tables": tables,
            "table_ids": [t.get("table_id") for t in items],
            "target": target,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})

@app.route("/api/mapping", methods=["GET"])
def get_mapping():
    try:
        with open(MAPPING_PATH, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as exc:
        return jsonify({"error": str(exc)})

@app.route("/api/mapping", methods=["POST"])
def save_mapping():
    """保存字段映射与唯一键到 mapping.json（运行时可写，通用场景）。"""
    payload = request.get_json(force=True) or {}
    mapping = payload.get("mapping")
    unique_key = payload.get("unique_key")
    unique_key_label = payload.get("unique_key_label")

    if not isinstance(mapping, list):
        return jsonify({"ok": False, "message": "映射数据格式错误"}), 400

    # 清洗：只保留 sql / feishu 两字段
    cleaned = []
    for m in mapping:
        if isinstance(m, dict) and m.get("sql") and m.get("feishu"):
            cleaned.append({"sql": m["sql"], "feishu": m["feishu"]})

    data = {
        "mapping": cleaned,
        "unique_key": unique_key or "",
        "unique_key_label": unique_key_label or "",
    }
    try:
        with open(MAPPING_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True, "message": f"已保存 {len(cleaned)} 条字段映射", "data": data})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500

@app.route("/api/columns/sql", methods=["GET"])
def get_sql_columns():
    """自动读取 SQL Server 表的全部列名（通用场景）。"""
    try:
        from sync import get_sql_connection
        table = os.environ.get("DB_TABLE", "").strip()
        if not table:
            return jsonify({"ok": False, "message": "请先在「数据库配置」填写并保存数据表名"})
        # 从表名里拆出真正的表名（去 schema 前缀），用于 INFORMATION_SCHEMA
        if "." in table:
            bare = table.split(".")[-1].strip()
            schema = table.split(".")[-2].strip() if len(table.split(".")) >= 2 else "dbo"
        else:
            bare, schema = table, "dbo"
        bare = bare.strip("[]")
        conn = get_sql_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = %s AND TABLE_SCHEMA = %s ORDER BY ORDINAL_POSITION",
            (bare, schema),
        )
        cols = [r[0] for r in cur.fetchall()]
        conn.close()
        return jsonify({"ok": True, "columns": cols, "table": table})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})

@app.route("/api/fields/feishu", methods=["GET"])
def get_feishu_fields():
    """自动读取飞书多维表格的全部字段名（通用场景）。"""
    try:
        from sync import get_tenant_access_token
        token = get_tenant_access_token()
        target = _feishu_target()
        table_id = target["table_id"]
        app_token = target["app_token"]
        if not app_token:
            return jsonify({"ok": False, "message": "缺少多维表格链接/App Token，请先在「飞书配置」填写"})
        if not table_id:
            return jsonify({"ok": False, "message": "无法从链接解析出 Table ID。请在飞书里打开具体数据表后复制地址栏链接，或在「飞书配置」填写 Table ID"})
        headers = {"Authorization": f"Bearer {token}"}
        page_token = None
        fields = []
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(
                f"{FEISHU_API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                headers=headers, params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                return jsonify({"ok": False, "message": data.get("msg", "飞书接口返回错误")})
            items = data.get("data", {}).get("items", [])
            fields.extend(i.get("field_name") for i in items)
            if data.get("data", {}).get("has_more"):
                page_token = data.get("data", {}).get("page_token")
            else:
                break
        return jsonify({"ok": True, "fields": fields})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})

@app.route("/api/status")
def status():
    return jsonify({"running": _running})

def _stream_sync():
    global _running
    with _running_lock:
        if _running:
            yield "PROGRESS:busy:0:同步任务正在运行，请等待完成。\n"
            return
        _running = True

    try:
        with open(LOG_PATH, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始同步\n")
            process = subprocess.Popen(
                [sys.executable, "sync.py"],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if process.stdout is None:
                yield "PROGRESS:error:0:无法读取同步进程输出\n"
                return

            for line in process.stdout:
                log_file.write(line)
                log_file.flush()
                yield line

            process.wait()
            exit_msg = f"同步结束，退出码: {process.returncode}\n"
            log_file.write(exit_msg)
            yield exit_msg
    except Exception as exc:
        yield f"PROGRESS:error:0:运行出错: {exc}\n"
    finally:
        _running = False

@app.route("/api/run", methods=["POST"])
def run_sync():
    return Response(_stream_sync(), mimetype="text/plain")

@app.route("/api/logs")
def logs():
    n = request.args.get("n", 200, type=int)
    if LOG_PATH.exists():
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"log": "".join(lines[-n:])})
    return jsonify({"log": ""})

if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=False)
