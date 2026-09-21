//! SQL Server 连接层的集成测试：只验证加密协商与 TLS 这一层。
//!
//! 为什么只测这一层：从 pymssql 换到 tiberius-ng，认证、SQL 语法、
//! 元数据查询都是有官方保证的常规能力，**唯一**有实质不确定性的是
//! 「rustls 能不能和 SQL Server 的自签证书完成握手」。
//!
//! 桩服务（`tests/fixtures/tds_stub.py`）完成明文 PRELOGIN 应答后开始 TLS，
//! 随后就不再应答。于是两种结果的含义很明确：
//!   - 握手成功、之后在 TDS 登录阶段报错 → TLS 这条路通了
//!   - 握手失败                          → 需要调 TLS 配置
//!
//! 另外两个用例分别证明：证书校验是真的在生效（而不是被静默关掉）；
//! 以及服务器不支持加密时客户端会明确拒绝，而不是悄悄降级。

mod common;

use std::time::Duration;

use common::{openssl_binary, unique_temp_dir, StubServer};
use sqlserver_to_feishu::config::DataSource;
use sqlserver_to_feishu::mssql;

/// 证书校验失败时客户端报错里会出现的特征词。
/// 命中任何一个都说明失败发生在 TLS 层，而非 TDS 协议层。
const CERT_ERROR_HINTS: [&str; 5] = [
    "certificate",
    "UnknownIssuer",
    "invalid peer",
    "CertNotValidForName",
    "bad certificate",
];

fn source(port: u16) -> DataSource {
    DataSource {
        id: "source_tls_test".to_string(),
        name: "TDS 桩".to_string(),
        server: "127.0.0.1".to_string(),
        port,
        database: String::new(),
        user: "sa".to_string(),
        password_env: "SQLFEISHU_TEST_PASSWORD".to_string(),
        enabled: true,
    }
}

/// 启动桩。本机没有 openssl 时返回 `None`（Windows runner 就是这种情况），
/// 调用方跳过用例而不是判失败 —— 见 `common::openssl_binary` 的说明。
fn start_stub(tag: &str, mode: &str) -> Option<StubServer> {
    let openssl = openssl_binary()?;
    let cert_dir = unique_temp_dir(tag);
    Some(StubServer::start(
        "tds_stub.py",
        &[cert_dir.display().to_string(), openssl, mode.to_string()],
    ))
}

#[tokio::test]
async fn trusted_self_signed_cert_completes_tls_handshake() {
    common::init_logging();
    let Some(server) = start_stub("tds-trust", "encrypt") else {
        eprintln!("跳过 trusted_self_signed_cert_completes_tls_handshake：本机没有 openssl");
        return;
    };

    let error = mssql::connect_with(&source(server.port), true)
        .await
        .expect_err("桩不会完成 TDS 登录，连接必须报错");
    let message = format!("{error:#}");

    let log = server.wait_for_log_count("ok", 1, Duration::from_secs(15));
    assert!(
        log.contains("ok"),
        "带 trust_cert 时 rustls 应当能完成自签证书握手；桩记录 {log:?}，客户端报错: {message}"
    );
    for hint in CERT_ERROR_HINTS {
        assert!(
            !message.contains(hint),
            "失败应当落在 TDS 登录阶段而不是证书校验层，但报错里出现了 {hint:?}: {message}"
        );
    }
}

#[tokio::test]
async fn rejects_self_signed_cert_when_verification_is_on() {
    let Some(server) = start_stub("tds-strict", "encrypt") else {
        eprintln!("跳过 rejects_self_signed_cert_when_verification_is_on：本机没有 openssl");
        return;
    };

    let error = mssql::connect_with(&source(server.port), false)
        .await
        .expect_err("严格校验下自签证书必须被拒绝");
    let message = format!("{error:#}");

    let log = server.wait_for_log_count("fail", 1, Duration::from_secs(15));
    assert!(
        log.contains("fail"),
        "桩应当记录到握手失败；实际记录 {log:?}，客户端报错: {message}"
    );
}

#[tokio::test]
async fn refuses_to_continue_when_server_disables_encryption() {
    let Some(server) = start_stub("tds-plain", "plain") else {
        eprintln!("跳过 refuses_to_continue_when_server_disables_encryption：本机没有 openssl");
        return;
    };

    let error = mssql::connect_with(&source(server.port), true)
        .await
        .expect_err("服务器不支持加密时必须拒绝继续，而不是明文登录");
    let message = format!("{error:#}");

    let log = server.wait_for_log_count("plain", 1, Duration::from_secs(15));
    assert!(
        log.contains("plain"),
        "桩应当走到「回应不支持加密」这一步；实际记录 {log:?}，客户端报错: {message}"
    );
    assert!(
        !log.contains("ok"),
        "客户端不应在服务器拒绝加密后仍然继续: {log}"
    );
}
