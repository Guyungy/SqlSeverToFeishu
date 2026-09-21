//! 前端可调用的命令。
//!
//! 这一层只做三件事：读写工作区文件、调用同步内核、把 `anyhow` 错误转成
//! 前端能显示的字符串。真正的逻辑一律放在 `sqlserver-to-feishu` 内核里，
//! 命令行版和桌面版共用同一套实现 —— 否则两边迟早会漂移。

use serde::{Deserialize, Serialize};
use tauri::State;

use sqlserver_to_feishu::config::{
    password_env_key, AppPaths, ColumnMapping, DataSource, Incremental, JobState, RuntimeSettings,
    SyncConfig, SyncJob, SyncState, TargetConfig,
};
use sqlserver_to_feishu::{feishu, mssql};

pub struct AppState {
    pub paths: AppPaths,
}

type CmdResult<T> = Result<T, String>;

fn fail(error: impl std::fmt::Display) -> String {
    format!("{error:#}")
}

fn uuid_suffix() -> String {
    uuid::Uuid::new_v4().simple().to_string()[..12].to_string()
}

// ------------------------------------------------------------------ 工作区

/// 回显给界面的飞书配置。
///
/// 刻意**不含 App Secret**：密钥只写不读，界面填过一次之后就不再回显，
/// 留空提交表示「保持原值」。这样即使界面上截图、投屏也不会泄露密钥。
#[derive(Debug, Serialize)]
pub struct FeishuView {
    pub app_id: String,
    pub base_url: String,
    pub base_app_token: String,
    pub base_table_id: String,
    pub base_view_id: String,
    pub app_secret_set: bool,
}

#[derive(Debug, Serialize)]
pub struct Workspace {
    pub base_dir: String,
    pub config_path: String,
    pub env_path: String,
    pub state_path: String,
    pub log_path: String,
    pub feishu: FeishuView,
    pub config: SyncConfig,
}

fn env_value(key: &str) -> String {
    std::env::var(key)
        .map(|value| value.trim().to_string())
        .unwrap_or_default()
}

fn config_of(paths: &AppPaths) -> CmdResult<SyncConfig> {
    paths.read_config().map_err(fail)
}

fn workspace_of(paths: &AppPaths) -> CmdResult<Workspace> {
    Ok(Workspace {
        base_dir: paths.base_dir.display().to_string(),
        config_path: paths.config_path().display().to_string(),
        env_path: paths.env_path().display().to_string(),
        state_path: paths.state_path().display().to_string(),
        log_path: paths.log_path().display().to_string(),
        feishu: FeishuView {
            app_id: env_value("FEISHU_APP_ID"),
            base_url: env_value("FEISHU_BASE_URL"),
            base_app_token: env_value("FEISHU_BASE_APP_TOKEN"),
            base_table_id: env_value("FEISHU_BASE_TABLE_ID"),
            base_view_id: env_value("FEISHU_BASE_VIEW_ID"),
            app_secret_set: !env_value("FEISHU_APP_SECRET").is_empty(),
        },
        config: config_of(paths)?,
    })
}

fn save_config(paths: &AppPaths, config: &SyncConfig) -> CmdResult<()> {
    paths
        .write_json(&paths.config_path(), config)
        .map_err(fail)
}

#[tauri::command]
pub async fn workspace(state: State<'_, AppState>) -> CmdResult<Workspace> {
    workspace_of(&state.paths)
}

// ---------------------------------------------------------------- 飞书目标

#[derive(Debug, Deserialize)]
pub struct FeishuInput {
    pub app_id: String,
    /// 空字符串表示「保持原值不变」，不是「清空」。
    pub app_secret: String,
    pub base_url: String,
    pub base_app_token: String,
    pub base_table_id: String,
    pub base_view_id: String,
}

