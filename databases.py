"""Database-specific connections, metadata and SELECT syntax for the web sync engine."""

import contextlib
import json
import re
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone


DATABASE_TYPES = {"mssql", "mysql", "mariadb", "postgresql", "sqlite"}
DEFAULT_PORTS = {"mssql": 1433, "mysql": 3306, "mariadb": 3306, "postgresql": 5432, "sqlite": 0}
DEFAULT_DATABASES = {"mssql": "master", "mysql": "", "mariadb": "", "postgresql": "postgres", "sqlite": ""}


def database_type(source):
    kind = str(source.get("type") or "mssql").lower()
    if kind not in DATABASE_TYPES:
        raise ValueError(f"不支持的数据库类型: {kind}")
    return kind


def default_schema(source):
    return {"mssql": "dbo", "postgresql": "public", "sqlite": "main"}.get(database_type(source), source.get("database", ""))


def quote_identifier(source, value):
    from sync import quote_identifier as mssql_quote

    kind = database_type(source)
    if kind == "mssql":
        return mssql_quote(value)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("数据库标识符不能为空或包含空字符")
    quote = "`" if kind in {"mysql", "mariadb"} else '"'
    quoted = quote + value.replace(quote, quote * 2) + quote
    # DB-API drivers interpolate percent placeholders even inside identifiers.
    return quoted if kind == "sqlite" else quoted.replace("%", "%%")


def qualified_table(source, schema, table):
    return f"{quote_identifier(source, schema)}.{quote_identifier(source, table)}"


def placeholder(source):
    return "?" if database_type(source) == "sqlite" else "%s"


@contextlib.contextmanager
def connect(source, database=None, settings=None):
    from workspace import runtime_settings, source_password

    settings = settings or runtime_settings()
    kind = database_type(source)
    database = source["database"] if database is None else database
    timeout = int(settings["query_timeout"])
    if kind == "sqlite":
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise ValueError("SQLite 数据库文件不存在，请填写运行同步程序的电脑上的文件绝对路径")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=timeout)
        deadline = time.monotonic() + timeout
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    elif kind in {"mysql", "mariadb"}:
        import pymysql

        conn = pymysql.connect(
            host=source["server"], port=source["port"], user=source["user"],
            password=source_password(source), database=database or None, charset="utf8mb4",
            connect_timeout=10, read_timeout=timeout, write_timeout=timeout,
            cursorclass=pymysql.cursors.SSCursor,
        )
    elif kind == "postgresql":
        import psycopg

        conn = psycopg.connect(
            host=source["server"], port=source["port"], dbname=database,
            user=source["user"], password=source_password(source), connect_timeout=10,
            client_encoding="UTF8", options=f"-c statement_timeout={timeout * 1000} -c default_transaction_read_only=on",
        )
    else:
        raise ValueError("SQL Server 连接应通过 sql_connection 创建")
    try:
        if kind in {"mysql", "mariadb"}:
            with conn.cursor() as cursor:
                cursor.execute("SET SESSION time_zone = '+00:00'")
                cursor.execute("START TRANSACTION READ ONLY")
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def cursor_for(conn, source, streaming=False):
    if database_type(source) == "mssql":
        with conn.cursor() as cursor:
            yield cursor
        return
    if streaming and database_type(source) == "postgresql":
        cursor = conn.cursor(name="sqlfeishu_read")
    else:
        cursor = conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


def list_databases(source):
    kind = database_type(source)
    if kind == "sqlite":
        with connect(source):
            return [source["database"]]
    query = "SHOW DATABASES"
    if kind == "postgresql":
        query = "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND has_database_privilege(datname, 'CONNECT') ORDER BY datname"
    with connect(source) as conn, cursor_for(conn, source) as cursor:
        cursor.execute(query)
        names = [row[0] for row in cursor.fetchall()]
    if kind in {"mysql", "mariadb"}:
        names = [name for name in names if name not in {"information_schema", "mysql", "performance_schema", "sys"}]
    return names


def list_tables(source):
    kind = database_type(source)
    params = ()
    if kind == "sqlite":
        query = "SELECT 'main', name, type FROM sqlite_schema WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    elif kind in {"mysql", "mariadb"}:
        if not source["database"]:
            raise ValueError("请先选择业务数据库并保存数据源，再读取数据表")
        query = "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM information_schema.tables WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME"
        params = (source["database"],)
    else:
        query = ("SELECT table_schema, table_name, table_type FROM information_schema.tables "
                 "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY table_schema, table_name")
    with connect(source) as conn, cursor_for(conn, source) as cursor:
        cursor.execute(query, params)
        return [{"schema": row[0], "name": row[1], "type": "view" if row[2].lower() == "view" else "table"}
                for row in cursor.fetchall()]


