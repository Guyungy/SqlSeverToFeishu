"""Database-specific connections, metadata and SELECT syntax for the web sync engine."""

import contextlib
import json
import re
import sqlite3
import ssl
import time
from pathlib import Path
from datetime import datetime, timezone

import pymysql


DATABASE_TYPES = {"mssql", "mysql", "mariadb", "postgresql", "sqlite"}
DEFAULT_PORTS = {"mssql": 1433, "mysql": 3306, "mariadb": 3306, "postgresql": 5432, "sqlite": 0}
DEFAULT_DATABASES = {"mssql": "master", "mysql": "", "mariadb": "", "postgresql": "postgres", "sqlite": ""}


def validate_tls(source):
    kind = database_type(source)
    tls = source.get("tls", {})
    if not isinstance(tls, dict):
        raise ValueError("TLS 配置必须是对象")
    mode = tls.get("mode", "default")
    modes = {"default"} if kind == "sqlite" else {"default", "disable", "require"}
    if kind in {"postgresql", "mysql", "mariadb"}:
        modes.add("verify-full")
    if not isinstance(mode, str) or mode not in modes:
        raise ValueError(f"{kind} 不支持此 TLS 模式")
    result = {"mode": mode}
    for key, label in (("ca", "CA 证书"), ("cert", "客户端证书"), ("key", "客户端私钥")):
        value = tls.get(key, "")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"{label}路径格式错误")
        value = value.strip()
        if value:
            if kind in {"mssql", "sqlite"} or mode not in {"require", "verify-full"}:
                raise ValueError(f"当前数据库和 TLS 模式不支持设置{label}")
            if key == "ca" and mode != "verify-full":
                raise ValueError("设置 CA 证书时请选择校验证书和主机名")
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError(f"{label}请填写运行同步程序的电脑上的绝对路径")
            value = str(path)
        result[key] = value
    if bool(result["cert"]) != bool(result["key"]):
        raise ValueError("客户端证书和私钥必须同时填写")
    if kind == "postgresql" and mode in {"require", "verify-full"}:
        if any(host.strip().startswith(("/", "@")) for host in source.get("server", "").split(",")):
            raise ValueError("PostgreSQL TLS 连接请使用 TCP 主机名或 IP 地址")
    return result


def tls_options(source):
    """Build driver options after validation, including files on the sync host."""
    tls = validate_tls(source)
    mode = tls["mode"]
    if mode == "default":
        return {}
    kind = database_type(source)
    if kind == "mssql":
        return {"encryption": "off" if mode == "disable" else "require"}
    for key, label in (("ca", "CA 证书"), ("cert", "客户端证书"), ("key", "客户端私钥")):
        if tls[key] and not Path(tls[key]).is_file():
            raise ValueError(f"{label}文件不存在，请检查同步程序所在电脑上的路径")
    if kind == "postgresql":
        options = {"sslmode": mode, "gssencmode": "disable"}
        if mode == "verify-full":
            options["sslrootcert"] = tls["ca"] or "system"
        if tls["cert"]:
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_cert_chain(tls["cert"], tls["key"], password=lambda: "")
            except (OSError, ValueError) as exc:
                raise ValueError("TLS 客户端证书加载失败，请检查 PEM 格式、文件权限及未加密私钥是否匹配") from exc
            options.update(sslcert=tls["cert"], sslkey=tls["key"])
        return options
    if mode == "disable":
        return {"ssl_disabled": True}
    try:
        if mode == "verify-full":
            context = ssl.create_default_context(cafile=tls["ca"] or None)
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if tls["cert"]:
            # An encrypted key should fail promptly instead of prompting on stdin.
            context.load_cert_chain(tls["cert"], tls["key"], password=lambda: "")
    except (OSError, ValueError) as exc:
        raise ValueError("TLS 证书加载失败，请检查 PEM 格式、文件权限及客户端证书与未加密私钥是否匹配") from exc
    return {"ssl": context}


