# SQL Server → 飞书多维表格 同步工具

一个**本地运行的 Web 工具**，把 SQL Server 数据库里的订单/业务数据，同步（导入）到**飞书多维表格**，并按业务主键自动去重（新增的加进去，已有的更新）。

> 一句话：**不用再手工把数据库数据搬进飞书了，点几下按钮自动同步。**

---

## 这个工具是做什么用的？

**典型场景**：你的订单数据存在公司的 SQL Server 数据库里，但日常协同（看进度、跟单、汇报）用的是飞书多维表格。以前只能每次手动导出 CSV 再传飞书；现在启动这个小工具，填好两边连接信息，点「开始同步」，数据就自动进飞书了。

**它解决什么问题：**
- 🔄 把 SQL Server 数据**自动**同步到飞书，省去手工导出/导入
- ♻️ **自动去重**：按「订单号」判断——新订单自动新增，已存在的订单自动更新
- 📊 全程可视化：有网页配置面板、连接测试、进度条和实时日志

**它不做什么（重要边界）：**
- ⚠️ **仅单向**：数据从 SQL Server → 飞书，不会把飞书数据写回数据库
- ⚠️ **不删除**：只做「新增 + 更新」，绝不会删飞书里的任何记录
- ⚠️ **手动触发**：当前版本需手动点「开始同步」，非定时自动跑

---

## 功能特性

- 步骤式向导：数据库配置 → 飞书配置 → 字段映射 → 执行同步
- 一键测试 SQL Server 和飞书连接（先测通，再同步）
- 实时同步进度条 + 日志
- 按订单号去重，自动判断新增/更新
- 按 500 条/批 批量写入飞书，大数据量也能跑

## 环境要求

| 依赖 | 说明 |
| --- | --- |
| Python 3.9+ | 运行环境（脚本可自动建独立环境） |
| SQL Server | 本地或内网，需开启 TCP/IP 协议 |
| 飞书自建应用 + 多维表格 | 需要 App ID / Secret 和 Table ID |

## 项目结构

| 文件 | 作用 |
| --- | --- |
| `dashboard.py` | 网页配置面板主程序 |
| `sync.py` | 后台同步数据的核心脚本 |
| `mapping.json` | SQL 列 → 飞书字段 的映射配置 |
| `.env.example` | 配置模板（复制为 `.env` 填写） |
| `requirements.txt` | Python 依赖清单 |
| `install.sh` / `install.bat` / `install.command` | 一键安装依赖 |
| `start.sh` / `start.bat` / `start.command` | 启动程序 |
| `使用手册（小白版）.md` | 零基础使用文档（推荐先看这个） |

---

## 安装与启动

> 🧑‍💻 **零基础用户**：请直接看《使用手册（小白版）.md》，含一步一步的图文说明。

### 一键安装（推荐）

1. **安装依赖**（仅首次）：
   - Windows：双击 `install.bat`
   - macOS / Linux：跑 `./install.sh`（或双击 `install.command`）
2. **启动面板**：
   - Windows：双击 `start.bat`
   - macOS：双击 `start.command`（或用 `./start.sh`）
3. 浏览器打开：`http://127.0.0.1:5001`

> 💡 启动脚本会自动选择 Python：优先用项目独立环境 `.venv`；没有则自动探测本机装了依赖的系统 Python，无需手动配置。

### 手动安装（懂命令行）

```bash
cd <项目目录>
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # macOS/Linux
.venv/Scripts/pip install -r requirements.txt  # Windows
.venv/bin/python dashboard.py               # 启动
```

---

## 使用步骤

1. **数据库配置**：填 SQL Server 连接信息和数据表名，点「测试」
2. **飞书配置**：填 App ID / Secret / Table ID，点「测试」
3. **字段映射**：确认 SQL 列和飞书字段的对应关系（一般不用改）
4. **执行同步**：点「开始同步」，看进度条和日志，完成后去飞书查看

## 配置项说明

| 配置项 | 说明 |
| --- | --- |
| DB_SERVER | SQL Server 地址，如 `localhost` 或 `localhost\\SQLEXPRESS` |
| DB_PORT | 端口，默认 1433 |
| DB_NAME | 数据库名 |
| DB_USER | 用户名，Windows 认证可留空 |
| DB_PASSWORD | 密码，Windows 认证可留空 |
| DB_TABLE | 数据表名或视图名，如 `dbo.Orders` |
| FEISHU_APP_ID | 飞书自建应用 App ID |
| FEISHU_APP_SECRET | 飞书自建应用 App Secret |
| FEISHU_BASE_TABLE_ID | 目标多维表格 Table ID |

## 常见问题

### SQL Server 连接失败，提示 `DB-Lib error 20009`
- SQL Server 服务已启动
- 已启用 TCP/IP，端口为 1433
- 防火墙未拦截
- 命名实例填 `localhost\\SQLEXPRESS`

### 飞书连接失败
- App ID / Secret 是否正确
- 应用是否**发布了版本**、是否**关联到目标多维表格**
- Table ID（`tbl` 开头）是否正确

### 双击启动器报错：找不到 Python / 依赖
- 先运行 `install.bat` / `install.command` 自动建环境并装依赖
- 详细排查见《使用手册（小白版）.md》「常见问题 #0」

---

## 注意事项

- 仅本地运行，不暴露公网端口，数据库密码保存在本地 `.env`
- 默认端口 5001，被占用可改 `start.sh` / `start.bat` 里的 `DASHBOARD_PORT`
- `.env` 含连接密钥，**不要提交到仓库或对外分享**