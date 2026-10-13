//! SQL Server 访问层（基于 tiberius-ng，纯 Rust TDS 实现，不需要 ODBC / FreeTDS）。
//!
//! 本轮只实现连通性与元数据探测。行值到飞书字段的转换在下一步补齐，
//! 因为那部分要按列类型逐个匹配，先用 CI 把编译链路打通更稳妥。

use std::future::Future;
use std::time::Duration;

use anyhow::{Context, Result};
use tiberius::{AuthMethod, Client, Config, ToSql};
use tokio::net::TcpStream;
use tokio_util::compat::{Compat, TokioAsyncWriteCompatExt};

use crate::config::{env_or_map, DataSource, EnvMap};

pub type SqlClient = Client<Compat<TcpStream>>;

/// 建立 TCP / 完成 TDS 登录的总时限。
///
/// 单独计时而不是沿用 `query_timeout`：连接阶段卡住通常是网络不通或
/// 防火墙丢包，等满 60 秒没有意义。命令行与桌面版都走这里的默认值。
pub const CONNECT_TIMEOUT_SECS: u64 = 15;

/// 给一段查询工作套上超时，超时时给出可读的中文错误。
///
/// `what` 用于拼出「查询表列表超时」这类可定位的提示。参数直接接一个已经
/// 创建的 future，而不是闭包工厂：闭包返回的 async 块要借用 `&mut client`，
/// 写成 `FnOnce() -> Fut` 会因生命周期无法表达而编译失败。
async fn within_timeout<T>(
    what: &str,
    timeout_secs: u64,
    work: impl Future<Output = Result<T>>,
) -> Result<T> {
    match tokio::time::timeout(Duration::from_secs(timeout_secs), work).await {
        Ok(result) => result,
        Err(_) => Err(anyhow::anyhow!(
            "{what}超时（超过 {timeout_secs} 秒，可在运行设置里调整查询超时）"
        )),
    }
}

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
/// 调用方可以传 `false` 走严格校验，`tests/mssql_tls.rs` 会分别验证两条路径。
pub async fn connect_with(source: &DataSource, trust_cert: bool) -> Result<SqlClient> {
    connect_inner(source, source.password(), trust_cert).await
}

/// 桌面版入口：密码与证书策略从 `.env` 解析出的映射读取，不依赖进程环境变量。
///
/// 这样 `reload_env` 那类「运行期回写进程环境」的 UB 路径可以被彻底删掉：
/// 界面每次操作前重新读一遍 `.env` 文件，改完设置无需重启即可生效。
pub async fn connect_from_env(source: &DataSource, env: &EnvMap) -> Result<SqlClient> {
    connect_inner(source, source.password_with(env), trust_cert_from(env)).await
}

/// 从 `.env` 映射解析是否校验证书。
///
/// 缺省 `true`（信任自签证书，与既有行为一致）；只有显式写入
/// `DB_TRUST_CERT=false` / `0` / `no` / `off` 时才开启严格校验。
pub fn trust_cert_from(env: &EnvMap) -> bool {
    let value = env_or_map(env, "DB_TRUST_CERT");
    !matches!(
        value.to_ascii_lowercase().as_str(),
        "false" | "0" | "no" | "off"
    )
}

async fn connect_inner(
    source: &DataSource,
    password: String,
    trust_cert: bool,
) -> Result<SqlClient> {
    let mut config = Config::new();
    // tiberius 的 Config::host / Config::database 接受 `Cow<'static, str>`，
    // 直接传 &str 会因生命周期不足而编译失败，这里显式传 owned String。
    config.host(source.server.clone());
    config.port(source.port);
    if !source.database.is_empty() {
        config.database(source.database.clone());
    }
    config.authentication(AuthMethod::sql_server(source.user.clone(), password));
    if trust_cert {
        config.trust_cert();
    }

    // TCP 连接与 TDS 登录都套上时限：没有超时的话，地址写错或目标静默丢包时
    // 这里会一直挂着，桌面版表现为界面卡死、命令行表现为毫无输出。
    let addr = config.get_addr();
    let connect_label = format!(
        "连接 SQL Server {}:{}（请检查地址、端口、TCP/IP 与防火墙）",
        source.server, source.port
    );
    let tcp = within_timeout(
        &format!("连接 SQL Server {}:{}", source.server, source.port),
        CONNECT_TIMEOUT_SECS,
        async {
            TcpStream::connect(addr)
                .await
                .with_context(|| connect_label.clone())
        },
    )
    .await?;
    tcp.set_nodelay(true)?;

    let login_label = format!(
        "SQL Server 登录失败（用户 {}, 数据库 {}）",
        source.user, source.database
    );
    within_timeout(
        &format!("登录 SQL Server（用户 {}）", source.user),
        CONNECT_TIMEOUT_SECS,
        async {
            Client::connect(config, tcp.compat_write())
                .await
                .with_context(|| login_label.clone())
        },
    )
    .await
}

