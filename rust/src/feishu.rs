//! 飞书开放平台客户端：令牌管理、带退避重试的请求、多维表格元数据分页。
//!
//! 对齐 Python 版 `sync.py` 中的 `FeishuClient`：同样的重试策略、
//! 同样的令牌过期码集合，同样在分页缺 `page_token` 时立即失败而不是继续翻页。

use std::collections::HashMap;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use serde_json::json;

use crate::config::env_or_map;

pub const DEFAULT_API_BASE: &str = "https://open.feishu.cn/open-apis";
const MAX_ATTEMPTS: usize = 5;
const REQUEST_TIMEOUT_SECS: u64 = 60;
const CONNECT_TIMEOUT_SECS: u64 = 5;
/// 分页上限（每页 500 条）：约 10 万条。用于兜底「服务器一直回 has_more
/// 且游标不推进」的病态响应，避免 `loop` 永不退出。
const MAX_PAGES: usize = 200;

/// 飞书返回这些业务码时属于可重试的暂时性故障。
const TRANSIENT_CODES: [i64; 3] = [1254290, 1254291, 1254607];
/// 令牌失效相关业务码，需要清空缓存令牌后重试。
const TOKEN_EXPIRED_CODES: [i64; 3] = [99991663, 99991664, 99991668];

// ------------------------------------------------------------ 链接解析

const ALLOWED_HOST_SUFFIXES: [&str; 3] = [".feishu.cn", ".larksuite.com", ".larkoffice.com"];

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct BaseTarget {
    pub app_token: String,
    pub table_id: String,
    pub view_id: String,
}

fn is_token(value: &str) -> bool {
    value.len() >= 10 && value.chars().all(|ch| ch.is_ascii_alphanumeric())
}

fn is_id(value: &str) -> bool {
    !value.is_empty()
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-')
}

fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            let high = (bytes[index + 1] as char).to_digit(16);
            let low = (bytes[index + 2] as char).to_digit(16);
            if let (Some(high), Some(low)) = (high, low) {
                out.push((high * 16 + low) as u8);
                index += 3;
                continue;
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// 解析多维表格链接。支持完整 HTTPS 链接、裸 app_token，以及 wiki 链接的明确拒绝。
pub fn parse_base_url(raw: &str) -> Result<BaseTarget> {
    let value = raw.trim();
    if value.is_empty() {
        return Ok(BaseTarget::default());
    }
    if is_token(value) {
        return Ok(BaseTarget {
            app_token: value.to_string(),
            ..Default::default()
        });
    }

    let rest = value
        .strip_prefix("https://")
        .ok_or_else(|| anyhow!("飞书链接必须是完整的 HTTPS 地址"))?;
    let (host_part, after_host) = match rest.find('/') {
        Some(index) => (&rest[..index], &rest[index..]),
        None => (rest, ""),
    };
    let host = host_part
        .split(':')
        .next()
        .unwrap_or("")
        .to_ascii_lowercase();
    if host.is_empty() {
        return Err(anyhow!("飞书链接缺少域名"));
    }
    if !ALLOWED_HOST_SUFFIXES
        .iter()
        .any(|suffix| host.ends_with(suffix))
    {
        return Err(anyhow!("链接域名不是受支持的飞书/Lark 域名"));
    }

    let (path, query) = match after_host.find('?') {
        Some(index) => (&after_host[..index], &after_host[index + 1..]),
        None => (after_host, ""),
    };

    let parts: Vec<String> = path
        .split('/')
        .filter(|part| !part.is_empty())
        .map(percent_decode)
        .collect();
    if parts.iter().any(|part| part == "wiki") {
        return Err(anyhow!(
            "暂不支持知识库 wiki 链接，请打开多维表格后复制 /base/ 链接"
        ));
    }
    let base_index = parts
        .iter()
        .position(|part| part == "base")
        .ok_or_else(|| anyhow!("链接中未找到 /base/{{app_token}}"))?;
    let app_token = parts.get(base_index + 1).cloned().unwrap_or_default();
    if !is_token(&app_token) {
        return Err(anyhow!("链接中的多维表格 App Token 格式不正确"));
    }

    let mut table_id = String::new();
    let mut view_id = String::new();
    for pair in query.split('&').filter(|pair| !pair.is_empty()) {
        let (key, value) = match pair.split_once('=') {
            Some((key, value)) => (key, percent_decode(value)),
            None => (pair, String::new()),
        };
        match key {
            "table" | "tbl" | "table_id" if table_id.is_empty() => table_id = value,
            "view" | "view_id" if view_id.is_empty() => view_id = value,
            _ => {}
        }
    }
    if !table_id.is_empty() && !is_id(&table_id) {
        return Err(anyhow!("链接中的 Table ID 格式不正确"));
    }
    if !view_id.is_empty() && !is_id(&view_id) {
        return Err(anyhow!("链接中的 View ID 格式不正确"));
    }

    Ok(BaseTarget {
        app_token,
        table_id,
        view_id,
    })
}

// -------------------------------------------------------------- 客户端

fn api_base() -> String {
    std::env::var("FEISHU_API_BASE")
        .ok()
        .map(|value| value.trim().trim_end_matches('/').to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_API_BASE.to_string())
}

async fn sleep_backoff(attempt: usize) {
    let seconds = (0.5 * 2f64.powi(attempt as i32)).min(8.0);
    tokio::time::sleep(Duration::from_secs_f64(seconds)).await;
}

pub struct FeishuClient {
    http: reqwest::Client,
    api_base: String,
    app_id: String,
    app_secret: String,
    token: tokio::sync::Mutex<Option<String>>,
    /// 授权进行中标志：并发的首个请求只发起一次 auth，其余等待其完成。
    auth_inflight: tokio::sync::Mutex<bool>,
}

impl FeishuClient {
    /// 接口地址取自 `FEISHU_API_BASE`，未设置时用官方地址。
    pub fn new(app_id: impl Into<String>, app_secret: impl Into<String>) -> Result<Self> {
        Self::with_api_base(app_id, app_secret, api_base())
    }

    /// 显式指定接口地址。
    ///
    /// 集成测试需要把请求指向本地桩服务；如果只靠 `new()` 读环境变量，
    /// 同一个测试进程里的并行用例会互相覆盖 `FEISHU_API_BASE`。
    pub fn with_api_base(
        app_id: impl Into<String>,
        app_secret: impl Into<String>,
        api_base: impl Into<String>,
    ) -> Result<Self> {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(CONNECT_TIMEOUT_SECS))
            .timeout(Duration::from_secs(REQUEST_TIMEOUT_SECS))
            .user_agent(concat!("sqlserver-to-feishu/", env!("CARGO_PKG_VERSION")))
            .build()
            .context("初始化飞书 HTTP 客户端失败")?;
        Ok(Self {
            http,
            api_base: api_base.into(),
            app_id: app_id.into(),
            app_secret: app_secret.into(),
            token: tokio::sync::Mutex::new(None),
            auth_inflight: tokio::sync::Mutex::new(false),
        })
    }

    /// 从环境变量构造；缺 App ID / Secret 时报错。
    pub fn from_env() -> Result<Self> {
        Self::from_map(&HashMap::new())
    }

    /// 从 `.env` 映射构造（其次进程环境）；缺 App ID / Secret 时报错。
    pub fn from_map(env: &HashMap<String, String>) -> Result<Self> {
        let app_id = env_or_map(env, "FEISHU_APP_ID");
        let app_secret = env_or_map(env, "FEISHU_APP_SECRET");
        if app_id.trim().is_empty() || app_secret.trim().is_empty() {
            return Err(anyhow!("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET"));
        }
        Self::new(app_id.trim().to_string(), app_secret.trim().to_string())
    }

    /// 目标多维表格：优先解析链接，其次回退到独立的 token 环境变量。
    pub fn target_from_env() -> Result<BaseTarget> {
        Self::target_from_map(&HashMap::new())
    }

    /// 同 [`FeishuClient::target_from_env`]，但先查 `.env` 映射。
    pub fn target_from_map(env: &HashMap<String, String>) -> Result<BaseTarget> {
        let url = env_or_map(env, "FEISHU_BASE_URL");
        let mut target = parse_base_url(&url)?;
        if target.app_token.is_empty() {
            target.app_token = env_or_map(env, "FEISHU_BASE_APP_TOKEN");
        }
        if target.table_id.is_empty() {
            target.table_id = env_or_map(env, "FEISHU_BASE_TABLE_ID");
        }
        if target.view_id.is_empty() {
            target.view_id = env_or_map(env, "FEISHU_BASE_VIEW_ID");
        }
        Ok(target)
    }

    /// 取一个可用令牌：缓存命中直接返回，否则做单飞授权。
    ///
    /// 关键点：`auth()` 是网络往返，绝不能持锁执行——否则拿锁的请求一旦卡住，
    /// 其他请求全部排队等它。这里先用 `auth_inflight` 让并发请求只授权一次，
    /// 真正的取值与写回都用短临界区。
    async fn token(&self) -> Result<String> {
        if let Some(token) = self.token.lock().await.clone() {
            return Ok(token);
        }

        let mut inflight = self.auth_inflight.lock().await;
        if !*inflight {
            *inflight = true;
            let result = self.auth().await;
            // 先解锁再写缓存：让等待的请求能立刻观察到新令牌。
            *inflight = false;
            drop(inflight);

            let fresh = result?;
            *self.token.lock().await = Some(fresh.clone());
            return Ok(fresh);
        }
        drop(inflight);

        // 已有请求在授权：轮询等待其写回缓存（授权失败时超时退出，不永久挂住）。
        for _ in 0..300 {
            tokio::time::sleep(Duration::from_millis(50)).await;
            if let Some(token) = self.token.lock().await.clone() {
                return Ok(token);
            }
        }
        Err(anyhow!("等待飞书令牌超时"))
    }

    async fn auth(&self) -> Result<String> {
        let mut last_error: Option<anyhow::Error> = None;
        for attempt in 0..MAX_ATTEMPTS {
            let response = self
                .http
                .post(format!(
                    "{}/auth/v3/tenant_access_token/internal",
                    self.api_base
                ))
                .json(&json!({
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                }))
                .send()
                .await;

            let response = match response {
                Ok(response) => response,
                Err(error) => {
                    last_error = Some(anyhow!("飞书授权请求失败: {error}"));
                    sleep_backoff(attempt).await;
                    continue;
                }
            };

            let status = response.status();
            if status.as_u16() == 429 || status.is_server_error() {
                last_error = Some(anyhow!("飞书授权接口暂时不可用（HTTP {status}）"));
                sleep_backoff(attempt).await;
                continue;
            }

            let data: serde_json::Value = response.json().await.context("飞书授权响应无法解析")?;
            let code = data
                .get("code")
                .and_then(|value| value.as_i64())
                .unwrap_or(0);
            let token = data
                .get("tenant_access_token")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            if code != 0 || token.is_empty() {
                let message = data
                    .get("msg")
                    .and_then(|value| value.as_str())
                    .unwrap_or("未知错误");
                return Err(anyhow!("飞书授权失败 code={code}: {message}"));
            }
            return Ok(token.to_string());
        }
        Err(last_error.unwrap_or_else(|| anyhow!("飞书授权重试后仍失败")))
    }

    /// 带令牌刷新与指数退避的通用请求。
    pub async fn request(
        &self,
        method: reqwest::Method,
        path: &str,
        query: &[(String, String)],
        body: Option<serde_json::Value>,
    ) -> Result<serde_json::Value> {
        let mut last_error: Option<anyhow::Error> = None;

        for attempt in 0..MAX_ATTEMPTS {
            let token = self.token().await?;

            let mut builder = self
                .http
                .request(method.clone(), format!("{}{}", self.api_base, path))
                .header("Authorization", format!("Bearer {token}"));
            if !query.is_empty() {
                builder = builder.query(query);
            }
            if let Some(payload) = &body {
                builder = builder.json(payload);
            }

            let response = match builder.send().await {
                Ok(response) => response,
                Err(error) => {
                    last_error = Some(anyhow!("飞书接口请求失败: {error}"));
                    sleep_backoff(attempt).await;
                    continue;
                }
            };

            let status = response.status();
            let retry_after = response
                .headers()
                .get("Retry-After")
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse::<f64>().ok());

            // 可重试的服务端状态（429 / 5xx）必须在解析响应体**之前**处理：
            // 这类响应体往往是网关或反向代理返回的 HTML 错误页，根本不是 JSON。
            // 若先解析，就会把这个本可重试的瞬时故障变成一次硬失败，也就丢掉了
            // 原有的重试语义（HEAD 用 `{"raw": text}` 兜底时靠重试侥幸掩盖了这一点）。
            if status.as_u16() == 429 || status.is_server_error() {
                last_error = Some(anyhow!("飞书接口暂时不可用（HTTP {status}）"));
                match retry_after {
                    Some(seconds) => {
                        tokio::time::sleep(Duration::from_secs_f64(seconds.min(30.0))).await
                    }
                    None => sleep_backoff(attempt).await,
                }
                continue;
            }

            let text = response.text().await.unwrap_or_default();
            let trimmed = text.trim();
            // 空响应体：当作成功（某些删除类接口返回 204，省掉一层特判）。
            // 非空但不是 JSON：一律判失败。此前用 `{"raw": text}` 兜底，会让
            // `code` 落到默认值 0，把一个 HTML 错误页/网关页当成成功返回。
            let data: serde_json::Value = if trimmed.is_empty() {
                json!({})
            } else {
                serde_json::from_str(trimmed).map_err(|error| {
                    anyhow!(
                        "飞书接口返回的不是 JSON（HTTP {status}）: {}（响应片段: {}）",
                        error,
                        snippet(trimmed)
                    )
                })?
            };
            let code = data
                .get("code")
                .and_then(|value| value.as_i64())
                .unwrap_or(0);

            if matches!(status.as_u16(), 401 | 403) || TOKEN_EXPIRED_CODES.contains(&code) {
                *self.token.lock().await = None;
                continue;
            }

            if TRANSIENT_CODES.contains(&code) {
                last_error = Some(anyhow!("飞书接口暂时不可用（HTTP {status}, code {code}）"));
                match retry_after {
                    Some(seconds) => {
                        tokio::time::sleep(Duration::from_secs_f64(seconds.min(30.0))).await
                    }
                    None => sleep_backoff(attempt).await,
                }
                continue;
            }

            if !status.is_success() {
                return Err(anyhow!("飞书接口 HTTP 错误: {status}"));
            }
            if code != 0 {
                let message = data
                    .get("msg")
                    .and_then(|value| value.as_str())
                    .unwrap_or("未知错误");
                return Err(anyhow!("飞书接口错误 code={code}: {message}"));
            }
            return Ok(data);
        }

        Err(last_error.unwrap_or_else(|| anyhow!("飞书接口重试后仍失败")))
    }

    /// 分页读取全部数据表。
    pub async fn list_tables(&self, app_token: &str) -> Result<Vec<serde_json::Value>> {
        self.paginate(&format!("/bitable/v1/apps/{app_token}/tables"), None)
            .await
    }

    /// 分页读取指定表的全部字段。
    pub async fn list_fields(
        &self,
        app_token: &str,
        table_id: &str,
    ) -> Result<Vec<serde_json::Value>> {
        self.paginate(
            &format!("/bitable/v1/apps/{app_token}/tables/{table_id}/fields"),
            None,
        )
        .await
    }

    /// 分页读取现有记录，只取指定字段以减小响应体积。
    pub async fn list_records(
        &self,
        app_token: &str,
        table_id: &str,
        field_names: &[String],
    ) -> Result<Vec<serde_json::Value>> {
        let encoded = serde_json::to_string(field_names).context("字段名序列化失败")?;
        self.paginate(
            &format!("/bitable/v1/apps/{app_token}/tables/{table_id}/records"),
            Some(vec![("field_names".to_string(), encoded)]),
        )
        .await
    }

    async fn paginate(
        &self,
        path: &str,
        extra: Option<Vec<(String, String)>>,
    ) -> Result<Vec<serde_json::Value>> {
        let mut items: Vec<serde_json::Value> = Vec::new();
        let mut page_token: Option<String> = None;

        for _ in 0..MAX_PAGES {
            let mut query = vec![("page_size".to_string(), "500".to_string())];
            if let Some(extra) = &extra {
                query.extend(extra.iter().cloned());
            }
            if let Some(token) = &page_token {
                query.push(("page_token".to_string(), token.clone()));
            }

            let data = self
                .request(reqwest::Method::GET, path, &query, None)
                .await?;
            let page = data.get("data").cloned().unwrap_or_else(|| json!({}));

            if let Some(list) = page.get("items").and_then(|value| value.as_array()) {
                items.extend(list.iter().cloned());
            }

            let has_more = page
                .get("has_more")
                .and_then(|value| value.as_bool())
                .unwrap_or(false);
            if !has_more {
                return Ok(items);
            }

            match page.get("page_token").and_then(|value| value.as_str()) {
                Some(token) if !token.is_empty() => {
                    // 游标没前进说明服务端不认这个 page_token，继续翻会死循环。
                    if page_token.as_deref() == Some(token) {
                        return Err(anyhow!(
                            "飞书分页游标未推进（page_token={token}），已中止以避免死循环"
                        ));
                    }
                    page_token = Some(token.to_string());
                }
                _ => return Err(anyhow!("飞书分页响应缺少 page_token")),
            }
        }

        Err(anyhow!("飞书分页超过 {MAX_PAGES} 页，已中止"))
    }
}

