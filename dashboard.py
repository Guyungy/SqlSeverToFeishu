#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local dashboard for multi-database SQL Server to Feishu synchronization."""

import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import uuid
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request

from multi_sync import feishu_client, list_columns, list_databases, list_tables, resolve_app_token, sql_connection
from workspace import (
    BASE_DIR,
    ENV_PATH,
    get_source,
    load_config,
    load_state,
    new_id,
    password_env_key,
    public_config,
    save_config,
    save_state,
    update_env,
    validate_config,
    validate_source,
)

load_dotenv(ENV_PATH, override=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024
CSRF_TOKEN = secrets.token_urlsafe(32)
LOG_PATH = BASE_DIR / "sync.log"
DASHBOARD_LOG_PATH = BASE_DIR / "dashboard_server.log"
SECRET_MASKS = {"********", "************"}
config_lock = threading.Lock()
state_lock = threading.Lock()

logger = logging.getLogger("dashboard")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(DASHBOARD_LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)


@app.before_request
def protect_local_dashboard():
    hostname = (request.host.split(":", 1)[0] or "").lower()
    if hostname not in {"127.0.0.1", "localhost"}:
        abort(403)
    origin = request.headers.get("Origin")
    if origin and (urlparse(origin).hostname or "").lower() not in {"127.0.0.1", "localhost"}:
        abort(403)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path.startswith("/api/"):
        if request.headers.get("X-CSRF-Token") != CSRF_TOKEN:
            abort(403)


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def safe_error(exc: Exception, fallback: str) -> str:
    if isinstance(exc, (ValueError, RuntimeError)):
        return str(exc)
    logger.exception("%s: %s", fallback, exc)
    return f"{fallback}，详细信息已写入 dashboard_server.log"


def feishu_public_config() -> Dict[str, Any]:
    return {
        "app_id": os.environ.get("FEISHU_APP_ID", ""),
        "app_secret": "",
        "has_app_secret": bool(os.environ.get("FEISHU_APP_SECRET")),
        "base_url": os.environ.get("FEISHU_BASE_URL", ""),
        "app_token": os.environ.get("FEISHU_BASE_APP_TOKEN", ""),
    }


def save_feishu_config(payload: Dict[str, Any]) -> None:
    updates = {
        "FEISHU_APP_ID": str(payload.get("app_id") or "").strip(),
        "FEISHU_BASE_URL": str(payload.get("base_url") or "").strip(),
        "FEISHU_BASE_APP_TOKEN": str(payload.get("app_token") or "").strip(),
    }
    secret = str(payload.get("app_secret") or "")
    if secret and secret not in SECRET_MASKS:
        updates["FEISHU_APP_SECRET"] = secret
    update_env(updates)


class JobManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.process: Optional[subprocess.Popen] = None
        self.job_id: Optional[str] = None
        self.status = "idle"
        self.mode = ""
        self.exit_code: Optional[int] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.cancel_requested = False
        self.seq = 0
        self.lines = deque(maxlen=5000)

    def add_line(self, line: str) -> None:
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, line.rstrip("\n")))

    def start(self, dry_run: bool, sync_job_id: str = "") -> Dict[str, Any]:
        with self.lock:
            if self.status in {"starting", "running", "cancelling"}:
                return {"ok": False, "message": "已有同步任务正在运行", "job_id": self.job_id}
            self.job_id = uuid.uuid4().hex
            self.status = "starting"
            self.mode = "dry_run" if dry_run else "sync"
            self.exit_code = None
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.finished_at = None
            self.cancel_requested = False
            self.seq = 0
            self.lines.clear()
            runtime_id = self.job_id
        threading.Thread(target=self._run, args=(runtime_id, dry_run, sync_job_id), daemon=True).start()
        return {"ok": True, "job_id": runtime_id, "mode": self.mode}

    def _run(self, runtime_id: str, dry_run: bool, sync_job_id: str) -> None:
        command = [sys.executable, "-u", str(BASE_DIR / "multi_sync.py")]
        if dry_run:
            command.append("--dry-run")
        if sync_job_id:
            command.extend(["--job-id", sync_job_id])
        kwargs: Dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            process = subprocess.Popen(
                command, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONUNBUFFERED": "1"}, **kwargs,
            )
            with self.lock:
                if self.job_id != runtime_id:
                    process.terminate()
                    return
                self.process = process
                self.status = "running"
            if process.stdout is None:
                raise RuntimeError("无法读取同步进程输出")
            for line in process.stdout:
                self.add_line(line)
            process.wait()
            with self.lock:
                self.exit_code = process.returncode
                self.status = "cancelled" if self.cancel_requested else ("success" if process.returncode == 0 else "failed")
                self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
                self.process = None
        except Exception as exc:
            logger.exception("同步任务启动失败: %s", exc)
            self.add_line("PROGRESS:error:0:同步任务启动失败，详见 dashboard_server.log")
            with self.lock:
                self.status = "failed"
                self.exit_code = -1
                self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
                self.process = None

    def cancel(self) -> Dict[str, Any]:
        with self.lock:
            process = self.process
            if not process or process.poll() is not None:
                return {"ok": False, "message": "当前没有正在运行的任务"}
            self.cancel_requested = True
            self.status = "cancelling"
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            return {"ok": True, "message": "已发送停止请求"}

    def snapshot(self, after: int = 0) -> Dict[str, Any]:
        with self.lock:
            return {
                "job_id": self.job_id, "status": self.status, "mode": self.mode,
                "running": self.status in {"starting", "running", "cancelling"},
                "exit_code": self.exit_code, "started_at": self.started_at,
                "finished_at": self.finished_at, "cursor": self.seq,
                "lines": [{"seq": seq, "text": line} for seq, line in self.lines if seq > after],
            }


