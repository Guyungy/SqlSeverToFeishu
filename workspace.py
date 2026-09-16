#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Configuration and state storage for multi-source synchronization."""

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import dotenv_values, load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
CONFIG_PATH = BASE_DIR / "sync_config.json"
STATE_PATH = BASE_DIR / "sync_state.json"


def atomic_json_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _quote_env(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def update_env(updates: Dict[str, Any]) -> None:
    current = {key: str(value or "") for key, value in dotenv_values(ENV_PATH).items()}
    for key, value in updates.items():
        current[key] = str(value or "")
    fd, temp_name = tempfile.mkstemp(prefix=".env.", dir=str(BASE_DIR), text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            for key in sorted(current):
                file.write(f"{key}={_quote_env(current[key])}\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, ENV_PATH)
        try:
            ENV_PATH.chmod(0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    load_dotenv(ENV_PATH, override=True)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def password_env_key(source_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "_", source_id).upper()
    return f"SQL_SOURCE_{safe}_PASSWORD"


def empty_config() -> Dict[str, Any]:
    return {"version": 2, "sources": [], "jobs": []}


def _legacy_config() -> Dict[str, Any]:
    server = (os.environ.get("DB_SERVER") or "").strip()
    database = (os.environ.get("DB_NAME") or "").strip()
    placeholders = {"your_database_name", "your_server", "example", "changeme"}
    if not server or not database or database.lower() in placeholders:
        return empty_config()
    source_id = "source_legacy"
    source = {
        "id": source_id,
        "name": "原数据库",
        "server": os.environ.get("DB_SERVER", ""),
        "port": int(os.environ.get("DB_PORT", "1433")),
        "database": os.environ.get("DB_NAME", ""),
        "user": os.environ.get("DB_USER", ""),
        "password_env": "DB_PASSWORD",
        "enabled": True,
    }
    jobs: List[Dict[str, Any]] = []
    mapping_path = BASE_DIR / "mapping.json"
    table_name = (os.environ.get("DB_TABLE") or "").strip()
    if mapping_path.exists() and table_name:
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            parts = table_name.split(".", 1)
            schema, table = (parts[0], parts[1]) if len(parts) == 2 else ("dbo", parts[0])
            selected = [
                {"source": item["sql"], "target": item.get("feishu") or item["sql"], "sql_type": "nvarchar"}
                for item in mapping.get("mapping", [])
                if item.get("sql")
            ]
            if selected and mapping.get("unique_key"):
                jobs.append({
                    "id": "job_legacy",
                    "name": f"{source['name']} / {schema}.{table}",
                    "source_id": source_id,
                    "schema": schema,
                    "table": table,
                    "enabled": True,
                    "columns": selected,
                    "unique_key": mapping["unique_key"],
                    "incremental": {"enabled": False, "column": ""},
                    "target": {
                        "mode": "existing" if os.environ.get("FEISHU_BASE_TABLE_ID") else "auto",
                        "table_id": os.environ.get("FEISHU_BASE_TABLE_ID", ""),
                        "table_name": table,
                        "auto_create_fields": True,
                    },
                })
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return {"version": 2, "sources": [source], "jobs": jobs}


def load_config() -> Dict[str, Any]:
    load_dotenv(ENV_PATH, override=True)
    if not CONFIG_PATH.exists():
        # Reads must not race a worker by persisting a legacy snapshot.
        # The first explicit save performs the migration under the write lock.
        return validate_config(_legacy_config())
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("sync_config.json 无法读取，请从备份恢复") from exc
    return validate_config(data)


def _clean_text(value: Any, label: str, max_length: int = 128) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label}不能为空")
    if len(text) > max_length:
        raise ValueError(f"{label}过长")
    return text


def _clean_sql_identifier(value: Any, label: str) -> str:
    # Reuse the same rules as query generation; importing here avoids
    # introducing configuration loading at module initialization time.
    from sync import ConfigError, _clean_identifier_part

    try:
        return _clean_identifier_part(_clean_text(value, label))
    except ConfigError as exc:
        raise ValueError(f"{label}: {exc}") from exc


def validate_source(source: Dict[str, Any]) -> Dict[str, Any]:
    source_id = _clean_text(source.get("id") or new_id("source"), "数据源 ID", 64)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
        raise ValueError("数据源 ID 格式不正确")
    port = int(source.get("port") or 1433)
    if not 1 <= port <= 65535:
        raise ValueError("SQL Server 端口必须在 1-65535 之间")
    password_key = str(source.get("password_env") or password_env_key(source_id))
    return {
        "id": source_id,
        "name": _clean_text(source.get("name"), "数据源名称"),
        "server": _clean_text(source.get("server"), "SQL Server 地址", 255),
        "port": port,
        "database": _clean_text(source.get("database") or "master", "数据库名"),
        "user": str(source.get("user") or "").strip(),
        "password_env": password_key,
        "enabled": bool(source.get("enabled", True)),
    }


def validate_job(job: Dict[str, Any], source_ids: Iterable[str]) -> Dict[str, Any]:
    job_id = _clean_text(job.get("id") or new_id("job"), "同步任务 ID", 64)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise ValueError("同步任务 ID 格式不正确")
    source_id = _clean_text(job.get("source_id"), "数据源")
    if source_id not in set(source_ids):
        raise ValueError("同步任务引用了不存在的数据源")
    columns = job.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError("每个同步任务至少勾选一个字段")
    clean_columns = []
    source_names = set()
    target_names = set()
    for item in columns:
        if not isinstance(item, dict):
            raise ValueError("字段配置必须是对象")
        source_name = _clean_sql_identifier(item.get("source"), "SQL 字段")
        target_name = _clean_text(item.get("target") or source_name, "飞书字段")
        if source_name in source_names:
            raise ValueError(f"SQL 字段重复: {source_name}")
        if target_name in target_names:
            raise ValueError(f"飞书字段名重复: {target_name}")
        source_names.add(source_name)
        target_names.add(target_name)
        clean_columns.append({
            "source": source_name,
            "target": target_name,
            "sql_type": str(item.get("sql_type") or "nvarchar").lower(),
            "nullable": bool(item.get("nullable", True)),
        })
    unique_key = _clean_text(job.get("unique_key"), "唯一键")
    if unique_key not in source_names:
        raise ValueError("唯一键必须包含在已勾选字段中")
    incremental = job.get("incremental") or {}
    incremental_enabled = bool(incremental.get("enabled"))
    incremental_column = str(incremental.get("column") or "").strip()
    if incremental_enabled and incremental_column not in source_names:
        raise ValueError("增量字段必须包含在已勾选字段中")
    target = job.get("target") or {}
    target_mode = target.get("mode") or "auto"
    if target_mode not in {"auto", "existing"}:
        raise ValueError("飞书目标模式仅支持 auto 或 existing")
    table_id = str(target.get("table_id") or "").strip()
    if target_mode == "existing" and not table_id:
        raise ValueError("选择已有飞书表时必须指定 Table ID")
    return {
        "id": job_id,
        "name": str(job.get("name") or f"{job.get('schema', 'dbo')}.{job.get('table', '')}").strip(),
        "source_id": source_id,
        "schema": _clean_sql_identifier(job.get("schema") or "dbo", "Schema"),
        "table": _clean_sql_identifier(job.get("table"), "数据表"),
        "enabled": bool(job.get("enabled", True)),
        "columns": clean_columns,
        "unique_key": unique_key,
        "incremental": {"enabled": incremental_enabled, "column": incremental_column},
        "target": {
            "mode": target_mode,
            "table_id": table_id,
            "table_name": _clean_text(target.get("table_name") or job.get("table"), "飞书表名", 100),
            "auto_create_fields": bool(target.get("auto_create_fields", True)),
        },
    }


def validate_config(data: Dict[str, Any]) -> Dict[str, Any]:
    sources_raw = data.get("sources") or []
    jobs_raw = data.get("jobs") or []
    if not isinstance(sources_raw, list) or not isinstance(jobs_raw, list):
        raise ValueError("同步配置格式错误")
    sources = [validate_source(item) for item in sources_raw]
    source_ids = [item["id"] for item in sources]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("数据源 ID 重复")
    jobs = [validate_job(item, source_ids) for item in jobs_raw]
    job_ids = [item["id"] for item in jobs]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError("同步任务 ID 重复")
    return {"version": 2, "sources": sources, "jobs": jobs}


def save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    validated = validate_config(data)
    atomic_json_write(CONFIG_PATH, validated)
    return validated


def public_config(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = data or load_config()
    result = json.loads(json.dumps(config, ensure_ascii=False))
    for source in result["sources"]:
        source["has_password"] = bool(os.environ.get(source["password_env"]))
        source.pop("password_env", None)
    return result


def get_source(config: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    source = next((item for item in config["sources"] if item["id"] == source_id), None)
    if not source:
        raise ValueError("数据源不存在")
    return source


def source_password(source: Dict[str, Any]) -> str:
    return os.environ.get(source["password_env"], "")


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": 1, "jobs": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("jobs"), dict):
            raise ValueError
        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("sync_state.json 已损坏，请备份后重置增量状态") from exc


def save_state(data: Dict[str, Any]) -> None:
    atomic_json_write(STATE_PATH, data)
