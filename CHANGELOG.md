# 更新日志 (Changelog)

> 版本按功能/修复里程碑记录。分发包命名沿用 `SqlSeverToFeishu_分发版_YYYY-MM-DD.zip`。

## [1.2] - 2026-09-09

### 新增
- **支持粘贴飞书多维表格完整链接**，自动识别 `app_token` / `table_id` / `view_id`，不再需要手工分辨 `bascn`/`Bak`/`tbl`/`vew`。
  - 新增 `feishu_link.py`：解析 `/base/{app_token}?table=&view=`、`?tbl=`、裸 token 等形态。
- 网页配置面板新增「多维表格链接」输入项（推荐用法），并补字段填写提示。

### 修复
- 附带上一里程碑的 400 修复（详见 [1.1.1]）。

### 变更
- 配置字段 `FEISHU_BASE_APP_TOKEN` / `FEISHU_BASE_TABLE_ID` / `FEISHU_BASE_VIEW_ID` 由「必填」改为「可选」：填了链接即无需再填。
- 文档（README / 使用手册 / .env.example）统一为「粘贴整条链接」优先的说明。

## [1.1.1] - 2026-09-08

### 修复
- **修复飞书 400 Bad Request**：旧逻辑误把应用 App ID（`cli_` 开头）当作多维表格 app_token 调用 `/bitable/v1/apps/{...}/tables`。
  - 新增独立配置 `FEISHU_BASE_APP_TOKEN`，`dashboard.py` / `sync.py` 统一从它取值；App ID/Secret 仅用于换取 `tenant_access_token`。

## [1.1] - 2026-09-08

### 新增
- 通用化改造：自动探测 SQL Server 表字段与飞书表字段，可视化下拉映射、按同名自动匹配、可增删映射行。
- 可选唯一键：按业务主键判断「新增 / 更新」，实现自动去重。

## [1.0] - 2026-09-02

### 新增
- 首个可用版本：SQL Server → 飞书多维表格单向同步工具。
- 网页配置面板（Flask）+ 后台同步脚本（pymssql）。
- 一键安装脚本（`install.sh` / `install.bat`）与零基础《使用手册》。