jobs = JobManager()


@app.route("/")
def index():
    return render_template("index.html", csrf_token=CSRF_TOKEN)


@app.route("/api/workspace")
def workspace_api():
    try:
        return jsonify({"ok": True, "config": public_config(), "feishu": feishu_public_config(), "state": load_state().get("jobs", {})})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "读取同步配置失败")}), 500


@app.route("/api/feishu", methods=["POST"])
def save_feishu_api():
    try:
        save_feishu_config(request.get_json(silent=True) or {})
        return jsonify({"ok": True, "message": "飞书配置已保存；留空的 Secret 保持不变", "feishu": feishu_public_config()})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "保存飞书配置失败")}), 400


@app.route("/api/feishu/test", methods=["POST"])
def test_feishu_api():
    try:
        save_feishu_config(request.get_json(silent=True) or {})
        app_token = resolve_app_token()
        tables = feishu_client().list_tables(app_token)
        return jsonify({"ok": True, "message": f"连接成功，当前多维表格包含 {len(tables)} 个数据表", "tables": [{"id": item.get("table_id"), "name": item.get("name")} for item in tables]})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "飞书连接失败")}), 400


@app.route("/api/sources", methods=["POST"])
def save_source_api():
    try:
        payload = request.get_json(silent=True) or {}
        source_data = payload.get("source") or {}
        config = load_config()
        source_id = str(source_data.get("id") or new_id("source"))
        existing = next((item for item in config["sources"] if item["id"] == source_id), None)
        source_data["id"] = source_id
        source_data["password_env"] = existing["password_env"] if existing else password_env_key(source_id)
        source = validate_source(source_data)
        password = str(payload.get("password") or "")
        if password and password not in SECRET_MASKS:
            update_env({source["password_env"]: password})
        if existing:
            config["sources"] = [source if item["id"] == source_id else item for item in config["sources"]]
        else:
            config["sources"].append(source)
        with config_lock:
            saved = save_config(config)
        return jsonify({"ok": True, "message": "数据源已保存", "config": public_config(saved), "source_id": source_id})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "保存数据源失败")}), 400


@app.route("/api/sources/<source_id>", methods=["DELETE"])
def delete_source_api(source_id: str):
    try:
        config = load_config()
        if any(job["source_id"] == source_id for job in config["jobs"]):
            raise ValueError("请先删除该数据源关联的同步任务")
        config["sources"] = [item for item in config["sources"] if item["id"] != source_id]
        with config_lock:
            saved = save_config(config)
        return jsonify({"ok": True, "message": "数据源已删除", "config": public_config(saved)})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "删除数据源失败")}), 400