def normalize_type(kind, native_type):
    value = re.sub(r"\([^)]*\)", "", native_type.lower()).strip()
    if kind == "postgresql" and value in {"bit", "bit varying", "money"}:
        # Bit strings and locale-formatted money are returned as text by psycopg.
        return "text"
    aliases = {
        "integer": "int", "mediumint": "int", "int2": "smallint", "int4": "int", "int8": "bigint",
        "double": "float", "double precision": "float", "float4": "real", "float8": "float",
        "boolean": "bit", "bool": "bit", "character varying": "varchar", "character": "char",
        "timestamp": "datetime2", "timestamp without time zone": "datetime2",
        "timestamp with time zone": "datetimeoffset", "timestamptz": "datetimeoffset",
        "tinytext": "text", "mediumtext": "text", "longtext": "text",
    }
    if kind in {"mysql", "mariadb"}:
        value = value.replace(" unsigned", "").replace(" zerofill", "")
        # MySQL BIT may contain multiple bits. Preserve the integer value.
        if value == "bit":
            return "bigint"
    return aliases.get(value, value or "text")


def list_columns(source, schema, table):
    kind = database_type(source)
    with connect(source) as conn, cursor_for(conn, source) as cursor:
        if kind == "sqlite":
            if schema != "main":
                raise ValueError("SQLite 数据源仅支持 main schema")
            cursor.execute("SELECT sql FROM sqlite_schema WHERE name = ? AND type = 'table'", (table,))
            definition = cursor.fetchone()
            auto = bool(definition and re.search(r"\bAUTOINCREMENT\b", definition[0] or "", re.I))
            cursor.execute('SELECT name, type, "notnull", cid, pk FROM pragma_table_xinfo(?, ?) WHERE hidden != 1 ORDER BY cid', (table, schema))
            rows = [(row[0], row[1], not bool(row[2] or row[4]), row[3] + 1,
                     bool(auto and row[4] and row[1].upper() == "INTEGER"), 1) for row in cursor.fetchall()]
        elif kind in {"mysql", "mariadb"}:
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION, EXTRA, @@auto_increment_increment "
                "FROM information_schema.columns WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (schema, table),
            )
            rows = [(r[0], r[1], r[2] == "YES", r[3], "auto_increment" in r[4], r[5]) for r in cursor.fetchall()]
        else:
            cursor.execute(
                "SELECT c.column_name, c.data_type, c.is_nullable, c.ordinal_position, "
                "pg_get_serial_sequence(format('%%I.%%I', c.table_schema, c.table_name), c.column_name), "
                "s.seqincrement FROM information_schema.columns c LEFT JOIN pg_sequence s "
                "ON s.seqrelid = to_regclass(pg_get_serial_sequence(format('%%I.%%I', c.table_schema, c.table_name), c.column_name)) "
                "WHERE c.table_schema = %s AND c.table_name = %s ORDER BY c.ordinal_position", (schema, table),
            )
            rows = [(r[0], r[1], r[2] == "YES", r[3], bool(r[4]), r[5] or 0) for r in cursor.fetchall()]
    return [{"name": r[0], "sql_type": normalize_type(kind, r[1]), "native_type": r[1].lower(),
             "nullable": r[2], "ordinal": int(r[3]), "is_identity": bool(r[4]),
             "identity_increment": float(r[5] or 0)} for r in rows]


def list_primary_key(source, schema, table):
    with connect(source) as conn, cursor_for(conn, source) as cursor:
        if database_type(source) == "sqlite":
            cursor.execute("SELECT name FROM pragma_table_xinfo(?, ?) WHERE pk > 0 ORDER BY pk", (table, schema))
        elif database_type(source) in {"mysql", "mariadb"}:
            cursor.execute("SHOW INDEX FROM " + qualified_table(source, schema, table), ())
            # TABLE_CONSTRAINTS can hide keys from SELECT-only MariaDB accounts.
            primary = [row for row in cursor.fetchall() if row[2] == "PRIMARY"]
            return [row[4] for row in sorted(primary, key=lambda row: row[3])]
        else:
            cursor.execute(
                "SELECT a.attname FROM pg_index i "
                "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, position) "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                "WHERE i.indrelid = to_regclass(format('%%I.%%I', %s::text, %s::text)) AND i.indisprimary "
                "AND k.position <= i.indnkeyatts ORDER BY k.position", (schema, table),
            )
        return [row[0] for row in cursor.fetchall()]


def select_query(source, schema, table, expressions, limit=None):
    top = f"TOP ({int(limit)}) " if limit and database_type(source) == "mssql" else ""
    query = f"SELECT {top}{', '.join(expressions)} FROM {qualified_table(source, schema, table)}"
    if limit and database_type(source) != "mssql":
        query += f" LIMIT {int(limit)}"
    return query


def prepare_read(cursor, source):
    if database_type(source) == "mssql":
        cursor.execute("SET TEXTSIZE 2147483647")


def decode_value(value, column, source):
    kind = database_type(source)
    if kind in {"mysql", "mariadb"} and column.get("native_type") == "timestamp" and isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc)
    if kind in {"mysql", "mariadb"} and column.get("native_type") == "bit" and isinstance(value, bytes):
        return int.from_bytes(value, "big")
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def cursor_parameter(value, column, source):
    if database_type(source) in {"mysql", "mariadb"} and column.get("native_type") == "timestamp" and isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
