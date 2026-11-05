# 安装、部署与更新

[返回首页](../README.md)

本程序运行在能连接客户 SQL Server 的电脑或服务器上。每个客户使用独立的项目目录和配置，不要复制其他客户的密钥。

## 开始前准备

| 需要准备 | 具体内容 |
| --- | --- |
| 电脑 | Windows、macOS 或 Linux，安装 Git 和 Python 3.10 或更高版本 |
| SQL Server | 服务器地址、端口、数据库名、用户名和密码，账号可读取目标数据库的全部待同步表 |
| 飞书应用 | 自建应用的 App ID 和 App Secret |
| 飞书多维表格 | 用于接收数据的多维表格完整网页链接，应用具备相应访问及写入权限 |
| 网络与目录 | 部署机器能访问 SQL Server、GitHub、Python 包源和飞书接口，项目目录对运行用户可写 |

> [!TIP]
> 请让数据库管理员准备只有目标表或视图 `SELECT` 权限的专用只读账号，不要使用 `sa`。

## 命令行完整安装流程

下面从获取代码开始，明确创建虚拟环境、安装依赖和启动程序的每一步。命令直接调用虚拟环境内的 Python。

### Windows PowerShell

先安装 Git 和 Python 3.10 或更高版本，打开 PowerShell 后逐行执行。

```powershell
git clone https://github.com/Guyungy/SqlSeverToFeishu.git
cd SqlSeverToFeishu
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe dashboard.py
```

如果电脑只有 `python` 命令，将上面的 `py -3` 替换成 `python`。已完成 Git 下载时，从 `cd SqlSeverToFeishu` 开始执行。

### macOS / Linux

先安装 Git 和 Python 3.10 或更高版本。Ubuntu 可先执行 `sudo apt install -y git python3-full python3-pip`，然后逐行执行以下命令。

```bash
git clone https://github.com/Guyungy/SqlSeverToFeishu.git
cd SqlSeverToFeishu
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python dashboard.py
```

启动成功后，打开 [本机管理页面](http://127.0.0.1:5001)。以后启动时，进入原项目目录，执行对应系统的最后一行命令即可，无需重复创建虚拟环境。

Windows、macOS、Linux 各自创建自己的 `.venv`。跨系统复制项目时，请排除 `.venv`，在目标系统重新安装依赖。项目目录需要当前运行用户有写入权限，用于保存配置、游标和日志。

> [!IMPORTANT]
> 启动窗口不能关闭，关闭后本地服务和定时同步都会停止。浏览器没有自动打开时，请访问 <http://127.0.0.1:5001>。

## Ubuntu 无桌面服务器

在服务器的项目目录启动，关闭自动打开浏览器：

```bash
NO_AUTO_BROWSER=1 .venv/bin/python dashboard.py
```

在自己电脑另开终端建立 SSH 隧道，将 `用户名`、`服务器IP` 替换为实际值：

```bash
ssh -N -L 15001:127.0.0.1:5001 用户名@服务器IP
```

保持 SSH 隧道运行，用自己电脑的浏览器访问 <http://127.0.0.1:15001>。以上按默认服务端口 5001 举例，修改服务端口后需要对应修改隧道目标端口。

当前项目没有提供 systemd 服务安装配置。服务器上长期运行需要额外配置进程托管和开机启动；直接在 SSH 会话里启动时，会话断开可能导致程序退出。

## 使用 Git 更新客户环境

先停止正在运行的程序，把项目目录中的 `.env`、`sync_config.json` 和 `sync_state.json` 备份到项目目录之外。这些文件分别保存客户连接配置、同步任务和同步状态。首次使用时，部分文件可能尚未生成，只需备份已有文件。

在原项目目录执行以下命令。

```bash
git status
git pull --ff-only origin main
```

如果 `git status` 显示本地代码改动，先确认并处理这些改动。如果拉取因冲突或分支分叉停止，保留现场并由维护人员处理。`.env`、`sync_config.json` 和 `sync_state.json` 已由仓库忽略规则排除，正常拉取会保留这些客户配置。

更新依赖后重新启动程序。Windows 在 PowerShell 中执行以下命令。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe dashboard.py
```

macOS 或 Linux 执行以下命令。

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python dashboard.py
```

启动后刷新管理页面，确认飞书连接和所选数据库，执行预检。需要补上数据库中新增加的表时，在“同步计划”点击“读取数据库表”，调整后保存同步计划。

## 当前跨平台验证状态

- Python 网页版统一使用虚拟环境中的 Python 启动；Windows 和 Ubuntu 尚未完成实机运行验收。
- 保存密钥已增加 Windows 文件权限兼容处理，仍需 Windows 实机验证。
- Windows 同步锁已改用只读进程状态查询，停止任务覆盖启动阶段并增加超时强制停止，仍需 Windows 实机验证。
- 新增的计划调整、大数据暂存和停止流程已通过本地自动化测试，客户真实数据库及 Windows/Ubuntu 实机验收仍待完成。桌面版功能进度见[开发与同步原理](development.md)。

---

下一步：[配置数据库并开始同步](usage.md) · [飞书应用授权](feishu-permissions.md) · [故障排查](troubleshooting.md)