@app.route("/api/sources/<source_id>/test", methods=["POST"])
def test_source_api(source_id: str):
    try:
        source = get_source(load_config(), source_id)
        with sql_connection(source) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT DB_NAME(), @@SERVERNAME")
                database, server = cursor.fetchone()
        return jsonify({"ok": True, "message": f"连接成功：{server or source['server']} / {database}"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "SQL Server 连接失败")}), 400


@app.route("/api/sources/<source_id>/databases")
def source_databases_api(source_id: str):
    try:
        return jsonify({"ok": True, "databases": list_databases(get_source(load_config(), source_id))})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "读取数据库列表失败")}), 400


@app.route("/api/sources/<source_id>/tables")
def source_tables_api(source_id: str):
    try:
        return jsonify({"ok": True, "tables": list_tables(get_source(load_config(), source_id))})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "读取数据表失败")}), 400


@app.route("/api/sources/<source_id>/columns")
def source_columns_api(source_id: str):
    try:
        schema = request.args.get("schema", "dbo")
        table = request.args.get("table", "")
        if not table:
            raise ValueError("缺少数据表名")
        return jsonify({"ok": True, "columns": list_columns(get_source(load_config(), source_id), schema, table)})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "读取字段失败")}), 400


@app.route("/api/jobs", methods=["POST"])
def save_jobs_api():
    try:
        raw_jobs = (request.get_json(silent=True) or {}).get("jobs")
        if not isinstance(raw_jobs, list):
            raise ValueError("任务列表格式错误")
        config = load_config()
        validated = validate_config({**config, "jobs": raw_jobs})
        for job in validated["jobs"]:
            source = get_source(validated, job["source_id"])
            metadata = {item["name"] for item in list_columns(source, job["schema"], job["table"])}
            missing = [item["source"] for item in job["columns"] if item["source"] not in metadata]
            if missing:
                raise ValueError(f"任务 {job['name']} 中字段已不存在: {', '.join(missing)}")
        with config_lock:
            saved = save_config(validated)
        return jsonify({"ok": True, "message": f"已保存 {len(saved['jobs'])} 个同步任务", "config": public_config(saved)})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "保存同步任务失败")}), 400


@app.route("/api/jobs/<job_id>/state", methods=["DELETE"])
def reset_job_state_api(job_id: str):
    try:
        with state_lock:
            state = load_state()
            state["jobs"].pop(job_id, None)
            save_state(state)
        return jsonify({"ok": True, "message": "增量游标已重置，下次将执行全量扫描"})
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "重置增量状态失败")}), 400


@app.route("/api/run", methods=["POST"])
def run_api():
    try:
        payload = request.get_json(silent=True) or {}
        config = load_config()
        requested_job = str(payload.get("job_id") or "")
        enabled = [job for job in config["jobs"] if job.get("enabled") and (not requested_job or job["id"] == requested_job)]
        if not enabled:
            raise ValueError("没有可运行的同步任务")
        resolve_app_token()
        result = jobs.start(bool(payload.get("dry_run")), requested_job)
        return jsonify(result), 200 if result.get("ok") else 409
    except Exception as exc:
        return jsonify({"ok": False, "message": safe_error(exc, "启动同步失败")}), 400


@app.route("/api/cancel", methods=["POST"])
def cancel_api():
    result = jobs.cancel()
    return jsonify(result), 200 if result.get("ok") else 409


@app.route("/api/status")
def status_api():
    return jsonify(jobs.snapshot(max(0, request.args.get("after", 0, type=int))))


@app.route("/api/logs")
def logs_api():
    n = min(1000, max(1, request.args.get("n", 200, type=int)))
    if not LOG_PATH.exists():
        return jsonify({"log": ""})
    with LOG_PATH.open("r", encoding="utf-8", errors="replace") as file:
        lines = deque(file, maxlen=n)
    return jsonify({"log": "".join(lines)})


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", "5001"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