class TLSMySQLConnection(pymysql.connections.Connection):
    """PyMySQL 1.1.2 otherwise falls back to plaintext when CLIENT.SSL is absent."""

    def _request_authentication(self):
        if not self.server_capabilities & pymysql.constants.CLIENT.SSL:
            raise ValueError("数据库服务器未启用 TLS，已在发送认证信息前停止连接")
        return super()._request_authentication()


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
        options = tls_options(source)
        connector = TLSMySQLConnection if "ssl" in options else pymysql.connect
        conn = connector(
            host=source["server"], port=source["port"], user=source["user"],
            password=source_password(source), database=database or None, charset="utf8mb4",
            connect_timeout=10, read_timeout=timeout, write_timeout=timeout,
            cursorclass=pymysql.cursors.SSCursor,
            **options,
        )
    elif kind == "postgresql":
        import psycopg

        conn = psycopg.connect(
            host=source["server"], port=source["port"], dbname=database,
            user=source["user"], password=source_password(source), connect_timeout=10,
            client_encoding="UTF8", options=f"-c statement_timeout={timeout * 1000} -c default_transaction_read_only=on",
            **tls_options(source),
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
                 "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                 "AND has_schema_privilege(table_schema, 'USAGE') "
                 "AND (has_table_privilege(format('%%I.%%I', table_schema, table_name), 'SELECT') "
                 "OR has_any_column_privilege(format('%%I.%%I', table_schema, table_name), 'SELECT')) "
                 "UNION ALL SELECT n.nspname, c.relname, 'materialized_view' "
                 "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                 "WHERE c.relkind = 'm' AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                 "AND has_schema_privilege(n.oid, 'USAGE') "
                 "AND (has_table_privilege(c.oid, 'SELECT') OR has_any_column_privilege(c.oid, 'SELECT')) "
                 "ORDER BY table_schema, table_name")
    with connect(source) as conn, cursor_for(conn, source) as cursor:
        cursor.execute(query, params)
        return [{"schema": row[0], "name": row[1],
                 "type": {"view": "view", "materialized_view": "materialized_view"}.get(row[2].lower(), "table")}
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


def needs_exact_text(column, source):
    """Recommend lossless text for SQL types whose range exceeds JSON numbers."""
    kind = column["sql_type"]
    if kind == "bigint" or (database_type(source) == "sqlite" and kind == "int"):
        return True
    if kind in {"decimal", "numeric", "money"}:
        precision = column.get("numeric_precision")
        return precision is None or int(precision) > 15
    return False


def list_columns(source, schema, table):
    kind = database_type(source)
    postgres_types = {}
    with connect(source) as conn, cursor_for(conn, source) as cursor:
        if kind == "sqlite":
            if schema != "main":
                raise ValueError("SQLite 数据源仅支持 main schema")
            cursor.execute("SELECT sql FROM sqlite_schema WHERE name = ? AND type = 'table'", (table,))
            definition = cursor.fetchone()
            auto = bool(definition and re.search(r"\bAUTOINCREMENT\b", definition[0] or "", re.I))
            cursor.execute('SELECT name, type, "notnull", cid, pk FROM pragma_table_xinfo(?, ?) WHERE hidden != 1 ORDER BY cid', (table, schema))
            rows = [(row[0], row[1], not bool(row[2] or row[4]), row[3] + 1,
                     bool(auto and row[4] and row[1].upper() == "INTEGER"), 1, None) for row in cursor.fetchall()]
        elif kind in {"mysql", "mariadb"}:
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION, EXTRA, @@auto_increment_increment, NUMERIC_PRECISION "
                "FROM information_schema.columns WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (schema, table),
            )
            rows = [(r[0], r[1], r[2] == "YES", r[3], "auto_increment" in r[4], r[5], r[6]) for r in cursor.fetchall()]
        else:
            cursor.execute(
                "SELECT c.column_name, c.data_type, c.is_nullable, c.ordinal_position, "
                "pg_get_serial_sequence(format('%%I.%%I', c.table_schema, c.table_name), c.column_name), "
                "s.seqincrement, c.numeric_precision FROM information_schema.columns c LEFT JOIN pg_sequence s "
                "ON s.seqrelid = to_regclass(pg_get_serial_sequence(format('%%I.%%I', c.table_schema, c.table_name), c.column_name)) "
                "WHERE c.table_schema = %s AND c.table_name = %s "
                "AND has_schema_privilege(c.table_schema, 'USAGE') "
                "AND has_column_privilege(format('%%I.%%I', c.table_schema, c.table_name), c.column_name, 'SELECT') "
                "ORDER BY c.ordinal_position", (schema, table),
            )
            rows = [(r[0], r[1], r[2] == "YES", r[3], bool(r[4]), r[5] or 0, r[6]) for r in cursor.fetchall()]
            if not rows:
                # information_schema.columns omits PostgreSQL materialized views.
                cursor.execute(
                    "SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.attnum, "
                    "false, 0, information_schema._pg_numeric_precision(a.atttypid, a.atttypmod) "
                    "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'm' "
                    "AND a.attnum > 0 AND NOT a.attisdropped AND has_schema_privilege(n.oid, 'USAGE') "
                    "AND (has_table_privilege(c.oid, 'SELECT') OR has_column_privilege(c.oid, a.attnum, 'SELECT')) "
                    "ORDER BY a.attnum", (schema, table),
                )
                rows = cursor.fetchall()
            if rows:
                cursor.execute(
                    "WITH RECURSIVE domain_columns AS ("
                    "SELECT a.attname, a.atttypid AS type_id, a.atttypmod AS type_mod, "
                    "a.attnotnull AS not_null, "
                    "CASE WHEN t.typtype = 'd' THEN format_type(a.atttypid, NULL) END AS domain_name "
                    "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_type t ON t.oid = a.atttypid "
                    "WHERE n.nspname = %s AND c.relname = %s "
                    "AND a.attnum > 0 AND NOT a.attisdropped AND has_schema_privilege(n.oid, 'USAGE') "
                    "AND has_column_privilege(c.oid, a.attnum, 'SELECT') "
                    "UNION ALL SELECT d.attname, t.typbasetype, "
                    "CASE WHEN d.type_mod >= 0 THEN d.type_mod ELSE t.typtypmod END, "
                    "d.not_null OR t.typnotnull, d.domain_name "
                    "FROM domain_columns d JOIN pg_type t ON t.oid = d.type_id WHERE t.typtype = 'd') "
                    "SELECT d.attname, format_type(d.type_id, d.type_mod), NOT d.not_null, "
                    "information_schema._pg_numeric_precision(d.type_id, d.type_mod), d.domain_name, "
                    "t.typelem = 'bytea'::regtype AS binary_array "
                    "FROM domain_columns d JOIN pg_type t ON t.oid = d.type_id WHERE t.typtype <> 'd'",
                    (schema, table),
                )
                postgres_types = {row[0]: row for row in cursor.fetchall()}
    columns = [{"name": r[0], "sql_type": normalize_type(kind, r[1]), "native_type": r[1].lower(),
             "nullable": r[2], "ordinal": int(r[3]), "is_identity": bool(r[4]),
             "identity_increment": float(r[5] or 0), "numeric_precision": r[6]} for r in rows]
    for column in columns:
        resolved = postgres_types.get(column["name"])
        if resolved and resolved[4]:
            column.update(legacy_sql_type=column["sql_type"], domain_type=resolved[4],
                          sql_type=normalize_type(kind, resolved[1]), nullable=resolved[2], numeric_precision=resolved[3])
        if resolved and resolved[5]:
            column["binary_array"] = True
    return columns


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
                "SELECT a.attname, has_schema_privilege(n.oid, 'USAGE') "
                "AND has_column_privilege(c.oid, a.attnum, 'SELECT') FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, position) "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                "WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary "
                "AND k.position <= i.indnkeyatts ORDER BY k.position", (schema, table),
            )
            rows = cursor.fetchall()
            # A subset of a composite key is not necessarily unique. Use content
            # identity unless the reader can access every primary-key column.
            return [row[0] for row in rows] if all(row[1] for row in rows) else []
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
        if column.get("binary_array"):
            return json.dumps(value, ensure_ascii=False, default=lambda item: bytes(item).hex())
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def legacy_binary_array(value):
    """Restore the former driver's byte values from a decoded hex JSON array."""
    if isinstance(value, list):
        return [legacy_binary_array(item) for item in value]
    return bytes.fromhex(value) if value is not None else None


def cursor_parameter(value, column, source):
    if database_type(source) in {"mysql", "mariadb"} and column.get("native_type") == "timestamp" and isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
