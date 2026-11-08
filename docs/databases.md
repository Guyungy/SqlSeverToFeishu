# 数据库兼容说明

[返回首页](../README.md)

在 Python 网页版“数据源”中选择数据库类型，再保存连接。每个数据库保存为一个数据源，读取表后默认全选，可批量创建任务。`multi_sync.py` 使用同一套配置运行。

## 连接参数

| 类型 | 默认端口 | 数据库与 Schema | 连接说明 |
| --- | --- | --- | --- |
| SQL Server | 1433 | 库名，通常使用 `dbo` | 旧配置缺少 `type` 时自动识别为 SQL Server，保留原驱动和中文字符转换 |
| PostgreSQL | 5432 | 库名，自动读取可见业务 Schema | 留空时连接 `postgres`，支持 identity/serial 主键和时间戳 |
| MySQL / MariaDB | 3306 | 库名即 Schema | 使用 `utf8mb4`，库名留空可识别可访问数据库，选定后再读取表 |
| SQLite | 无 | 本机文件，Schema 为 `main` | 填写同步程序所在电脑上的绝对路径，以只读方式打开已有文件 |

数据库账号应有读取相关表、视图和元数据的权限。网络连接沿用数据库驱动的默认 TLS 行为，要求指定 CA、客户端证书或专用认证的环境需单独验证。SQLite 文件必须是普通 SQLite 数据库，SQLCipher 加密文件暂未适配。

已有任务的数据源不能直接切换数据库类型。请新增数据源，避免沿用不匹配的字段、目标绑定和游标。

## 字段与增量

- 中文字段、文本内容和 Emoji 按驱动 Unicode 连接读取。
- MySQL/PostgreSQL 的 `timestamp` 映射为日期，SQL Server 的同名类型保留行版本语义。
- MySQL/MariaDB 会话固定为 UTC，`TIMESTAMP` 保留绝对时间点，`DATETIME` 按页面的时区解释。升级后相关旧游标失效，下一次执行全量核对以修正日期。
- PostgreSQL JSON/数组转成 JSON 文本，新引擎的二进制值转成十六进制文本。MySQL 多位 `BIT` 保留整数值。
- 自动增量优先使用常见更新时间字段，其次使用正向自增主键。ID 增量适用于新增数据，更新历史记录需要维护更新时间字段。
- SQLite 日期建议统一保存为固定格式 ISO 日期时间文本。混用文本、Unix 时间戳和不同偏移会影响排序与增量比较。
- 复合主键生成稳定标识，无主键表使用内容标识并全量读取。源端删除不自动删除飞书记录。

## 当前验证范围

| 引擎 | 当前证据 |
| --- | --- |
| SQL Server | 原有 112 项回归持续通过，本次未连接客户实例 |
| SQLite | 真实数据库验证连接、发现、中文预览、批量任务、全量和增量读取、重复执行、只读限制、复合主键、无主键视图及 40001 行分表重试 |
| PostgreSQL 16 | 临时实例验证上述流程，包括只授予 Schema USAGE 和表 SELECT 的账号、identity 主键与服务端游标 |
| MySQL 8.4.13 | 临时实例验证上述流程，包括只授予表 SELECT 的账号、auto_increment 主键、UTC 时间戳和流式读取 |
| MariaDB 11.4.13 | 独立临时实例验证上述流程，主键通过只读账号可见的索引元数据识别 |

上述同步回归中的飞书写入使用测试替身。客户数据库、网络、权限及真实飞书目标仍需在部署环境预检和验收。

## 开发验证

默认测试会运行真实 SQLite 用例。配置环境变量后可启用其他引擎的集成用例。

- PostgreSQL 测试会创建随机 Schema、表、视图及只读角色，结束后清除，需要专用测试实例上的管理员账号。
- MySQL/MariaDB 测试会创建随机数据库、表、视图及只读账号，结束后清除，需要专用测试实例上的管理员账号。
- 每个引擎用 40001 行测试分表，模拟末批写入已接收但响应丢失，验证重跑后记录数量与唯一键一致，失败时不推进游标。

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_ui_interactions.cjs

SQLFEISHU_TEST_POSTGRES_DSN='host=127.0.0.1 port=5432 dbname=test user=tester' \
  .venv/bin/python -m unittest discover -s tests -p test_databases.py -v

SQLFEISHU_TEST_MYSQL_JSON='{"host":"127.0.0.1","port":53317,"user":"test_admin"}' \
SQLFEISHU_TEST_MARIADB_JSON='{"host":"127.0.0.1","port":53316,"user":"test_admin"}' \
  .venv/bin/python -m unittest discover -s tests -p test_databases.py -v
```

生产接入使用只读账号，测试库使用独立配置。

之前以 MariaDB 只读账号建出的无主键任务，可在任务编辑器中将唯一键改为实际主键，再配置增量。已有任务保留人工设置，升级后需要主动调整。
