//! 集成测试共用的桩服务启动器。
//!
//! 桩服务用 Python 写（`tests/fixtures/*.py`）：它们只做 HTTP / TLS 这类
//! 协议行为，不需要 Rust 侧的额外依赖，改动成本也低。
//!
//! 每个桩都在 `127.0.0.1:0` 上监听，把真实端口打到标准输出，
//! 测试读回来才知道该连哪里——避免硬编码端口导致并行跑测试时撞车。

#![allow(dead_code)]

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::time::Duration;

/// 桩服务用的 Python 解释器。
///
/// 不能硬编码 `/usr/bin/python3`：Windows 上没有这个路径，GitHub 的
/// windows runner 里是 `python`。需要时可用 `SQLFEISHU_TEST_PYTHON` 覆盖。
pub fn python_binary() -> String {
    if let Ok(value) = std::env::var("SQLFEISHU_TEST_PYTHON") {
        if !value.trim().is_empty() {
            return value.trim().to_string();
        }
    }
    if cfg!(windows) {
        "python".to_string()
    } else {
        "/usr/bin/python3".to_string()
    }
}

static SEQ: AtomicUsize = AtomicUsize::new(0);

pub struct StubServer {
    child: Child,
    pub port: u16,
    pub log_path: PathBuf,
}

impl StubServer {
    /// 启动 `tests/fixtures/<script>`，返回已就绪的桩。
    pub fn start(script: &str, extra_args: &[String]) -> Self {
        let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures")
            .join(script);
        assert!(fixture.exists(), "找不到桩脚本 {}", fixture.display());

        let seq = SEQ.fetch_add(1, Ordering::SeqCst);
        let log_path = std::env::temp_dir().join(format!(
            "sqlfeishu-stub-{}-{seq}.log",
            std::process::id()
        ));
        let _ = std::fs::remove_file(&log_path);

        // 约定：所有桩脚本的第一个参数都是日志文件，自定义参数排在后面。
        let mut args: Vec<String> = vec![fixture.display().to_string(), log_path.display().to_string()];
        args.extend_from_slice(extra_args);

        let mut child = Command::new(python_binary())
            .args(&args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap_or_else(|error| panic!("启动桩服务失败: {error}"));

        let stdout = child.stdout.take().expect("桩服务标准输出不可用");

        // 用一个带超时的通道读端口：桩脚本若启动失败，
        // 直接 read_line 会永久阻塞，测试就挂死而不是失败。
        let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            let outcome = reader.read_line(&mut line).map(|_| line);
            let _ = sender.send(outcome);
            // 继续抽干后续输出，避免管道写满把桩进程堵住。
            let mut sink = String::new();
            while reader.read_line(&mut sink).map(|n| n > 0).unwrap_or(false) {
                sink.clear();
            }
        });

        let port = match receiver.recv_timeout(Duration::from_secs(15)) {
            Ok(Ok(line)) => line
                .trim()
                .strip_prefix("PORT ")
                .and_then(|value| value.parse::<u16>().ok())
                .unwrap_or_else(|| panic!("桩服务未打印端口，实际输出: {line:?}")),
            Ok(Err(error)) => panic!("读取桩服务输出失败: {error}"),
            Err(_) => panic!("桩服务 15 秒内未启动"),
        };

        Self {
            child,
            port,
            log_path,
        }
    }

    /// 读回桩记录的行为日志；每个请求都即时 flush，所以不需要额外等待。
    pub fn log(&self) -> String {
        std::fs::read_to_string(&self.log_path).unwrap_or_default()
    }

    /// 等待日志里出现指定次数，最多等 `timeout`。
    /// 用于「客户端已经返回、但服务端线程还在收尾」的场景。
    pub fn wait_for_log_count(&self, needle: &str, expected: usize, timeout: Duration) -> String {
        let deadline = std::time::Instant::now() + timeout;
        loop {
            let text = self.log();
            if text.matches(needle).count() >= expected || std::time::Instant::now() >= deadline {
                return text;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

impl Drop for StubServer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_file(&self.log_path);
    }
}

/// 找一个可用的 openssl 可执行文件（只在需要生成自签证书的 TLS 用例里用到）。
///
/// 返回 `None` 表示本机没有 —— Windows runner 上就是这种情况。
/// 调用方应当**跳过**该用例而不是判定失败：TLS 握手逻辑本身与平台无关，
/// 而且证书能被正确接受/拒绝已经在 macOS 与 Linux 上覆盖过了。
pub fn openssl_binary() -> Option<String> {
    for candidate in [
        "/opt/homebrew/opt/openssl@3/bin/openssl",
        "/usr/local/opt/openssl@3/bin/openssl",
        "/usr/bin/openssl",
    ] {
        if std::path::Path::new(candidate).exists() {
            return Some(candidate.to_string());
        }
    }
    // PATH 里能直接找到也算（Linux 常见）。
    if Command::new("openssl")
        .arg("version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
    {
        return Some("openssl".to_string());
    }
    None
}

/// 每个用例独享的临时目录：桩脚本要在里面生成自签证书，
/// 共用一个目录会让并行用例互相覆盖。
pub fn unique_temp_dir(tag: &str) -> PathBuf {
    let seq = SEQ.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("sqlfeishu-{tag}-{}-{seq}", std::process::id()));
    std::fs::create_dir_all(&dir).expect("创建临时目录失败");
    dir
}

/// 打开 tiberius 的 TRACE 日志（连接类问题只看最终报错定位不到）。
///
/// 默认关闭，`SQLFEISHU_TEST_LOG=1 cargo test -- --nocapture` 时才输出，
/// 免得正常跑测试被日志淹没。
pub fn init_logging() {
    if std::env::var("SQLFEISHU_TEST_LOG").is_err() {
        return;
    }
    use std::sync::Once;
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        let _ = tracing_subscriber::fmt()
            .with_max_level(tracing::Level::TRACE)
            .with_test_writer()
            .try_init();
    });
}
