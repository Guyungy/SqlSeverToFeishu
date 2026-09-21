// 发行版下不要额外弹出控制台窗口（仅影响 Windows）。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    sqlfeishu_desktop_lib::run()
}