#[tauri::command]
pub async fn save_feishu(
    state: State<'_, AppState>,
    input: FeishuInput,
) -> CmdResult<Workspace> {
    let paths = &state.paths;
    let mut updates = vec![
        ("FEISHU_APP_ID".to_string(), input.app_id.trim().to_string()),
        ("FEISHU_BASE_URL".to_string(), input.base_url.trim().to_string()),
        (
            "FEISHU_BASE_APP_TOKEN".to_string(),
            input.base_app_token.trim().to_string(),
        ),
        (
            "FEISHU_BASE_TABLE_ID".to_string(),
            input.base_table_id.trim().to_string(),
        ),
        (
            "FEISHU_BASE_VIEW_ID".to_string(),
            input.base_view_id.trim().to_string(),
        ),
    ];
    // 界面不回显密钥。留空提交必须视为「不改」，否则用户点一次保存
    // 就会把已存在的 App Secret 抹成空，表现为突然授权失败。
    if !input.app_secret.trim().is_empty() {
        updates.push((
            "FEISHU_APP_SECRET".to_string(),
            input.app_secret.trim().to_string(),
        ));
    }
    paths.update_env(&updates).map_err(fail)?;
    paths.reload_env().map_err(fail)?;
    workspace_of(paths)
}

#[tauri::command]
pub async fn test_feishu(_state: State<'_, AppState>) -> CmdResult<Vec<String>> {
    let target = feishu::FeishuClient::target_from_env().map_err(fail)?;
    if target.app_token.is_empty() {
        return Err("还没填多维表格链接或 App Token".to_string());
    }
    let client = feishu::FeishuClient::from_env().map_err(fail)?;

    let tables = client.list_tables(&target.app_token).await.map_err(fail)?;
    let mut lines = vec![format!(
        "已取到访问令牌，多维表格 {} 共 {} 张表",
        target.app_token,
        tables.len()
    )];
    for table in tables.iter().take(20) {
        let name = table
            .get("name")
            .and_then(|value| value.as_str())
            .unwrap_or("(未命名)");
        let id = table
            .get("table_id")
            .and_then(|value| value.as_str())
            .unwrap_or("");
        let marker = if id == target.table_id { "  ← 目标表" } else { "" };
        lines.push(format!("  {name} [{id}]{marker}"));
    }
    if tables.len() > 20 {
        lines.push(format!("  …另有 {} 张", tables.len() - 20));
    }

    if !target.table_id.is_empty() {
        let fields = client
            .list_fields(&target.app_token, &target.table_id)
            .await
            .map_err(fail)?;
        lines.push(format!("目标表共 {} 个字段", fields.len()));
        for field in fields.iter().take(30) {
            let name = field
                .get("field_name")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            let kind = field
                .get("type")
                .and_then(|value| value.as_i64())
                .unwrap_or(1);
            lines.push(format!("  {name}（类型 {kind}）"));
        }
    }
    Ok(lines)
}

// ------------------------------------------------------------------ 设置

#[tauri::command]
pub async fn save_settings(
    state: State<'_, AppState>,
    settings: RuntimeSettings,
) -> CmdResult<Workspace> {
    let paths = &state.paths;
    let mut config = config_of(paths)?;
    config.settings = settings;
    save_config(paths, &config)?;
    workspace_of(paths)
}

// ---------------------------------------------------------------- 数据源

#[derive(Debug, Deserialize)]
pub struct SourceInput {
    /// 空表示新建。
    pub id: String,
    pub name: String,
    pub server: String,
    pub port: u16,
    pub database: String,
    pub user: String,
    /// 空表示「保持原密码不变」。
    pub password: String,
    pub enabled: bool,
}

#[tauri::command]
pub async fn save_source(
    state: State<'_, AppState>,
    input: SourceInput,
) -> CmdResult<Workspace> {
    let paths = &state.paths;
    let mut config = config_of(paths)?;

    let id = if input.id.trim().is_empty() {
        format!("source_{}", uuid_suffix())
    } else {
        input.id.trim().to_string()
    };

    if !input.password.is_empty() {
        paths
            .update_env(&[(password_env_key(&id), input.password.clone())])
            .map_err(fail)?;
        paths.reload_env().map_err(fail)?;
    }

    let source = DataSource {
        id: id.clone(),
        name: input.name.trim().to_string(),
        server: input.server.trim().to_string(),
        port: input.port,
        database: input.database.trim().to_string(),
        user: input.user.trim().to_string(),
        password_env: password_env_key(&id),
        enabled: input.enabled,
    };

    match config.sources.iter_mut().find(|item| item.id == id) {
        Some(existing) => *existing = source,
        None => config.sources.push(source),
    }
    save_config(paths, &config)?;
    workspace_of(paths)
}

