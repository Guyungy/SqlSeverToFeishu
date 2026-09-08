# SQL Server → 飞书多维表格 同步工具

一个本地运行的 Web 配置面板，用于把 SQL Server 里的订单数据同步到飞书多维表格。

## 功能

- 步骤式向导：SQL Server 配置 → 飞书配置 → 字段映射 → 执行同步
- 一键测试 SQL Server 和飞书连接
- 实时同步进度条和日志
- 按订单号去重，自动判断新增/更新

## 环境要求

- Python 3.9+
- SQL Server（本地或内网，需开启 TCP/IP）
- 飞书自建应用 + 多维表格

## 安装与启动

> 小白用户请看《使用手册（小白版）.md》，里面含完整图文步骤。

### 一键安装（推荐）

1. 安装依赖（仅首次）：
   - Windows：双击 `install.bat`
   - macOS/Linux：`./install.sh`

2. 启动面板：
   - Windows：双击 `start.bat`
   - macOS：双击 `start.command`（或用 `./start.sh`）

3. 浏览器打开：`http://127.0.0.1:5001`

### 手动安装

```bash
cd <项目目录>
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # macOS/Linux
.venv/Scripts/pip install -r requirements.txt  # Windows
.venv/bin/python dashboard.py               # 启动
```

## 使用步骤

1. 在「数据库配置」页面填写 SQL Server 连接信息，点击测试。
2. 在「飞书配置」页面填写 App ID、App Secret 和 Table ID，点击测试。
3. 确认「字段映射」页面中的字段对应关系。
4. 在「执行同步」页面点击「开始同步」，查看进度和日志。

## 配置项说明

| 配置项 | 说明 |
| --- | --- |
| DB_SERVER | SQL Server 地址，例如 `localhost` 或 `localhost\\SQLEXPRESS` |
| DB_PORT | 端口，默认 1433 |
| DB_NAME | 数据库名 |
| DB_USER | 用户名，Windows 认证可留空 |
| DB_PASSWORD | 密码，Windows 认证可留空 |
| DB_TABLE | 数据表名或视图名 |
| FEISHU_APP_ID | 飞书自建应用 App ID |
| FEISHU_APP_SECRET | 飞书自建应用 App Secret |
| FEISHU_BASE_TABLE_ID | 目标多维表格 Table ID |

## 常见问题

### SQL Server 连接失败，提示 `DB-Lib error 20009`

说明程序无法访问 SQL Server。请检查：

- SQL Server 服务已启动
- 已启用 TCP/IP，且端口为 1433
- 防火墙没有拦截
- 如果是命名实例（如 `SQLEXPRESS`），地址应填写为 `localhost\\SQLEXPRESS`

### 飞书连接失败

- 确认 App ID 和 App Secret 正确
- 确认应用已开通「多维表格」权限
- 确认 Table ID 正确

## 注意事项

- 当前仅支持在本地运行，不会暴露公网端口。
- 默认使用 5001 端口，如果被占用可修改 `start.sh` 或 `start.bat` 中的 `DASHBOARD_PORT`。
