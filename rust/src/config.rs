//! 配置与状态存储：对齐 Python 版 `workspace.py` 的语义。
//!
//! 与 Python 版最大的差异在路径选择上：打包成单文件后，可执行文件可能
//! 落在只读位置（macOS 的 `/Applications`、Windows 的 `Program Files`），
//! 所以这里先探测 exe 同目录是否可写，不可写则退回用户数据目录。

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

pub const ENV_FILE: &str = ".env";
pub const CONFIG_FILE: &str = "sync_config.json";
pub const STATE_FILE: &str = "sync_state.json";
pub const CONFIG_VERSION: u32 = 4;

/// 配置与状态文件所在目录。
#[derive(Debug, Clone)]
pub struct AppPaths {
    pub base_dir: PathBuf,
}

impl AppPaths {
    /// exe 同目录优先；不可写时退回用户数据目录。
    pub fn discover() -> Result<Self> {
        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(Path::to_path_buf));

        if let Some(dir) = exe_dir {
            if is_writable(&dir) {
                return Ok(Self { base_dir: dir });
            }
        }

        Self::user_data()
    }

    /// 强制使用用户数据目录，不看 exe 同目录。
    ///
    /// 桌面版（Tauri）必须走这条：应用的可执行文件位于
    /// `Xxx.app/Contents/MacOS/` 内，那里通常**是可写的**，于是 `discover()`
    /// 会把配置写进应用包内部——既不合平台规范，也会在重装或升级应用时
    /// 被整体覆盖，用户配置凭空消失。命令行版继续用 `discover()` 保持
    /// 「配置就放在程序旁边」的直觉。
    pub fn user_data() -> Result<Self> {
        let dir = user_data_dir();
        std::fs::create_dir_all(&dir)
            .with_context(|| format!("无法创建数据目录 {}", dir.display()))?;
        Ok(Self { base_dir: dir })
    }

    pub fn env_path(&self) -> PathBuf {
        self.base_dir.join(ENV_FILE)
    }

    pub fn config_path(&self) -> PathBuf {
        self.base_dir.join(CONFIG_FILE)
    }

    pub fn state_path(&self) -> PathBuf {
        self.base_dir.join(STATE_FILE)
    }

    pub fn log_path(&self) -> PathBuf {
        self.base_dir.join("sync.log")
    }

    /// 更新 `.env`，返回写入后的路径。
    ///
    /// 与 Python 版 `workspace.update_env` 的差异：这里**保留注释和原有顺序**，
    /// 只替换命中的键、追加缺失的键。Python 版会把整个文件按键名排序重写、
    /// 顺手丢掉注释；两个版本读同一个文件，但 Rust 版写得更有礼貌。
    /// 值的引号规则与 Python 版一致（单行双引号转义），互相都能读。
    pub fn update_env(&self, updates: &[(String, String)]) -> Result<PathBuf> {
        let path = self.env_path();
        let existing = std::fs::read_to_string(&path).unwrap_or_default();
        let mut pending: Vec<(String, String)> = updates.to_vec();
        let mut lines: Vec<String> = Vec::new();

        for line in existing.lines() {
            let trimmed = line.trim_start();
            let key = if trimmed.starts_with('#') {
                None
            } else {
                trimmed
                    .split_once('=')
                    .map(|(key, _)| key.trim().to_string())
            };
            match key {
                Some(name) => match pending.iter().position(|(key, _)| *key == name) {
                    Some(index) => {
                        let (_, value) = pending.remove(index);
                        lines.push(format!("{name}={}", quote_env_value(&value)));
                    }
                    None => lines.push(line.to_string()),
                },
                None => lines.push(line.to_string()),
            }
        }
        for (key, value) in pending {
            lines.push(format!("{key}={}", quote_env_value(&value)));
        }

        let temp = self.base_dir.join(".env.tmp");
        std::fs::write(&temp, format!("{}\n", lines.join("\n")))
            .with_context(|| format!("无法写入 {}", temp.display()))?;
        // .env 里有密码，权限必须是 0600（Python 版同样这么设）。
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&temp, std::fs::Permissions::from_mode(0o600));
        }
        std::fs::rename(&temp, &path).with_context(|| format!("无法替换 {}", path.display()))?;
        Ok(path)
    }

    /// 把新写入的 `.env` 同步进当前进程的环境变量。
    ///
    /// `dotenvy` 默认不覆盖已存在的变量，所以保存后不重新载入的话，
    /// 界面里改的值要重启才生效。
    pub fn reload_env(&self) -> Result<()> {
        let path = self.env_path();
        if !path.exists() {
            return Ok(());
        }
        let values = dotenvy::from_path_iter(&path)
            .with_context(|| format!("无法解析 {}", path.display()))?;
        for item in values {
            let (key, value) = item.with_context(|| format!("{} 格式错误", path.display()))?;
            std::env::set_var(key, value);
        }
        Ok(())
    }

    /// 载入 `.env`（存在才载入，缺失不是错误）。
    pub fn load_env(&self) -> Result<()> {
        let path = self.env_path();
        if path.exists() {
            dotenvy::from_path(&path).with_context(|| format!("无法读取 {}", path.display()))?;
        }
        Ok(())
    }

    pub fn read_config(&self) -> Result<SyncConfig> {
        let path = self.config_path();
        if !path.exists() {
            return Ok(SyncConfig::empty());
        }
        let raw = std::fs::read_to_string(&path)
            .with_context(|| format!("无法读取 {}", path.display()))?;
        let parsed: SyncConfig = serde_json::from_str(&raw)
            .with_context(|| format!("{} 格式错误，请从备份恢复", path.display()))?;
        Ok(parsed)
    }

    /// 原子写入：先写同目录临时文件，再 rename 覆盖。
    pub fn write_json<T: Serialize>(&self, path: &Path, value: &T) -> Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let temp = path.with_extension("json.tmp");
        let text = serde_json::to_string_pretty(value)?;
        std::fs::write(&temp, format!("{text}\n"))
            .with_context(|| format!("无法写入 {}", temp.display()))?;
        std::fs::rename(&temp, path).with_context(|| format!("无法替换 {}", path.display()))?;
        Ok(())
    }
}