#[tauri::command]
pub async fn delete_source(state: State<'_, AppState>, id: String) -> CmdResult<Workspace> {
    let paths = &state.paths;
    let mut config = config_of(paths)?;

    // 有任务还在引用时拒绝删除：放行会留下指向空气的任务，
    // 用户在同步那一刻才会看到报错，比现在拦住难排查得多。
    let used: Vec<String> = config
        .jobs
        .iter()
        .filter(|job| job.source_id == id)
        .map(|job| {
            if job.name.is_empty() {
                job.id.clone()
            } else {
                job.name.clone()
            }
        })
        .collect();
    if !used.is_empty() {
        return Err(format!(
            "还有 {} 个任务在用这个数据源（{}），请先删除这些任务",
            used.len(),
            used.join("、")
        ));
    }

    config.sources.retain(|item| item.id != id);
    save_config(paths, &config)?;
    workspace_of(paths)
}

/// 数据源不会因为停用等原因找不到：这里统一取出来，顺带给出可读的报错。
fn source_of(config: &SyncConfig, id: &str) -> CmdResult<DataSource> {
    config
        .sources
        .iter()
        .find(|item| item.id == id)
        .cloned()
        .ok_or_else(|| format!("数据源不存在: {id}"))
}

#[tauri::command]
pub async fn test_source(state: State<'_, AppState>, id: String) -> CmdResult<Vec<String>> {
    let config = config_of(&state.paths)?;
    let source = source_of(&config, &id)?;
    let mut client = mssql::connect(&source).await.map_err(fail)?;
    mssql::ping(&mut client).await.map_err(fail)?;

    let mut lines = vec![format!(
        "连接成功：{}:{}/{}（用户 {}）",
        source.server, source.port, source.database, source.user
    )];
    let tables = mssql::list_tables(&mut client).await.map_err(fail)?;
    lines.push(format!("可访问的表与视图共 {} 个", tables.len()));
    Ok(lines)
}

#[tauri::command]
pub async fn list_databases(state: State<'_, AppState>, id: String) -> CmdResult<Vec<String>> {
    let config = config_of(&state.paths)?;
    let source = source_of(&config, &id)?;
    let mut client = mssql::connect(&source).await.map_err(fail)?;
    mssql::list_databases(&mut client).await.map_err(fail)
}

#[derive(Debug, Serialize)]
pub struct TableOption {
    pub schema: String,
    pub name: String,
    pub kind: String,
}

#[tauri::command]
pub async fn list_tables(state: State<'_, AppState>, id: String) -> CmdResult<Vec<TableOption>> {
    let config = config_of(&state.paths)?;
    let source = source_of(&config, &id)?;
    let mut client = mssql::connect(&source).await.map_err(fail)?;
    Ok(mssql::list_tables(&mut client)
        .await
        .map_err(fail)?
        .into_iter()
        .map(|table| TableOption {
            schema: table.schema,
            name: table.name,
            kind: table.kind,
        })
        .collect())
}

#[derive(Debug, Serialize)]
pub struct ColumnOption {
    pub name: String,
    pub data_type: String,
    pub nullable: bool,
}

#[tauri::command]
pub async fn list_columns(
    state: State<'_, AppState>,
    id: String,
    schema: String,
    table: String,
) -> CmdResult<Vec<ColumnOption>> {
    let config = config_of(&state.paths)?;
    let source = source_of(&config, &id)?;
    let mut client = mssql::connect(&source).await.map_err(fail)?;
    Ok(mssql::list_columns(&mut client, &schema, &table)
        .await
        .map_err(fail)?
        .into_iter()
        .map(|column| ColumnOption {
            name: column.name,
            data_type: column.data_type,
            nullable: column.nullable,
        })
        .collect())
}

// ------------------------------------------------------------------ 任务

#[derive(Debug, Deserialize)]
pub struct JobInput {
    pub id: String,
    pub name: String,
    pub source_id: String,
    pub schema: String,
    pub table: String,
    pub enabled: bool,
    pub columns: Vec<ColumnMapping>,
    pub unique_key: String,
    pub incremental_enabled: bool,
    pub incremental_column: String,
    pub target_mode: String,
    pub target_table_id: String,
    pub target_table_name: String,
    pub auto_create_fields: bool,
}

