<div align="center">

<img src="src-tauri/icons/icon.png" width="80" alt="数据库 → 飞书同步中心">

# 数据库 → 飞书同步中心

选择数据库，批量把业务表同步到飞书多维表格。支持 SQL Server、PostgreSQL、SQLite，已接入 MySQL/MariaDB 适配。[兼容范围与验证状态](docs/databases.md)。

中文管理界面 · 批量建任务 · 增量同步 · 自动分表

</div>

连接数据库后，默认选中全部表和字段，可取消不需要的表，再批量建立同步任务。已有数据按唯一键新增或更新，支持定时运行。

## 1. 安装并启动

准备 Git、Python 3.10 或更高版本，以及以下信息：

- 数据库类型、地址、端口、库名和只读账号密码。SQLite 填写本机文件路径。
- 飞书自建应用的 App ID、App Secret，以及目标多维表格链接。首次使用先完成[飞书应用授权](docs/feishu-permissions.md)。

在能访问数据库和飞书接口的电脑上，打开终端获取代码：

```bash
git clone https://github.com/Guyungy/SqlSeverToFeishu.git
cd SqlSeverToFeishu
```

选择对应系统，创建虚拟环境、安装依赖并启动。

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe dashboard.py
```

如果没有 `py` 命令，将 `py -3` 替换成 `python`。

<details>
<summary>macOS / Linux 命令</summary>

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python dashboard.py
```

Ubuntu 缺少虚拟环境支持时，先执行 `sudo apt install -y python3-full python3-pip`。无桌面服务器等部署方式见[部署指南](docs/deployment.md)。

</details>

打开 [本机管理页面](http://127.0.0.1:5001)。**保持启动窗口运行**，关闭后同步服务和定时任务都会停止。以后启动只需进入项目目录，执行对应系统最后一行命令。

## 2. 批量接入业务数据

| 步骤 | 页面操作 |
| --- | --- |
| 连接飞书 | “飞书空间”填写应用凭证和多维表格链接，点击“保存并测试” |
| 连接数据库 | “数据源”选择数据库类型并保存连接，选择需要同步的数据库 |
| 建立任务 | “同步计划”默认全选全部表，取消不需要的表后点击“保存同步计划”，字段会自动配置 |
| 开始同步 | 点击“预检全部任务”核对数量，再点击“同步全部任务”，到飞书检查结果 |

首次使用可以先同步一张表，确认字段和数据正确后，再同步其余业务表。

后续常用操作：

- **补新表**：读取数据库表并保存计划，点击新任务的“同步此表”。也可搜索后“同步筛选任务”。
- **批量配置增量**：在已保存任务上方操作，覆盖全部筛选结果和分页。优先使用更新时间；递增 ID 只追踪新增，无法追踪旧记录修改。
- **自动运行**：在“运行设置”开启定时同步并填写间隔。
- **调整计划**：搜索表名，编辑任务或关闭启用开关；运行中的任务可在“执行与日志”停止。

首次同步读取全量，后续有有效游标的增量任务按增量字段读取。默认每 40000 行在同一个多维表格文件内新建分表，仍受飞书实际容量限制。源库删除的记录不会自动从飞书删除。

更多操作见[同步指南](docs/usage.md)。当前生产同步使用 Python 网页版，桌面版仍在开发。

## 3. 更新已有环境

先停止程序，备份项目目录中的 `.env`、`sync_config.json` 和 `sync_state.json` 到项目目录之外，然后执行：

```bash
git status
git pull --ff-only origin main
```

如果存在本地代码修改或拉取报冲突，先按[更新说明](docs/deployment.md#使用-git-更新客户环境)处理。

**Windows PowerShell**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe dashboard.py
```

<details>
<summary>macOS / Linux 更新后启动</summary>

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python dashboard.py
```

</details>

刷新管理页面，预检后继续同步。正常 Git 更新会保留客户配置和游标；已有任务的增量设置不会自动替换，需要时使用“批量配置增量”。

## 4. 遇到问题

保留**任务名、报错时间、页面截图或报错原文**，以及操作系统、Python、数据库类型及版本。[日志与故障排查](docs/troubleshooting.md)说明如何查看 `run_history.log` 和 `dashboard_server.log`。

在 [GitHub Issues](https://github.com/Guyungy/SqlSeverToFeishu/issues) 提交脱敏后的问题。不要上传 `.env`、密码、App Secret 或真实业务数据。

## 详细文档

| 需要了解 | 查看 |
| --- | --- |
| 不同系统安装、服务器部署、更新与验证状态 | [部署指南](docs/deployment.md) |
| 表和字段选择、增量、分表、定时与运行设置 | [同步操作指南](docs/usage.md) |
| 创建应用、发布版本、给多维表格授权 | [飞书权限](docs/feishu-permissions.md) |
| 日志位置、连接失败、权限和数据问题 | [故障排查](docs/troubleshooting.md) |
| 同步原理、限制、配置文件、命令行和桌面版进度 | [开发说明](docs/development.md) |

[更新日志](CHANGELOG.md) · [GPL-3.0 许可证](LICENSE) · 如果项目帮到了你，欢迎点个 Star。
