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
    //
    // 这里刻意不用 `.expect()`：release 开了 `panic = "abort"`，一旦失败
    // 会直接中止且不给任何解释。改成打印可读错误再退出，用户至少知道
    // 是「配置目录准备失败」而不是窗口莫名其妙不见了。
    let paths = match AppPaths::user_data() {
        Ok(paths) => paths,
        Err(error) => {
            eprintln!("无法准备配置目录: {error:#}");
            std::process::exit(1);
        }
    };

    // 启动阶段仍是单线程，此处的 `set_var` 是安全的；运行起来之后一律
    // 走 `AppPaths::env_map()` 现读 `.env` 文件，不再回写进程环境。
    if let Err(error) = paths.load_env() {
        eprintln!("读取 .env 失败: {error:#}");
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(commands::AppState::new(paths))
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
        .unwrap_or_else(|error| {
            eprintln!("启动桌面应用失败: {error:#}");
            std::process::exit(1);
        });
}