fn is_writable(dir: &Path) -> bool {
    let probe = dir.join(".write-probe");
    match std::fs::write(&probe, b"") {
        Ok(()) => {
            let _ = std::fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

#[cfg(target_os = "macos")]
fn user_data_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(|home| PathBuf::from(home).join("Library/Application Support/SqlSeverToFeishu"))
        .unwrap_or_else(|| PathBuf::from("."))
}

#[cfg(target_os = "windows")]
fn user_data_dir() -> PathBuf {
    std::env::var_os("APPDATA")
        .map(|appdata| PathBuf::from(appdata).join("SqlSeverToFeishu"))
        .unwrap_or_else(|| PathBuf::from("."))
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn user_data_dir() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(|base| PathBuf::from(base).join("sqlserver-to-feishu"))
        .unwrap_or_else(|| PathBuf::from("."))
}

// ---------------------------------------------------------------- 数据源

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DataSource {
    pub id: String,
    pub name: String,
    pub server: String,
    #[serde(default = "default_port")]
    pub port: u16,
    #[serde(default)]
    pub database: String,
    #[serde(default)]
    pub user: String,
    /// 密码不落盘在配置里，只存环境变量名。
    pub password_env: String,
    #[serde(default = "default_true")]
    pub enabled: bool,
}

fn default_port() -> u16 {
    1433
}

fn default_true() -> bool {
    true
}

impl DataSource {
    pub fn password(&self) -> String {
        std::env::var(&self.password_env)
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| std::env::var("DB_PASSWORD").unwrap_or_default())
    }
}

// ------------------------------------------------------------------ 任务

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ColumnMapping {
    pub source: String,
    pub target: String,
    #[serde(default)]
    pub sql_type: String,
    #[serde(default = "default_true")]
    pub nullable: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Incremental {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub column: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TargetConfig {
    #[serde(default = "default_target_mode")]
    pub mode: String,
    #[serde(default)]
    pub table_id: String,
    #[serde(default)]
    pub table_name: String,
    #[serde(default = "default_true")]
    pub auto_create_fields: bool,
}

fn default_target_mode() -> String {
    "auto".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SyncJob {
    pub id: String,
    #[serde(default)]
    pub name: String,
    pub source_id: String,
    #[serde(default = "default_schema")]
    pub schema: String,
    pub table: String,
    #[serde(default = "default_true")]
    pub enabled: bool,
    pub columns: Vec<ColumnMapping>,
    pub unique_key: String,
    #[serde(default)]
    pub incremental: Incremental,
    pub target: TargetConfig,
}

fn default_schema() -> String {
    "dbo".to_string()
}

impl SyncJob {
    /// 增量游标指纹：数据源 / 字段 / 唯一键 / 增量字段 / 目标表任一变化，
    /// 旧游标立即失效并回退全量。对齐 Python 版 `job_fingerprint`。
    pub fn fingerprint(&self, app_token: &str) -> String {
        let columns: Vec<String> = self
            .columns
            .iter()
            .map(|item| format!("{}:{}:{}", item.source, item.target, item.sql_type))
            .collect();
        format!(
            "{}\u{1f}{}\u{1f}{}\u{1f}{}\u{1f}{}\u{1f}{}\u{1f}{}\u{1f}{}\u{1f}{}",
            self.source_id,
            self.schema,
            self.table,
            columns.join(","),
            self.unique_key,
            self.incremental.enabled,
            self.incremental.column,
            app_token,
            self.target.table_id,
        )
    }

    /// 增量字段的 SQL 类型，用于判断游标类型是否仍然匹配。
    pub fn incremental_type(&self) -> Option<&str> {
        let column = self.incremental.column.as_str();
        self.columns
            .iter()
            .find(|item| item.source == column)
            .map(|item| item.sql_type.as_str())
    }
}

// -------------------------------------------------------------- 运行设置

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NullPolicy {
    Skip,
    Overwrite,
}

impl NullPolicy {
    pub fn parse(raw: &str) -> Result<Self> {
        // 历史文档写过 clear，语义等同 overwrite，必须继续接受，
        // 否则老配置会被静默降级成 skip（从"清空"变成"跳过"）。
        match raw.trim().to_ascii_lowercase().as_str() {
            "" | "skip" => Ok(Self::Skip),
            "overwrite" | "clear" => Ok(Self::Overwrite),
            other => Err(anyhow::anyhow!(
                "SQL NULL 处理只能是 跳过(skip) 或 覆盖(overwrite)，收到 {other}"
            )),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Skip => "skip",
            Self::Overwrite => "overwrite",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct RuntimeSettings {
    pub query_timeout: u64,
    pub null_policy: String,
    pub timezone_offset: f64,
    pub dashboard_port: u16,
    pub schedule_enabled: bool,
    pub schedule_interval_minutes: u64,
}

impl Default for RuntimeSettings {
    fn default() -> Self {
        Self {
            query_timeout: 60,
            null_policy: "skip".to_string(),
            timezone_offset: 8.0,
            dashboard_port: 5001,
            schedule_enabled: false,
            schedule_interval_minutes: 60,
        }
    }
}

impl RuntimeSettings {
    /// 取值优先级：界面设置 > `.env` > 默认值。
    /// 服务端口例外——命令行显式设置 `DASHBOARD_PORT` 时以命令行为准。
    pub fn effective(raw: Option<&Self>) -> Self {
        let mut settings = raw.cloned().unwrap_or_default();

        // 端口属于启动参数：命令行显式给出时，意图强于界面里保存的值。
        if let Some(value) = env_string("DASHBOARD_PORT") {
            if let Ok(port) = value.parse::<u16>() {
                settings.dashboard_port = port;
            }
        }

        if let Some(value) = env_string("DB_QUERY_TIMEOUT") {
            if let Ok(parsed) = value.parse::<u64>() {
                settings.query_timeout = parsed.clamp(1, 600);
            }
        }
        if let Some(value) = env_string("SYNC_NULL_POLICY") {
            if let Ok(policy) = NullPolicy::parse(&value) {
                settings.null_policy = policy.as_str().to_string();
            }
        }
        if let Some(value) = env_string("SYNC_TIMEZONE_OFFSET") {
            if let Ok(parsed) = value.parse::<f64>() {
                if (-12.0..=14.0).contains(&parsed) {
                    settings.timezone_offset = parsed;
                }
            }
        }
        if let Some(value) = env_string("SYNC_SCHEDULE_ENABLED") {
            settings.schedule_enabled = matches!(
                value.to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            );
        }
        if let Some(value) = env_u64("SYNC_SCHEDULE_INTERVAL_MINUTES") {
            settings.schedule_interval_minutes = value.clamp(1, 10080);
        }

        settings
    }

    pub fn null_policy_kind(&self) -> NullPolicy {
        NullPolicy::parse(&self.null_policy).unwrap_or(NullPolicy::Skip)
    }
}

fn env_string(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn env_u64(key: &str) -> Option<u64> {
    env_string(key).and_then(|value| value.parse::<u64>().ok())
}

// ------------------------------------------------------------ 顶层配置

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SyncConfig {
    #[serde(default = "default_version")]
    pub version: u32,
    #[serde(default)]
    pub sources: Vec<DataSource>,
    #[serde(default)]
    pub jobs: Vec<SyncJob>,
    #[serde(default)]
    pub settings: RuntimeSettings,
}

fn default_version() -> u32 {
    CONFIG_VERSION
}

impl SyncConfig {
    pub fn empty() -> Self {
        Self {
            version: CONFIG_VERSION,
            sources: Vec::new(),
            jobs: Vec::new(),
            settings: RuntimeSettings::default(),
        }
    }

    pub fn source(&self, source_id: &str) -> Result<&DataSource> {
        self.sources
            .iter()
            .find(|item| item.id == source_id)
            .ok_or_else(|| anyhow::anyhow!("数据源不存在: {source_id}"))
    }
}

// -------------------------------------------------------------- 增量状态

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobState {
    #[serde(default)]
    pub fingerprint: String,
    #[serde(default)]
    pub cursor: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SyncState {
    #[serde(default = "default_state_version")]
    pub version: u32,
    #[serde(default)]
    pub jobs: std::collections::BTreeMap<String, JobState>,
}

fn default_state_version() -> u32 {
    1
}

impl Default for SyncState {
    fn default() -> Self {
        Self {
            version: 1,
            jobs: std::collections::BTreeMap::new(),
        }
    }
}

impl SyncState {
    pub fn load(path: &Path) -> Result<Self> {
        if !path.exists() {
            return Ok(Self::default());
        }
        let raw = std::fs::read_to_string(path)
            .with_context(|| format!("无法读取 {}", path.display()))?;
        serde_json::from_str(&raw)
            .with_context(|| format!("{} 已损坏，请备份后重置增量状态", path.display()))
    }
}

/// SQL 标识符安全校验：与查询生成用同一套规则，避免注入。
pub fn clean_identifier_part(part: &str) -> Result<String> {
    let mut value = part.trim().to_string();
    if value.starts_with('[') && value.ends_with(']') && value.len() >= 2 {
        value = value[1..value.len() - 1].replace("]]", "]");
    }
    if value.is_empty() || value.chars().count() > 128 {
        return Err(anyhow::anyhow!("SQL 标识符为空或过长: {part}"));
    }
    for token in [";", "--", "/*", "*/", "'", "\"", "\0"] {
        if value.contains(token) {
            return Err(anyhow::anyhow!("SQL 标识符包含不安全字符: {part}"));
        }
    }
    // 注意：str::contains 的 Pattern 只实现给 char / &str / &[char]，
    // 数组字面量必须先取切片。
    if value.contains(&['\r', '\n', '\t', '(', ')'][..]) {
        return Err(anyhow::anyhow!("SQL 标识符格式不正确: {part}"));
    }
    Ok(value)
}

/// 只接受 `table` 或 `schema.table`，返回 (schema, table)。
pub fn parse_table_name(table_name: &str) -> Result<(String, String)> {
    let parts: Vec<&str> = table_name.trim().split('.').collect();
    match parts.len() {
        1 => Ok(("dbo".to_string(), clean_identifier_part(parts[0])?)),
        2 => Ok((
            clean_identifier_part(parts[0])?,
            clean_identifier_part(parts[1])?,
        )),
        _ => Err(anyhow::anyhow!("数据表名仅支持 table 或 schema.table 格式")),
    }
}

/// 用 `[...]` 包住标识符，内部 `]` 双写转义。
pub fn quote_identifier(value: &str) -> Result<String> {
    let cleaned = clean_identifier_part(value)?;
    Ok(format!("[{}]", cleaned.replace(']', "]]")))
}

pub fn quoted_table(table_name: &str) -> Result<String> {
    let (schema, table) = parse_table_name(table_name)?;
    Ok(format!(
        "{}.{}",
        quote_identifier(&schema)?,
        quote_identifier(&table)?
    ))
}

/// 与 Python 版 `_quote_env` 完全一致的引号规则，保证两个版本互读无歧义。
fn quote_env_value(value: &str) -> String {
    let escaped = value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n");
    format!("\"{escaped}\"")
}

/// 数据源密码在 `.env` 里的键名。
///
/// 必须与 Python 版 `password_env_key` 逐字符一致：键名不同的话，
/// 换到 Rust 版就读不到用户原来存的密码，会表现为「认证失败」。
pub fn password_env_key(source_id: &str) -> String {
    let safe: String = source_id
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() {
                ch.to_ascii_uppercase()
            } else {
                '_'
            }
        })
        .collect();
    format!("SQL_SOURCE_{safe}_PASSWORD")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_writes_preserve_comments_and_are_readable() {
        let dir = std::env::temp_dir().join(format!("sqlfeishu-env-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let paths = AppPaths {
            base_dir: dir.clone(),
        };
        std::fs::write(
            paths.env_path(),
            "# 注释必须保留\nDB_SERVER='localhost'\nOLD_KEY=\"keep\"\n",
        )
        .unwrap();

        paths
            .update_env(&[
                ("DB_SERVER".to_string(), "10.0.0.5".to_string()),
                ("NEW_KEY".to_string(), "含\"引号\"与中文".to_string()),
            ])
            .unwrap();

        let text = std::fs::read_to_string(paths.env_path()).unwrap();
        assert!(text.contains("# 注释必须保留"), "注释被丢掉了: {text}");
        assert!(
            text.contains("OLD_KEY=\"keep\""),
            "未命中的键被改动了: {text}"
        );
        assert!(
            text.contains(r#"NEW_KEY="含\"引号\"与中文""#),
            "值必须转义: {text}"
        );

        // 写出的格式必须能被 dotenvy 读回，否则等于写了个读不出来的文件。
        let parsed: std::collections::HashMap<String, String> =
            dotenvy::from_path_iter(paths.env_path())
                .unwrap()
                .map(|item| item.unwrap())
                .collect();
        assert_eq!(
            parsed.get("DB_SERVER").map(String::as_str),
            Some("10.0.0.5")
        );
        assert_eq!(
            parsed.get("NEW_KEY").map(String::as_str),
            Some("含\"引号\"与中文")
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn password_env_key_matches_python_version() {
        // 对照 Python 版 workspace.password_env_key 的输出。
        // 键名一旦不一致，换版本后用户原存的密码就读不到了。
        assert_eq!(
            password_env_key("source_abc123"),
            "SQL_SOURCE_SOURCE_ABC123_PASSWORD"
        );
        assert_eq!(password_env_key("a-b.c"), "SQL_SOURCE_A_B_C_PASSWORD");
    }

    #[test]
    fn parses_table_names() {
        assert_eq!(
            parse_table_name("Orders").unwrap(),
            ("dbo".to_string(), "Orders".to_string())
        );
        assert_eq!(
            parse_table_name("sales.Orders").unwrap(),
            ("sales".to_string(), "Orders".to_string())
        );
        assert!(parse_table_name("a.b.c").is_err());
    }

    #[test]
    fn rejects_injection_attempts() {
        assert!(clean_identifier_part("Orders; DROP TABLE x").is_err());
        assert!(clean_identifier_part("Orders--").is_err());
        assert!(clean_identifier_part("Orders'").is_err());
        assert!(clean_identifier_part("[Order]]s]").is_ok());
    }

    #[test]
    fn quotes_identifiers() {
        assert_eq!(quote_identifier("Orders").unwrap(), "[Orders]");
        assert_eq!(
            quoted_table("sales.Order Details").unwrap(),
            "[sales].[Order Details]"
        );
    }

    #[test]
    fn null_policy_accepts_legacy_clear() {
        assert_eq!(NullPolicy::parse("clear").unwrap(), NullPolicy::Overwrite);
        assert_eq!(NullPolicy::parse("").unwrap(), NullPolicy::Skip);
        assert_eq!(
            NullPolicy::parse("OVERWRITE").unwrap(),
            NullPolicy::Overwrite
        );
        assert!(NullPolicy::parse("bogus").is_err());
    }

    #[test]
    fn fingerprint_changes_with_any_binding() {
        let job = SyncJob {
            id: "job_1".into(),
            name: "t".into(),
            source_id: "source_1".into(),
            schema: "dbo".into(),
            table: "Orders".into(),
            enabled: true,
            columns: vec![ColumnMapping {
                source: "Id".into(),
                target: "Id".into(),
                sql_type: "int".into(),
                nullable: false,
            }],
            unique_key: "Id".into(),
            incremental: Incremental {
                enabled: true,
                column: "Id".into(),
            },
            target: TargetConfig {
                mode: "auto".into(),
                table_id: String::new(),
                table_name: "Orders".into(),
                auto_create_fields: true,
            },
        };
        let base = job.fingerprint("app_1");
        assert_eq!(base, job.fingerprint("app_1"));

        let mut changed = job.clone();
        changed.incremental.column = "UpdatedAt".into();
        assert_ne!(base, changed.fingerprint("app_1"));
        assert_ne!(base, job.fingerprint("app_2"));
    }
}
