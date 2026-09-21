//! SQL Server 访问层（基于 tiberius-ng，纯 Rust TDS 实现，不需要 ODBC / FreeTDS）。
//!
//! 本轮只实现连通性与元数据探测。行值到飞书字段的转换在下一步补齐，
//! 因为那部分要按列类型逐个匹配，先用 CI 把编译链路打通更稳妥。

use anyhow::{Context, Result};
use tiberius::{AuthMethod, Client, Config, ToSql};
use tokio::net::TcpStream;
use tokio_util::compat::{Compat, TokioAsyncWriteCompatExt};

use crate::config::DataSource;

pub type SqlClient = Client<Compat<TcpStream>>;

#[derive(Debug, Clone)]
pub struct TableInfo {
    pub schema: String,
    pub name: String,
    pub kind: String,
}

impl TableInfo {
    pub fn qualified(&self) -> String {
        format!("{}.{}", self.schema, self.name)
    }
}

#[derive(Debug, Clone)]
pub struct ColumnInfo {
    pub name: String,
    pub data_type: String,
    pub nullable: bool,
    pub max_length: Option<i64>,
}

/// 建立连接（信任服务器证书，与 Python 版 pymssql 行为一致）。
pub async fn connect(source: &DataSource) -> Result<SqlClient> {
    connect_with(source, true).await
}

/// 建立连接，可显式控制是否校验证书。
///
/// `trust_cert = true` 会跳过证书校验。SQL Server 默认使用自签证书，
/// 且内网部署普遍不做证书替换，所以默认放过——但这是**显式**行为，
/// 调用方可以传 `false` 走严格校验，`tests/tls_handshake.rs` 会分别验证两条路径。
pub async fn connect_with(source: &DataSource, trust_cert: bool) -> Result<SqlClient> {
    let password = source.password();
    let mut config = Config::new();
    // tiberius 的 Config::host / Config::database 接受 `Cow<'static, str>`，
    // 直接传 &str 会因生命周期不足而编译失败，这里显式传 owned String。
    config.host(source.server.clone());
    config.port(source.port);
    if !source.database.is_empty() {
        config.database(source.database.clone());
    }
    config.authentication(AuthMethod::sql_server(
        source.user.clone(),
        password,
    ));
    if trust_cert {
        config.trust_cert();
    }

    let tcp = TcpStream::connect(config.get_addr())
        .await
        .with_context(|| {
            format!(
                "无法连接 SQL Server {}:{}（请检查地址、端口、TCP/IP 与防火墙）",
                source.server, source.port
            )
        })?;
    tcp.set_nodelay(true)?;

    Client::connect(config, tcp.compat_write())
        .await
        .with_context(|| {
            format!(
                "SQL Server 登录失败（用户 {}, 数据库 {}）",
                source.user, source.database
            )
        })
}

/// 轻量连通性检查。
pub async fn ping(client: &mut SqlClient) -> Result<()> {
    let stream = client
        .simple_query("SELECT 1")
        .await
        .context("连通性查询失败")?;
    let rows = stream.into_first_result().await?;
    if rows.is_empty() {
        return Err(anyhow::anyhow!("SQL Server 未返回结果"));
    }
    Ok(())
}

/// 列出当前账号可访问的数据库。
pub async fn list_databases(client: &mut SqlClient) -> Result<Vec<String>> {
    let stream = client
        .simple_query("SELECT name FROM sys.databases ORDER BY name")
        .await
        .context("查询数据库列表失败")?;
    let rows = stream.into_first_result().await?;
    Ok(rows
        .iter()
        .filter_map(|row| row.get::<&str, _>(0).map(str::to_string))
        .collect())
}

/// 列出库内的表与视图。
///
/// 只看 INFORMATION_SCHEMA.TABLES 的 BASE TABLE / VIEW；其余类型（如系统表）
/// 不暴露给界面，避免用户选到无法同步的对象。
pub async fn list_tables(client: &mut SqlClient) -> Result<Vec<TableInfo>> {
    let sql = "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE \
               FROM INFORMATION_SCHEMA.TABLES \
               WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') \
               ORDER BY TABLE_SCHEMA, TABLE_NAME";
    let stream = client.simple_query(sql).await.context("查询表列表失败")?;
    let rows = stream.into_first_result().await?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            let schema = row.get::<&str, _>(0)?.to_string();
            let name = row.get::<&str, _>(1)?.to_string();
            let kind = row
                .get::<&str, _>(2)
                .unwrap_or("BASE TABLE")
                .to_string();
            Some(TableInfo {
                schema,
                name,
                kind,
            })
        })
        .collect())
}

/// 列出指定表的字段。
pub async fn list_columns(
    client: &mut SqlClient,
    schema: &str,
    table: &str,
) -> Result<Vec<ColumnInfo>> {
    let sql = "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, \
                      CHARACTER_MAXIMUM_LENGTH \
               FROM INFORMATION_SCHEMA.COLUMNS \
               WHERE TABLE_SCHEMA = @P1 AND TABLE_NAME = @P2 \
               ORDER BY ORDINAL_POSITION";
    let stream = client
        .query(
            sql,
            &[&schema as &dyn ToSql, &table as &dyn ToSql],
        )
        .await
        .context("查询字段列表失败")?;
    let rows = stream.into_first_result().await?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            let name = row.get::<&str, _>(0)?.to_string();
            let data_type = row
                .get::<&str, _>(1)
                .unwrap_or("nvarchar")
                .to_string();
            let nullable = row
                .get::<&str, _>(2)
                .map(|value| value.eq_ignore_ascii_case("YES"))
                .unwrap_or(true);
            let max_length = row.get::<i32, _>(3).map(i64::from);
            Some(ColumnInfo {
                name,
                data_type,
                nullable,
                max_length,
            })
        })
        .collect())
}
