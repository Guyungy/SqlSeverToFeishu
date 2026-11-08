# 同步原理与开发说明

[返回首页](../README.md)

日常使用请从[同步操作指南](usage.md)开始。本页说明游标、字段转换、安全边界、配置文件和桌面版进度。

## 🔄 同步是怎么工作的

第一次执行会读取全表。成功后，每个增量任务会保存自己的游标，下一次只查询：

```sql
WHERE [增量字段] >= 上次成功游标
```

这里故意保留等号。边界记录会再次读取并按唯一键比较，内容没有变化就跳过，从而避免多行共享同一时间戳时漏数据。

安全规则：

- 只有整张表的新增和更新全部成功后才推进游标；
- 失败、取消或网络异常不会推进游标；
- 数据源、字段、唯一键、增量字段或目标表变化后，旧游标自动失效；
- 增量字段的 SQL 类型改变后，自动回退为全量扫描；
- SQL 中删除的记录不会自动从飞书删除，避免误删；
- 需要重新检查全表时，可点击任务上的“重置游标”。

### 字段类型映射

| 数据库字段类型 | 自动创建的飞书类型 |
| --- | --- |
| 整数、小数、金额、浮点 | 数字 |
| `date`、`datetime`、`datetime2`、`datetimeoffset`、`smalldatetime`、MySQL/PostgreSQL 时间戳 | 日期 |
| SQL Server `bit`、PostgreSQL/SQLite 布尔类型 | 复选框 |
| 其他常见类型 | 文本 |

SQL Server 的 `timestamp` 是二进制行版本，保留文本映射。MySQL/PostgreSQL 的 `timestamp` 归一为日期类型。只有时间而无日期的 `time` 保留文本。配置和验证状态见[数据库兼容说明](databases.md)。

SQL NULL 默认不会覆盖飞书已有值。如果确实需要清空，可以在“运行设置”中把 NULL 处理改为“覆盖”，建议先在副本表验证。

## 🔒 安全边界

| | |
| --- | --- |
| 🏠 **只在本机管理** | 管理页面仅监听 `127.0.0.1`，并包含 Host、Origin 和 CSRF 检查 |
| 🔑 **密钥不进任务配置** | App Secret 和 SQL 密码保存在本机 `.env`，页面不回显 |
| 📖 **数据库只读** | 推荐专用只读账号，程序只读取源数据库 |
| 🧭 **先预检再写入** | 正式同步前可查看预计新增、更新和跳过数量 |
| 🧱 **一次只跑一个** | 手动和定时任务共用运行锁，避免同时写入 |
| ♻️ **请求可安全重试** | 飞书请求有超时、有限退避和幂等标识 |

不要分享或提交 `.env`、日志以及真实业务数据。

---

## 📁 文件都是什么

| 文件 | 用途 | 是否敏感 |
| --- | --- | --- |
| `.env` | 飞书密钥和 SQL 数据源密码，由网页自动维护 | **是，禁止提交或分享** |
| `sync_config.json` | 数据源、字段、任务和运行设置，不含密码 | 通常否 |
| `sync_state.json` | 各任务最后成功的增量游标 | 通常否 |
| `sync.log` | 同步日志，自动轮转 | 可能包含业务字段名 |
| `run_history.log` | 网页及定时任务历史，自动轮转 | 可能包含任务名、业务字段名和错误上下文 |
| `dashboard_server.log` | 管理页面错误日志 | 可能包含错误上下文 |
| `mapping.json` | 旧版单表配置，仅用于迁移兼容 | 视旧配置而定 |

旧版 `.env` 和 `mapping.json` 会在首次加载时尽量迁移，不会删除原文件。

---

## 🖥️ 桌面版进度

仓库中还有一个 Rust + TypeScript + Tauri v2 桌面版，目标是免装 Python和数据库驱动，打开即用。

| 目录 | 内容 |
| --- | --- |
| `rust/` | Rust 同步内核与命令行工具 |
| `src-tauri/` | Tauri 桌面端后端与应用配置 |
| `ui/` | TypeScript 桌面界面 |

目前桌面版已具备编译、连接测试和库表字段扫描；完整的飞书写入、字段自动创建、唯一键去重和增量游标仍在迁移。因此，**生产同步请继续使用[首页说明的 Python 网页版](../README.md)**。

两个版本共用 `sync_config.json` 和 `.env` 的配置思路，后续迁移不需要重新理解整套流程。

---

## 🧑‍💻 开发与命令行

<details>
<summary><b>Python 命令行</b></summary>

```bash
# 预检全部启用任务
.venv/bin/python multi_sync.py --dry-run

# 同步全部启用任务
.venv/bin/python multi_sync.py

# 只预检一个任务
.venv/bin/python multi_sync.py --dry-run --job-id job_xxx

# 运行测试
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_ui_interactions.cjs
```

</details>

<details>
<summary><b>项目结构</b></summary>

```text
SqlSeverToFeishu/
├── dashboard.py              Python 网页管理服务
├── multi_sync.py             多任务同步与增量游标
├── databases.py              数据库驱动、元数据与查询方言
├── sync.py                   SQL / 飞书同步基础能力
├── workspace.py              配置、状态与运行设置
├── templates/ + static/      中文管理页面
├── docs/                     部署、操作、权限和排障文档
├── tests/                    Python 与界面回归测试
├── rust/                     Rust 同步内核
├── src-tauri/                Tauri 桌面应用
└── ui/                       TypeScript 桌面界面
```

</details>

---

## 已知限制

- 当前是数据库 → 飞书的单向同步，不会把飞书修改回写数据库；
- SQL 中删除的记录不会自动删除飞书记录；
- 人员、附件、公式、关联记录等复杂字段不能由普通 SQL 值自动转换；
- 定时同步依赖管理程序保持运行，不是操作系统级后台服务；
- 飞书多维表格自身的表、字段、记录和接口频率限制仍然适用；
- 桌面版尚未完成完整同步链路，生产环境请使用 Python 网页版。

---

相关文档：[同步操作](usage.md) · [运行日志](troubleshooting.md) · [更新日志](../CHANGELOG.md)