/// 轻量连通性检查。`timeout_secs` 取自运行设置的 `query_timeout`。
pub async fn ping(client: &mut SqlClient, timeout_secs: u64) -> Result<()> {
    let stream = within_timeout("连通性查询", timeout_secs, async {
        client
            .simple_query("SELECT 1")
            .await
            .context("连通性查询失败")
    })
    .await?;
    let rows = within_timeout("读取连通性结果", timeout_secs, async {
        stream.into_first_result().await.context("读取查询结果失败")
    })
    .await?;
    if rows.is_empty() {
        return Err(anyhow::anyhow!("SQL Server 未返回结果"));
    }
    Ok(())
}

/// 列出当前账号可访问的数据库。
pub async fn list_databases(client: &mut SqlClient, timeout_secs: u64) -> Result<Vec<String>> {
    let stream = within_timeout("查询数据库列表", timeout_secs, async {
        client
            .simple_query("SELECT name FROM sys.databases ORDER BY name")
            .await
            .context("查询数据库列表失败")
    })
    .await?;
    let rows = within_timeout("读取数据库列表", timeout_secs, async {
        stream.into_first_result().await.context("读取查询结果失败")
    })
    .await?;
    Ok(rows
        .iter()
        .filter_map(|row| row.get::<&str, _>(0).map(str::to_string))
        .collect())
}

/// 列出库内的表与视图。
///
/// 只看 INFORMATION_SCHEMA.TABLES 的 BASE TABLE / VIEW；其余类型（如系统表）
/// 不暴露给界面，避免用户选到无法同步的对象。
pub async fn list_tables(client: &mut SqlClient, timeout_secs: u64) -> Result<Vec<TableInfo>> {
    let sql = "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE \
               FROM INFORMATION_SCHEMA.TABLES \
               WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') \
               ORDER BY TABLE_SCHEMA, TABLE_NAME";
    let stream = within_timeout("查询表列表", timeout_secs, async {
        client.simple_query(sql).await.context("查询表列表失败")
    })
    .await?;
    let rows = within_timeout("读取表列表", timeout_secs, async {
        stream.into_first_result().await.context("读取查询结果失败")
    })
    .await?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            let schema = row.get::<&str, _>(0)?.to_string();
            let name = row.get::<&str, _>(1)?.to_string();
            let kind = row.get::<&str, _>(2).unwrap_or("BASE TABLE").to_string();
            Some(TableInfo { schema, name, kind })
        })
        .collect())
}

/// 列出指定表的字段。
pub async fn list_columns(
    client: &mut SqlClient,
    schema: &str,
    table: &str,
    timeout_secs: u64,
) -> Result<Vec<ColumnInfo>> {
    let sql = "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, \
                      CHARACTER_MAXIMUM_LENGTH \
               FROM INFORMATION_SCHEMA.COLUMNS \
               WHERE TABLE_SCHEMA = @P1 AND TABLE_NAME = @P2 \
               ORDER BY ORDINAL_POSITION";
    let stream = within_timeout("查询字段列表", timeout_secs, async {
        client
            .query(sql, &[&schema as &dyn ToSql, &table as &dyn ToSql])
            .await
            .context("查询字段列表失败")
    })
    .await?;
    let rows = within_timeout("读取字段列表", timeout_secs, async {
        stream.into_first_result().await.context("读取查询结果失败")
    })
    .await?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            let name = row.get::<&str, _>(0)?.to_string();
            let data_type = row.get::<&str, _>(1).unwrap_or("nvarchar").to_string();
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
