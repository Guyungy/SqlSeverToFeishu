//! 桌面版入口。
//!
//! 与命令行版共用 `sqlserver-to-feishu` 内核；这里只负责准备配置目录、
//! 注册命令、起窗口。

mod commands;

use sqlserver_to_feishu::config::AppPaths;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // 桌面版必须落在用户数据目录，不能用 exe 同目录：
    // macOS 上应用的可执行文件在 `SqlFeishuSync.app/Contents/MacOS/` 内，
    // 那里对用户可写，于是配置会被写进应用包本身，重装或升级应用时
    // 连同用户配置一起被覆盖掉。
    let paths = AppPaths::user_data().expect("无法准备配置目录");

    // 内核的飞书客户端与 SQL 连接都从进程环境读凭据，这里先把 .env 灌进去。
    if let Err(error) = paths.reload_env() {
        eprintln!("读取 .env 失败: {error:#}");
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(commands::AppState { paths })
        .invoke_handler(tauri::generate_handler![
            commands::workspace,
            commands::save_feishu,
            commands::test_feishu,
            commands::save_settings,
            commands::save_source,
            commands::delete_source,
            commands::test_source,
            commands::list_databases,
            commands::list_tables,
            commands::list_columns,
            commands::save_job,
            commands::delete_job,
            commands::job_states,
            commands::reset_job_state,
            commands::tail_log,
        ])
        .run(tauri::generate_context!())
        .expect("启动桌面应用失败");
}