#[tauri::command]
pub async fn save_job(state: State<'_, AppState>, input: JobInput) -> CmdResult<Workspace> {
    let paths = &state.paths;
    let mut config = config_of(paths)?;
    if config.sources.iter().all(|item| item.id != input.source_id) {
        return Err("请先选择一个存在的 SQL 数据源".to_string());
    }
    if input.columns.is_empty() {
        return Err("至少要为一列配置映射".to_string());
    }
    if input.unique_key.trim().is_empty() {
        return Err("必须指定唯一键列，否则无法判断记录该新增还是更新".to_string());
    }

    let id = if input.id.trim().is_empty() {
        format!("job_{}", uuid_suffix())
    } else {
        input.id.trim().to_string()
    };

    let job = SyncJob {
        id: id.clone(),
        name: input.name.trim().to_string(),
        source_id: input.source_id.clone(),
        schema: input.schema.trim().to_string(),
        table: input.table.trim().to_string(),
        enabled: input.enabled,
        columns: input.columns,
        unique_key: input.unique_key.trim().to_string(),
        incremental: Incremental {
            enabled: input.incremental_enabled,
            column: input.incremental_column.trim().to_string(),
        },
        target: TargetConfig {
            mode: input.target_mode.trim().to_string(),
            table_id: input.target_table_id.trim().to_string(),
            table_name: input.target_table_name.trim().to_string(),
            auto_create_fields: input.auto_create_fields,
        },
    };

    match config.jobs.iter_mut().find(|item| item.id == id) {
        Some(existing) => *existing = job,
        None => config.jobs.push(job),
    }
    save_config(paths, &config)?;
    workspace_of(paths)
}

#[tauri::command]
pub async fn delete_job(state: State<'_, AppState>, id: String) -> CmdResult<Workspace> {
    let paths = &state.paths;
    let mut config = config_of(paths)?;
    config.jobs.retain(|item| item.id != id);
    save_config(paths, &config)?;

    // 任务没了，它的增量游标也一起清掉；留着下次同名任务可能会误用。
    let state_path = paths.state_path();
    let mut sync_state = SyncState::load(&state_path).map_err(fail)?;
    sync_state.jobs.remove(&id);
    paths
        .write_json(&state_path, &sync_state)
        .map_err(fail)?;

    workspace_of(paths)
}

// ------------------------------------------------------------------ 状态

#[derive(Debug, Serialize)]
pub struct JobStateView {
    pub id: String,
    pub fingerprint: String,
    pub cursor: Option<serde_json::Value>,
}

fn job_states_of(paths: &AppPaths) -> CmdResult<Vec<JobStateView>> {
    let sync_state = SyncState::load(&paths.state_path()).map_err(fail)?;
    Ok(sync_state
        .jobs
        .into_iter()
        .map(|(id, JobState { fingerprint, cursor })| JobStateView {
            id,
            fingerprint,
            cursor,
        })
        .collect())
}

#[tauri::command]
pub async fn job_states(state: State<'_, AppState>) -> CmdResult<Vec<JobStateView>> {
    job_states_of(&state.paths)
}

#[tauri::command]
pub async fn reset_job_state(
    state: State<'_, AppState>,
    id: String,
) -> CmdResult<Vec<JobStateView>> {
    let paths = &state.paths;
    let state_path = paths.state_path();
    let mut sync_state = SyncState::load(&state_path).map_err(fail)?;
    sync_state.jobs.remove(&id);
    paths.write_json(&state_path, &sync_state).map_err(fail)?;
    job_states_of(paths)
}

#[tauri::command]
pub async fn tail_log(state: State<'_, AppState>, lines: usize) -> CmdResult<Vec<String>> {
    let path = state.paths.log_path();
    if !path.exists() {
        return Ok(Vec::new());
    }
    let text = std::fs::read_to_string(&path).map_err(fail)?;
    let all: Vec<&str> = text.lines().collect();
    let take = lines.clamp(1, 2000);
    let start = all.len().saturating_sub(take);
    Ok(all[start..].iter().map(|line| line.to_string()).collect())
}
