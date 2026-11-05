# 日志与故障排查

[返回首页](../README.md)

先保留页面报错和对应时间的日志，再按下列常见情况检查。

## 📋 日志与排查

Python 网页版有页面实时输出及本地日志文件，两者的记录范围不同。

| 位置 | 内容与保留方式 |
| --- | --- |
| 页面“执行与日志” | 当前预检或同步的阶段、读取数量、新增、更新、跳过和错误输出；保留最近 5000 行在内存中，开始新任务时清空，重启后丢失 |
| 项目目录 `run_history.log` | 网页版手动及定时任务的持久化记录，包含时间、运行编号、模式、任务范围、进度、结果、退出状态及停止请求；单文件约 5 MiB，最多保留 3 份轮转备份 |
| 项目目录 `sync.log` | 同步日志处理器的输出；单文件约 5 MiB，最多保留 3 份轮转备份 |
| 项目目录 `dashboard_server.log` | 部分管理接口异常、任务启动异常及定时调度异常；单文件约 2 MiB，最多保留 2 份轮转备份 |

在页面“执行与日志”中点击“读取最近 1000 行”，可读取当前文件及轮转备份中的最近历史，程序重启后仍可查看。“清空显示”只清空实时输出显示。历史文件依次为 `run_history.log`、`run_history.log.1`、`.2`、`.3`，超出轮转容量后最旧记录会淘汰。

历史日志由管理程序统一收集子进程输出，覆盖通过网页和内置定时器执行的任务。直接在终端单独运行 `multi_sync.py` 时，其输出不进入该历史文件。程序强制退出或断电时，最后一次运行可能只有开始及进度记录，没有完成记录；这类运行需要结合飞书数据和游标核查。

`sync.log` 仍用于原有同步日志处理器，多任务运行详情请查看 `run_history.log`。部分接口错误只返回页面，排查时应同时保留页面错误。

在 Ubuntu/Linux/macOS 的项目目录查看管理错误日志：

```bash
tail -n 100 dashboard_server.log
tail -f dashboard_server.log
tail -f run_history.log
```

在 Windows PowerShell 的项目目录查看：

```powershell
Get-Content .\dashboard_server.log -Tail 100
Get-Content .\dashboard_server.log -Tail 100 -Wait
Get-Content .\run_history.log -Tail 100 -Wait
```

排查时记录任务名、执行时间和页面错误。分享日志前先检查其中的数据库地址、业务数据和错误上下文。

---

## 🩺 常见问题

<details>
<summary><b>提示“未检测到 Python”或版本不足</b></summary>

安装 Python 3.10 或更高版本。Windows 安装时勾选 `Add Python to PATH`，重新打开终端，再按[安装步骤](deployment.md#命令行完整安装流程)创建虚拟环境并运行 `dashboard.py`。如果没有 `py` 命令，可将 `py -3` 替换为 `python`。

</details>

<details>
<summary><b>管理页面没有自动打开</b></summary>

先确认启动窗口仍在运行，再访问 <http://127.0.0.1:5001>。仍然打不开时，查看项目目录中的 `dashboard_server.log`。

</details>

<details>
<summary><b>SQL Server 连接失败</b></summary>

检查服务器地址、端口、数据库名、账号密码、读取权限、TCP/IP 和防火墙。命名实例可能没有使用默认的 1433 端口，请让数据库管理员确认实际端口。

</details>

<details>
<summary><b>识别不到数据库</b></summary>

账号可能没有查看其他数据库的权限。可以直接填写已知数据库名，或者让数据库管理员授权读取目标数据库。

</details>

<details>
<summary><b>飞书能连接，但不能创建表或字段</b></summary>

检查应用是否拥有数据表和字段管理权限、应用版本是否已发布，以及应用是否已添加到目标多维表格并获得可管理权限。

</details>

<details>
<summary><b>预检提示唯一键重复或为空</b></summary>

请先在 SQL Server 或飞书中处理异常数据。不要绕过检查，否则程序无法确定应该更新哪一条记录。

</details>

<details>
<summary><b>我想每次都检查全表</b></summary>

关闭任务的“启用增量同步”。程序每次读取全表，但仍然只写入新增或真正变化的数据。

</details>

<details>
<summary><b>SQL 中删除的数据会同步删除吗</b></summary>

不会。当前只新增和更新，不自动删除飞书记录，避免数据库异常或配置错误造成批量误删。

</details>

## 提交问题

请提供操作系统、Python 和 SQL Server 版本，任务名、执行时间、操作步骤，以及脱敏后的报错或相关日志片段。通过 [GitHub Issues](https://github.com/Guyungy/SqlSeverToFeishu/issues) 反馈。不要提交 `.env`、密码、App Secret 或真实业务数据。

---

相关文档：[飞书应用授权](feishu-permissions.md) · [安装与更新](deployment.md) · [同步配置](usage.md)
