//! SQL Server 到飞书多维表格同步的核心库。
//!
//! 拆出 lib target 的目的有两个：
//! 1. 让 `tests/` 下的集成测试能直接调用 `config` / `feishu` / `mssql`，
//!    而不是只能对着命令行做黑盒测试；
//! 2. 后续补同步主逻辑和网页面板时，核心逻辑不必挤在 `main.rs` 里。
//!
//! 二进制入口在 `main.rs`，只负责参数解析与输出编排。

// 同步主逻辑尚未接线，config / feishu 里已备好的接口暂时无人调用。
// 整体放行未使用告警，避免噪音盖住真正的编译错误。
#![allow(dead_code)]

pub mod config;
pub mod feishu;
pub mod mssql;
