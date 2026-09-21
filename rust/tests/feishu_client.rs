//! 飞书客户端的集成测试：跑真实的 HTTP 往返，但服务端是本地桩。
//!
//! 覆盖的是 Python 版里最容易出错的几段逻辑——它们都只在异常路径上才暴露：
//! 分页拼接、限流退避、令牌失效后重新取号、业务错误码必须直接抛出。
//! 这几段用单元测试覆盖不到，因为要靠一个会说协议的对手方才能触发。

mod common;

use common::StubServer;
use sqlserver_to_feishu::feishu::FeishuClient;

fn client(server: &StubServer) -> FeishuClient {
    FeishuClient::with_api_base(
        "cli_integration_test",
        "secret_integration_test",
        format!("http://127.0.0.1:{}/open-apis", server.port),
    )
    .expect("构造飞书客户端失败")
}

#[tokio::test]
async fn paginates_all_pages() {
    let server = StubServer::start("feishu_mock.py", &[]);
    let client = client(&server);

    let tables = client
        .list_tables("appPaging00000001")
        .await
        .expect("分页读取失败");

    let names: Vec<&str> = tables
        .iter()
        .filter_map(|table| table.get("name").and_then(|value| value.as_str()))
        .collect();
    assert_eq!(names, vec!["第一张表", "第二张表", "第三张表"]);

    let log = server.log();
    assert_eq!(
        log.matches("/tables?").count(),
        2,
        "两页数据应当请求两次: {log}"
    );
    assert!(
        log.contains("page_token=page-2"),
        "第二页必须带上服务端给的 page_token: {log}"
    );
}

#[tokio::test]
async fn retries_after_rate_limit() {
    let server = StubServer::start("feishu_mock.py", &[]);
    let client = client(&server);

    let tables = client
        .list_tables("appRetry429000001")
        .await
        .expect("首次被限流后应当退避重试成功");
    assert_eq!(tables.len(), 1);

    let log = server.log();
    assert!(log.contains("-> 429"), "首次应当真的被限流: {log}");
    assert_eq!(
        log.matches("/tables?").count(),
        2,
        "限流后应当重试一次: {log}"
    );
}

#[tokio::test]
async fn refreshes_expired_token() {
    let server = StubServer::start("feishu_mock.py", &[]);
    let client = client(&server);

    let tables = client
        .list_tables("appStaleTkn0000001")
        .await
        .expect("令牌失效后应当重新取号再重试");
    assert_eq!(tables.len(), 1);

    let log = server.log();
    assert!(
        log.contains("code=99991663"),
        "桩应当返回过一次令牌失效: {log}"
    );
    assert_eq!(
        log.matches("/internal").count(),
        2,
        "令牌失效后必须重新取号（含首次共两次）: {log}"
    );
}

#[tokio::test]
async fn propagates_business_error_without_retry() {
    let server = StubServer::start("feishu_mock.py", &[]);
    let client = client(&server);

    let error = client
        .list_tables("appBizErr00000001")
        .await
        .expect_err("业务错误码必须直接抛出");
    let message = format!("{error:#}");
    assert!(
        message.contains("1254005") && message.contains("app not found"),
        "错误码与原因都应当带出: {message}"
    );

    let log = server.log();
    assert_eq!(
        log.matches("/tables?").count(),
        1,
        "业务错误不该被当成暂时性故障反复重试: {log}"
    );
}

#[tokio::test]
async fn lists_fields_and_sends_bearer_token() {
    let server = StubServer::start("feishu_mock.py", &[]);
    let client = client(&server);

    let fields = client
        .list_fields("appNormal00000001", "tblA")
        .await
        .expect("字段读取失败");
    assert_eq!(fields.len(), 2);
    assert_eq!(
        fields[0].get("field_name").and_then(|value| value.as_str()),
        Some("订单号")
    );

    let log = server.log();
    assert!(
        log.contains("/fields") && log.contains("auth=yes"),
        "字段请求应当带上 Bearer 令牌: {log}"
    );
    assert_eq!(
        log.matches("/internal").count(),
        1,
        "同一客户端只应取号一次（令牌要缓存）: {log}"
    );
}