/// 取一段足够诊断、又不会把整页 HTML 塞进报错的响应片段。
fn snippet(text: &str) -> String {
    let mut out: String = text.chars().take(200).collect();
    if text.chars().count() > 200 {
        out.push('…');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_full_base_url() {
        let target = parse_base_url(
            "https://example.feishu.cn/base/BakExampleAppToken001?table=tblExampleTable01&view=vewExampleView01",
        )
        .unwrap();
        assert_eq!(target.app_token, "BakExampleAppToken001");
        assert_eq!(target.table_id, "tblExampleTable01");
        assert_eq!(target.view_id, "vewExampleView01");
    }

    #[test]
    fn accepts_tbl_alias_and_bare_token() {
        let target =
            parse_base_url("https://x.feishu.cn/base/BakExampleAppToken001?tbl=tblAbc123456")
                .unwrap();
        assert_eq!(target.table_id, "tblAbc123456");

        let bare = parse_base_url("BakExampleAppToken001").unwrap();
        assert_eq!(bare.app_token, "BakExampleAppToken001");
        assert!(bare.table_id.is_empty());
    }

    #[test]
    fn rejects_unsafe_urls() {
        assert!(parse_base_url("http://example.feishu.cn/base/BakExampleAppToken001").is_err());
        assert!(parse_base_url("https://evil.com/base/BakExampleAppToken001").is_err());
        assert!(parse_base_url("https://example.feishu.cn/wiki/BakExampleAppToken001").is_err());
        assert!(parse_base_url("https://example.feishu.cn/base/short").is_err());
    }

    #[test]
    fn empty_input_is_not_an_error() {
        assert_eq!(parse_base_url("   ").unwrap(), BaseTarget::default());
    }
}
