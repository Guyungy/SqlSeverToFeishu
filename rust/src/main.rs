//! 命令行入口。
//!
//! 本轮是可行性验证（POC）：确认 tiberius-ng 能连上 SQL Server、
//! 飞书客户端能取到令牌并读出多维表格元数据。同步主逻辑与网页面板在下一步补齐。

use anyhow::Result;

use sqlserver_to_feishu::config::{self, AppPaths};
use sqlserver_to_feishu::{feishu, mssql};

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("错误: {error:#}");
        std::process::exit(1);
    }
}

async fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let command = args.first().map(String::as_str).unwrap_or("help");

    let paths = AppPaths::discover()?;
    paths.load_env()?;

    match command {
        "help" | "-h" | "--help" => {
            print_help();
            Ok(())
        }
        "where" => {
            println!("配置目录: {}", paths.base_dir.display());
            println!("配置文件: {}", paths.config_path().display());
            println!("状态文件: {}", paths.state_path().display());
            Ok(())
        }
        "sql" => probe_sql(&paths).await,
        "feishu" => probe_feishu().await,
        "probe" => {
            probe_sql(&paths).await?;
            println!();
            probe_feishu().await
        }
        other => {
            eprintln!("未知命令: {other}\n");
            print_help();
            std::process::exit(2);
        }
    }
}

fn print_help() {
    println!("sqlfeishu — SQL Server 到飞书多维表格同步");
    println!();
    println!("用法: sqlfeishu <命令>");
    println!();
    println!("命令:");
    println!("  probe    依次检查 SQL Server 与飞书连通性");
    println!("  sql      只检查 SQL Server（连接、可访问库、表列表）");
    println!("  feishu   只检查飞书（令牌、数据表列表、目标表字段）");
    println!("  where    打印实际使用的配置目录");
    println!("  help     显示本帮助");
}

async fn probe_sql(paths: &AppPaths) -> Result<()> {
    let config = paths.read_config()?;
    if config.sources.is_empty() {
        println!("[SQL] {} 里还没有数据源。", paths.config_path().display());
        println!("      可以先用 Python 版网页配置一次，或手工补 sources 段。");
        return Ok(());
    }

    let settings = config::RuntimeSettings::effective(Some(&config.settings));
    println!(
        "[SQL] 运行设置：查询超时 {} 秒，NULL 处理 {}，时区 UTC+{}",
        settings.query_timeout, settings.null_policy, settings.timezone_offset
    );

    for source in &config.sources {
        println!(
            "[SQL] 数据源「{}」 {}:{}/{}",
            source.name, source.server, source.port, source.database
        );
        let mut client = mssql::connect(source).await?;
        mssql::ping(&mut client).await?;
        println!("      连通性 OK");

        match mssql::list_databases(&mut client).await {
            Ok(databases) => println!(
                "      可访问数据库 {} 个：{}",
                databases.len(),
                if databases.is_empty() {
                    "（无）".to_string()
                } else {
                    databases.join(", ")
                }
            ),
            Err(error) => println!("      读取数据库列表失败：{error}"),
        }

        let tables = mssql::list_tables(&mut client).await?;
        println!("      表与视图共 {} 个", tables.len());
        for table in tables.iter().take(10) {
            println!("        {} [{}]", table.qualified(), table.kind);
        }
        if tables.len() > 10 {
            println!("        …另有 {} 个", tables.len() - 10);
        }

        if let Some(first) = tables.first() {
            let columns = mssql::list_columns(&mut client, &first.schema, &first.name).await?;
            println!(
                "      {} 有 {} 个字段：{}",
                first.qualified(),
                columns.len(),
                columns
                    .iter()
                    .map(|column| format!("{}:{}", column.name, column.data_type))
                    .collect::<Vec<_>>()
                    .join(", ")
            );
        }
    }
    Ok(())
}

async fn probe_feishu() -> Result<()> {
    let target = feishu::FeishuClient::target_from_env()?;
    if target.app_token.is_empty() {
        println!("[飞书] 未配置多维表格（FEISHU_BASE_URL 或 FEISHU_BASE_APP_TOKEN）。");
        return Ok(());
    }

    let client = feishu::FeishuClient::from_env()?;
    let tables = client.list_tables(&target.app_token).await?;
    println!(
        "[飞书] 多维表格 {} 共 {} 张表",
        target.app_token,
        tables.len()
    );
    for table in &tables {
        let name = table
            .get("name")
            .and_then(|value| value.as_str())
            .unwrap_or("(未命名)");
        let id = table
            .get("table_id")
            .and_then(|value| value.as_str())
            .unwrap_or("");
        let marker = if !target.table_id.is_empty() && id == target.table_id {
            "  ← 目标表"
        } else {
            ""
        };
        println!("        {name} [{id}]{marker}");
    }

    if !target.table_id.is_empty() {
        let fields = client
            .list_fields(&target.app_token, &target.table_id)
            .await?;
        println!("[飞书] 目标表共 {} 个字段", fields.len());
        for field in &fields {
            let name = field
                .get("field_name")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            let kind = field
                .get("type")
                .and_then(|value| value.as_i64())
                .unwrap_or(1);
            println!("        {name} (类型 {kind})");
        }
    }
    Ok(())
}
