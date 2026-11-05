# 飞书应用与多维表格授权

[返回首页](../README.md)

首次使用需要完成应用权限、版本发布和目标多维表格授权，然后在同步中心测试连接。

## 1. 创建企业自建应用

打开飞书开发者后台，选择接收数据的企业，点击“创建企业自建应用”，填写应用名称、描述和图标，例如命名为“SQL 数据同步”。创建后进入“凭证与基础信息”，取得 `App ID` 和 `App Secret`，用于稍后填写本项目的“飞书空间”页面。 [飞书开发者后台](https://open.feishu.cn/app)

## 2. 开通应用权限

在应用的“开发配置 → 权限管理”中，搜索并开通下列**应用身份权限**。本项目使用 `App ID`、`App Secret` 获取 `tenant_access_token`，以应用身份访问目标表格。

| 权限名称 | 权限标识 | 覆盖的项目功能 |
| --- | --- | --- |
| 查看、评论、编辑和管理多维表格 | `bitable:app` | 列出数据表、读取字段和记录、自动创建数据表和字段、批量新增及更新记录 |

也可以使用权限清单批量导入。在应用后台“权限管理 → 批量导入/导出权限”中，粘贴仓库内权限清单文件的完整内容，确认新增权限后继续发布应用版本。 [权限清单 JSON](../feishu-permissions.json)

```json
{
  "scopes": {
    "tenant": [
      "bitable:app"
    ],
    "user": []
  }
}
```

这项权限覆盖当前同步流程用到的多维表格 API。可在下列官方接口文档的“权限要求”中核对。 [列出记录](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/list) · [新增数据表](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table/create) · [新增字段](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-field/create) · [批量新增记录](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/batch_create) · [批量更新记录](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/batch_update)

## 3. 发布应用版本

进入“应用发布 → 版本管理与发布”，点击“创建版本”，填写版本号、更新说明和可用范围，提交发布。需要企业管理员审批时，等待审批通过并确认应用已上线。后续新增权限，也需要发布包含权限变更的新版本。 [飞书应用发布说明](https://www.feishu.cn/content/817244137021)

## 4. 给目标多维表格授权

先在飞书创建或打开用于接收数据的多维表格文件。使用该文件的所有者或有管理权限的账号，在右上角“…”菜单中找到“更多 → 添加文档应用”，搜索并添加刚发布的企业自建应用。入口名称可能随飞书界面版本变化。 [飞书官方授权示例](https://www.feishu.cn/content/7310068607639584771)

为应用授予可编辑及管理目标表结构所需的权限。若表格开启了高级权限，还需确认应用对目标数据表、字段和记录的实际读写范围，确保可以新增数据表和字段。应用的 API 权限与目标文件的文档授权都完成后，自动建表和写入才具备所需访问条件。 [飞书文档权限说明](https://open.feishu.cn/document/ukTMukTMukTM/uczNzUjL3czM14yN3MTN)

## 5. 在本项目中验证

1. 在“飞书空间”填写 `App ID`、`App Secret` 和目标多维表格完整链接。
2. 点击“保存并测试”，确认能列出目标文件里的数据表。
3. 配置 SQL 数据源和任务，选择“自动匹配或新建表”，开启“自动补建飞书字段”。
4. 执行预检，再正式同步，确认飞书已生成数据表、字段和数据。自动建表发生在指定的多维表格文件内部。

如果应用搜索不到，先检查应用已发布、审批状态和可用范围。能连接但建表或写入报权限错误时，分别检查 `bitable:app` 的开通和发布状态、目标文件的应用授权及高级权限范围。

---

授权完成后：[回到同步操作指南](usage.md) · [排查权限或写入失败](troubleshooting.md)
