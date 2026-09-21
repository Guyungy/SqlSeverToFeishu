// SQL Server → 飞书同步 桌面端界面。
//
// 刻意不引入前端框架：这个界面就是「表单 + 列表 + 日志」，
// 用原生 DOM 直接渲染反而更好读好改，也少一层构建复杂度。

import { api, describeError, desktopRuntimeAvailable } from './api';
import type {
  ColumnMapping,
  JobStateView,
  SyncJob,
  TableOption,
  Workspace,
} from './types';
import './styles.css';

// ------------------------------------------------------------------ 状态

type View = 'overview' | 'feishu' | 'sources' | 'jobs' | 'run' | 'log';

interface SourceDraft {
  id: string;
  name: string;
  server: string;
  port: number;
  database: string;
  user: string;
  enabled: boolean;
}

interface ColumnDraft {
  source: string;
  target: string;
  sql_type: string;
  nullable: boolean;
  included: boolean;
}

interface JobDraft {
  id: string;
  name: string;
  source_id: string;
  schema: string;
  table: string;
  enabled: boolean;
  unique_key: string;
  incremental_enabled: boolean;
  incremental_column: string;
  target_mode: string;
  target_table_id: string;
  target_table_name: string;
  auto_create_fields: boolean;
  columns: ColumnDraft[];
}

interface AppState {
  view: View;
  ws: Workspace | null;
  sourceDraft: SourceDraft | null;
  jobDraft: JobDraft | null;
  tables: TableOption[];
  databases: string[];
  jobStates: JobStateView[];
  outputTitle: string;
  output: string[];
  logs: string[];
  busy: boolean;
  toast: { kind: 'ok' | 'err'; text: string } | null;
}

const state: AppState = {
  view: 'overview',
  ws: null,
  sourceDraft: null,
  jobDraft: null,
  tables: [],
  databases: [],
  jobStates: [],
  outputTitle: '',
  output: [],
  logs: [],
  busy: false,
  toast: null,
};

// -------------------------------------------------------------- 小工具

function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function checkedIn(form: HTMLFormElement, name: string): boolean {
  return Boolean(form.querySelector<HTMLInputElement>(`[data-field="${name}"]`)?.checked);
}

function inputValue(form: HTMLFormElement, name: string): string {
  const field = form.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(
    `[data-field="${name}"]`,
  );
  return field ? field.value.trim() : '';
}

function inputNumber(form: HTMLFormElement, name: string, fallback: number): number {
  const parsed = Number(inputValue(form, name));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function sourceName(id: string): string {
  const found = state.ws?.config.sources.find((item) => item.id === id);
  if (!found) return id;
  return found.name || found.server || id;
}

const container = document.getElementById('app');
if (!container) {
  throw new Error('缺少 #app 容器');
}
const app: HTMLElement = container;

// ------------------------------------------------------------------ 渲染

const VIEWS: Array<{ id: View; label: string }> = [
  { id: 'overview', label: '概览' },
  { id: 'feishu', label: '飞书目标' },
  { id: 'sources', label: 'SQL 数据源' },
  { id: 'jobs', label: '同步任务' },
  { id: 'run', label: '预检与同步' },
  { id: 'log', label: '运行日志' },
];

function render(): void {
  const config = state.ws?.config;
  const nav = VIEWS.map(
    (view) =>
      `<button class="nav-item${state.view === view.id ? ' active' : ''}" data-nav="${view.id}">${view.label}${
        view.id === 'jobs' && config ? `<span class="pill">${config.jobs.length}</span>` : ''
      }</button>`,
  ).join('');

  const banner = desktopRuntimeAvailable()
    ? ''
    : `<div class="banner err">没有检测到桌面运行时。请通过桌面应用启动；直接用浏览器打开这一页时无法读写配置。</div>`;
  const toast = state.toast
    ? `<div class="banner ${state.toast.kind === 'ok' ? 'ok' : 'err'}">${escapeHtml(state.toast.text)}</div>`
    : '';

  app.innerHTML = `
    <aside class="sidebar">
      <div class="brand"><strong>SQL Server → 飞书</strong><span>同步控制台</span></div>
      <nav>${nav}</nav>
      <div class="sidebar-foot">
        <span class="muted">配置目录</span>
        <code title="${escapeHtml(state.ws?.base_dir ?? '')}">${escapeHtml(state.ws?.base_dir ?? '加载中…')}</code>
      </div>
    </aside>
    <main class="main">
      ${banner}${toast}
      ${renderView()}
      ${state.output.length > 0 ? renderOutput() : ''}
    </main>
    <div class="busy${state.busy ? '' : ' hidden'}">处理中…</div>
  `;
}

function renderView(): string {
  if (!state.ws) {
    return `<section class="card"><h1>正在读取工作区…</h1></section>`;
  }
  switch (state.view) {
    case 'overview':
      return renderOverview();
    case 'feishu':
      return renderFeishu();
    case 'sources':
      return renderSources();
    case 'jobs':
      return renderJobs();
    case 'run':
      return renderRun();
    case 'log':
      return renderLog();
  }
}

function renderOverview(): string {
  const ws = state.ws as Workspace;
  const { config } = ws;
  const enabledJobs = config.jobs.filter((job) => job.enabled).length;
  const secret = ws.feishu.app_secret_set
    ? '<span class="tag ok">已配置</span>'
    : '<span class="tag warn">未配置</span>';
  const target = ws.feishu.base_url || ws.feishu.base_app_token || '(未设置)';
  return `
    <section class="card">
      <h1>概览</h1>
      <div class="grid">
        <div class="stat"><span>飞书 App Secret</span><strong>${secret}</strong></div>
        <div class="stat"><span>SQL 数据源</span><strong>${config.sources.length} 个</strong></div>
        <div class="stat"><span>同步任务</span><strong>${config.jobs.length} 个（启用 ${enabledJobs}）</strong></div>
        <div class="stat"><span>目标多维表格</span><strong>${escapeHtml(target)}</strong></div>
      </div>

      <h2>推荐操作顺序</h2>
      <ol class="steps">
        <li><b>飞书目标</b>：填应用凭据和多维表格链接，测试一次，确认能读到目标表。</li>
        <li><b>SQL 数据源</b>：填连接信息并测试，再扫描库里的表与字段。</li>
        <li><b>同步任务</b>：选表、勾选要同步的列、指定唯一键，保存。</li>
        <li><b>预检与同步</b>：先预检看影响面，确认后再写。</li>
      </ol>

      <h2>文件位置</h2>
      <table class="kv">
        <tr><th>配置文件</th><td><code>${escapeHtml(ws.config_path)}</code></td></tr>
        <tr><th>凭据文件</th><td><code>${escapeHtml(ws.env_path)}</code></td></tr>
        <tr><th>增量状态</th><td><code>${escapeHtml(ws.state_path)}</code></td></tr>
        <tr><th>运行日志</th><td><code>${escapeHtml(ws.log_path)}</code></td></tr>
      </table>
    </section>
  `;
}

function renderFeishu(): string {
  const feishu = (state.ws as Workspace).feishu;
  return `
    <section class="card">
      <h1>飞书目标空间</h1>
      <p class="muted">应用凭据在飞书开放平台 → 应用的「凭证与基础信息」里。多维表格链接直接粘地址栏的完整 URL，程序会自己解析出 App Token 与 Table ID。App Secret 只写不读，保存后不再回显，留空表示不修改。</p>
      <form data-form="feishu">
        <label>App ID
          <input data-field="app_id" value="${escapeHtml(feishu.app_id)}" placeholder="cli_xxxxxxxxxxxxxxxx" autocomplete="off" />
        </label>
        <label>App Secret
          <input data-field="app_secret" type="password" placeholder="${feishu.app_secret_set ? '已保存，留空表示不修改' : '尚未配置，请填入'}" autocomplete="off" />
        </label>
        <label>多维表格链接（推荐）
          <input data-field="base_url" value="${escapeHtml(feishu.base_url)}" placeholder="https://xxx.feishu.cn/base/xxxxxxxx?table=tblxxxxxxxx" autocomplete="off" />
        </label>
        <div class="row">
          <label>App Token（链接为空时才用）
            <input data-field="base_app_token" value="${escapeHtml(feishu.base_app_token)}" autocomplete="off" />
          </label>
          <label>Table ID
            <input data-field="base_table_id" value="${escapeHtml(feishu.base_table_id)}" autocomplete="off" />
          </label>
          <label>View ID
            <input data-field="base_view_id" value="${escapeHtml(feishu.base_view_id)}" autocomplete="off" />
          </label>
        </div>
        <div class="actions">
          <button type="submit" class="primary">保存</button>
          <button type="button" data-action="test-feishu">测试连接</button>
        </div>
      </form>
    </section>
  `;
}

function renderSources(): string {
  const config = (state.ws as Workspace).config;
  const list =
    config.sources.length === 0
      ? '<p class="muted">还没有数据源。点右上方「新建数据源」开始。</p>'
      : config.sources
          .map((source) => {
            const jobCount = config.jobs.filter((job) => job.source_id === source.id).length;
            return `
        <div class="item${state.sourceDraft?.id === source.id ? ' editing' : ''}">
          <div class="item-head">
            <strong>${escapeHtml(source.name || source.server)}</strong>
            ${source.enabled ? '' : '<span class="tag warn">已停用</span>'}
          </div>
          <div class="muted">${escapeHtml(source.server)}:${source.port} / ${escapeHtml(source.database || '(默认库)')} · 用户 ${escapeHtml(source.user || '(Windows 认证)')}</div>
          <div class="muted">关联任务 ${jobCount} 个 · 密码变量 <code>${escapeHtml(source.password_env)}</code></div>
          <div class="actions">
            <button data-action="edit-source" data-id="${source.id}">编辑</button>
            <button data-action="test-source" data-id="${source.id}">测试连接</button>
            <button data-action="scan-tables" data-id="${source.id}">扫描表</button>
            <button data-action="delete-source" data-id="${source.id}" class="danger">删除</button>
          </div>
        </div>`;
          })
          .join('');

  return `
    <section class="card">
      <div class="card-head"><h1>SQL 数据源</h1><button data-action="new-source">新建数据源</button></div>
      ${list}
      ${renderSourceEditor()}
      ${
        state.tables.length > 0
          ? `<div class="hint">扫描到 ${state.tables.length} 个表/视图：${state.tables
              .slice(0, 12)
              .map((table) => escapeHtml(`${table.schema}.${table.name}`))
              .join('、')}${state.tables.length > 12 ? ' …' : ''}</div>`
          : ''
      }
    </section>
  `;
}

function renderSourceEditor(): string {
  const draft = state.sourceDraft;
  if (!draft) return '';
  const isNew = draft.id === '';
  return `
    <div class="editor">
      <h2>${isNew ? '新建数据源' : `编辑：${escapeHtml(draft.name || draft.server)}`}</h2>
      <form data-form="source">
        <div class="row">
          <label>显示名称
            <input data-field="name" value="${escapeHtml(draft.name)}" placeholder="例如：生产库" />
          </label>
          <label>服务器地址
            <input data-field="server" value="${escapeHtml(draft.server)}" placeholder="192.168.1.10 或 主机名" />
          </label>
          <label>端口
            <input data-field="port" type="number" value="${draft.port}" />
          </label>
        </div>
        <div class="row">
          <label>默认数据库
            <input data-field="database" value="${escapeHtml(draft.database)}" placeholder="可留空" />
          </label>
          <label>登录用户
            <input data-field="user" value="${escapeHtml(draft.user)}" placeholder="留空表示 Windows 域认证" />
          </label>
          <label>密码
            <input data-field="password" type="password" placeholder="${isNew ? 'SQL 认证时必填' : '留空表示不修改'}" autocomplete="off" />
          </label>
        </div>
        <label class="check"><input data-field="enabled" type="checkbox" ${draft.enabled ? 'checked' : ''} />参与同步</label>
        <div class="actions">
          <button type="submit" class="primary">保存</button>
          <button type="button" data-action="cancel-source">取消</button>
          ${isNew ? '' : `<button type="button" data-action="load-databases" data-id="${draft.id}">列出数据库</button>`}
        </div>
      </form>
      ${
        state.databases.length > 0
          ? `<div class="hint">可访问的数据库：${state.databases.map(escapeHtml).join('、')}</div>`
          : ''
      }
    </div>
  `;
}

function renderJobs(): string {
  const config = (state.ws as Workspace).config;
  if (config.sources.length === 0) {
    return `<section class="card"><h1>同步任务</h1><p class="muted">请先在「SQL 数据源」里添加一个数据源。</p></section>`;
  }
  const list =
    config.jobs.length === 0
      ? '<p class="muted">还没有任务。一个任务 = 一张 SQL 表 → 一张飞书表的字段映射。</p>'
      : config.jobs
          .map((job) => {
            const cursor = state.jobStates.some((item) => item.id === job.id);
            return `
        <div class="item${state.jobDraft?.id === job.id ? ' editing' : ''}">
          <div class="item-head">
            <strong>${escapeHtml(job.name || `${job.schema}.${job.table}`)}</strong>
            ${job.enabled ? '<span class="tag ok">启用</span>' : '<span class="tag warn">停用</span>'}
          </div>
          <div class="muted">${escapeHtml(sourceName(job.source_id))} · ${escapeHtml(job.schema)}.${escapeHtml(job.table)} → 飞书表 ${escapeHtml(job.target.table_name || job.target.table_id || '(按表名自动匹配)')}</div>
          <div class="muted">列 ${job.columns.length} 个 · 唯一键 <code>${escapeHtml(job.unique_key)}</code>${
            job.incremental.enabled ? ` · 增量字段 <code>${escapeHtml(job.incremental.column)}</code>` : ' · 全量'
          }${cursor ? ' · 有增量游标' : ''}</div>
          <div class="actions">
            <button data-action="edit-job" data-id="${job.id}">编辑</button>
            <button data-action="reset-job-state" data-id="${job.id}">重置增量游标</button>
            <button data-action="delete-job" data-id="${job.id}" class="danger">删除</button>
          </div>
        </div>`;
          })
          .join('');

  return `
    <section class="card">
      <div class="card-head"><h1>同步任务</h1><button data-action="new-job">新建任务</button></div>
      ${list}
      ${renderJobEditor()}
    </section>
  `;
}

function renderJobEditor(): string {
  const draft = state.jobDraft;
  if (!draft) return '';
  const config = (state.ws as Workspace).config;
  const isNew = draft.id === '';
  const sourceOptions = config.sources
    .map(
      (source) =>
        `<option value="${source.id}"${source.id === draft.source_id ? ' selected' : ''}>${escapeHtml(source.name || source.server)}</option>`,
    )
    .join('');

  const keyOptions = draft.columns
    .filter((column) => column.included)
    .map(
      (column) =>
        `<option value="${escapeHtml(column.source)}"${column.source === draft.unique_key ? ' selected' : ''}>${escapeHtml(column.source)}</option>`,
    )
    .join('');

  const columnRows =
    draft.columns.length === 0
      ? '<tr><td colspan="3" class="muted">先选数据源、填表名，再点「扫描字段」。</td></tr>'
      : draft.columns
          .map(
            (column) => `
        <tr>
          <td><input type="checkbox" data-col-include ${column.included ? 'checked' : ''} /></td>
          <td><code>${escapeHtml(column.source)}</code><div class="muted">${escapeHtml(column.sql_type)}${column.nullable ? ' · 可空' : ''}</div></td>
          <td><input data-col-target value="${escapeHtml(column.target)}" /></td>
        </tr>`,
          )
          .join('');

  return `
    <div class="editor">
      <h2>${isNew ? '新建同步任务' : `编辑：${escapeHtml(draft.name || draft.table)}`}</h2>
      <form data-form="job">
        <div class="row">
          <label>任务名称
            <input data-field="name" value="${escapeHtml(draft.name)}" placeholder="可留空，默认用表名" />
          </label>
          <label>数据源
            <select data-field="source_id"><option value="">请选择…</option>${sourceOptions}</select>
          </label>
          <label>Schema
            <input data-field="schema" value="${escapeHtml(draft.schema)}" />
          </label>
          <label>表名
            <input data-field="table" value="${escapeHtml(draft.table)}" />
          </label>
          <button type="button" data-action="scan-columns">扫描字段</button>
        </div>

        <h3>字段映射</h3>
        <table class="cols">
          <thead><tr><th>同步</th><th>SQL 列</th><th>飞书字段名</th></tr></thead>
          <tbody>${columnRows}</tbody>
        </table>

        <div class="row">
          <label>唯一键列
            ${
              keyOptions
                ? `<select data-field="unique_key">${keyOptions}</select>`
                : `<input data-field="unique_key" value="${escapeHtml(draft.unique_key)}" placeholder="例如 Id" />`
            }
          </label>
          <label class="check"><input data-field="incremental_enabled" type="checkbox" ${draft.incremental_enabled ? 'checked' : ''} />启用增量同步</label>
          <label>增量字段
            <input data-field="incremental_column" value="${escapeHtml(draft.incremental_column)}" placeholder="例如 UpdatedAt" />
          </label>
        </div>

        <div class="row">
          <label>目标表选择方式
            <select data-field="target_mode">
              <option value="auto"${draft.target_mode === 'auto' ? ' selected' : ''}>按表名自动匹配/创建</option>
              <option value="existing"${draft.target_mode === 'existing' ? ' selected' : ''}>使用现有多维表格</option>
            </select>
          </label>
          <label>飞书 Table ID
            <input data-field="target_table_id" value="${escapeHtml(draft.target_table_id)}" placeholder="tbl…（可留空）" />
          </label>
          <label>飞书目标表名
            <input data-field="target_table_name" value="${escapeHtml(draft.target_table_name)}" />
          </label>
          <label class="check"><input data-field="auto_create_fields" type="checkbox" ${draft.auto_create_fields ? 'checked' : ''} />自动创建缺失字段</label>
        </div>

        <label class="check"><input data-field="enabled" type="checkbox" ${draft.enabled ? 'checked' : ''} />参与同步</label>

        <div class="actions">
          <button type="submit" class="primary">保存</button>
          <button type="button" data-action="cancel-job">取消</button>
        </div>
      </form>
    </div>
  `;
}

function renderRun(): string {
  return `
    <section class="card">
      <h1>预检与同步</h1>
      <div class="banner warn">
        同步执行还没接到这个界面上。内核目前完成的是「配置读写 + 连通性探测 + 元数据读取」，
        写飞书那一段（字段自动创建、按唯一键去重、增量游标推进）仍在从 Python 版迁移。
        在那之前同步请继续用命令行版：界面与命令行版共用同一份
        <code>sync_config.json</code> 和 <code>.env</code>，两边数据是通的。
      </div>
      <div class="actions">
        <button disabled>预检（未实现）</button>
        <button disabled>同步（未实现）</button>
      </div>
    </section>
  `;
}

function renderLog(): string {
  const body =
    state.logs.length === 0
      ? '<p class="muted">日志为空，点「刷新」重新读取。</p>'
      : `<pre class="log">${escapeHtml(state.logs.join('\n'))}</pre>`;
  return `
    <section class="card">
      <div class="card-head"><h1>运行日志</h1><button data-action="refresh-log">刷新</button></div>
      ${body}
    </section>
  `;
}

function renderOutput(): string {
  return `
    <section class="card output">
      <div class="card-head">
        <h2>${escapeHtml(state.outputTitle || '输出')}</h2>
        <button data-action="clear-output">清空</button>
      </div>
      <pre>${escapeHtml(state.output.join('\n'))}</pre>
    </section>
  `;
}

// ------------------------------------------------------------------ 动作

async function run(label: string, task: () => Promise<void>): Promise<void> {
  state.busy = true;
  state.toast = null;
  render();
  try {
    await task();
  } catch (error) {
    state.toast = { kind: 'err', text: `${label}失败：${describeError(error)}` };
  } finally {
    state.busy = false;
    render();
  }
}

function newSourceDraft(): SourceDraft {
  return { id: '', name: '', server: '', port: 1433, database: '', user: '', enabled: true };
}

function jobToDraft(job: SyncJob): JobDraft {
  return {
    id: job.id,
    name: job.name,
    source_id: job.source_id,
    schema: job.schema,
    table: job.table,
    enabled: job.enabled,
    unique_key: job.unique_key,
    incremental_enabled: job.incremental.enabled,
    incremental_column: job.incremental.column,
    target_mode: job.target.mode,
    target_table_id: job.target.table_id,
    target_table_name: job.target.table_name,
    auto_create_fields: job.target.auto_create_fields,
    columns: job.columns.map((column) => ({
      source: column.source,
      target: column.target,
      sql_type: column.sql_type,
      nullable: column.nullable,
      included: true,
    })),
  };
}

function newJobDraft(): JobDraft {
  const first = state.ws?.config.sources[0];
  return {
    id: '',
    name: '',
    source_id: first ? first.id : '',
    schema: 'dbo',
    table: '',
    enabled: true,
    unique_key: '',
    incremental_enabled: false,
    incremental_column: '',
    target_mode: 'auto',
    target_table_id: '',
    target_table_name: '',
    auto_create_fields: true,
    columns: [],
  };
}

/** 把表单读回草稿。字段映射表是索引对齐的，行数由草稿决定。 */
function readJobForm(form: HTMLFormElement): void {
  const draft = state.jobDraft;
  if (!draft) return;
  form.querySelectorAll<HTMLTableRowElement>('tbody tr').forEach((row, index) => {
    const column = draft.columns[index];
    if (!column) return;
    column.included = Boolean(row.querySelector<HTMLInputElement>('[data-col-include]')?.checked);
    const target = row.querySelector<HTMLInputElement>('[data-col-target]');
    column.target = target ? target.value.trim() : column.source;
  });

  draft.name = inputValue(form, 'name');
  draft.source_id = inputValue(form, 'source_id');
  draft.schema = inputValue(form, 'schema') || 'dbo';
  draft.table = inputValue(form, 'table');
  draft.unique_key = inputValue(form, 'unique_key');
  draft.incremental_enabled = checkedIn(form, 'incremental_enabled');
  draft.incremental_column = inputValue(form, 'incremental_column');
  draft.target_mode = inputValue(form, 'target_mode') || 'auto';
  draft.target_table_id = inputValue(form, 'target_table_id');
  draft.target_table_name = inputValue(form, 'target_table_name');
  draft.auto_create_fields = checkedIn(form, 'auto_create_fields');
  draft.enabled = checkedIn(form, 'enabled');
}

function includedColumns(): ColumnMapping[] {
  const draft = state.jobDraft;
  if (!draft) return [];
  return draft.columns
    .filter((column) => column.included)
    .map((column) => ({
      source: column.source,
      target: column.target || column.source,
      sql_type: column.sql_type,
      nullable: column.nullable,
    }));
}

async function handleAction(action: string, id: string): Promise<void> {
  switch (action) {
    case 'clear-output':
      state.output = [];
      state.outputTitle = '';
      render();
      return;

    case 'test-feishu':
      await run('测试飞书', async () => {
        state.outputTitle = '飞书连接测试';
        state.output = await api.testFeishu();
        state.toast = { kind: 'ok', text: '飞书连接正常' };
      });
      return;

    case 'new-source':
      state.sourceDraft = newSourceDraft();
      state.databases = [];
      state.tables = [];
      render();
      return;

    case 'cancel-source':
      state.sourceDraft = null;
      state.databases = [];
      state.tables = [];
      render();
      return;

    case 'edit-source': {
      const source = state.ws?.config.sources.find((item) => item.id === id);
      if (source) {
        state.sourceDraft = {
          id: source.id,
          name: source.name,
          server: source.server,
          port: source.port,
          database: source.database,
          user: source.user,
          enabled: source.enabled,
        };
        state.databases = [];
        state.tables = [];
      }
      render();
      return;
    }

    case 'delete-source':
      if (!window.confirm('确定删除这个数据源？还有任务引用它时会被拦下。')) return;
      await run('删除数据源', async () => {
        state.ws = await api.deleteSource(id);
        if (state.sourceDraft?.id === id) state.sourceDraft = null;
        state.toast = { kind: 'ok', text: '数据源已删除' };
      });
      return;

    case 'test-source':
      await run('测试数据源', async () => {
        state.outputTitle = `数据源测试：${sourceName(id)}`;
        state.output = await api.testSource(id);
        state.toast = { kind: 'ok', text: '数据源连接正常' };
      });
      return;

    case 'scan-tables':
      await run('扫描表', async () => {
        state.tables = await api.listTables(id);
        state.sourceDraft = { ...(state.sourceDraft ?? newSourceDraft()), id };
        state.toast = { kind: 'ok', text: `扫描到 ${state.tables.length} 个表/视图` };
      });
      return;

    case 'load-databases':
      await run('列出数据库', async () => {
        state.databases = await api.listDatabases(id);
        state.toast = { kind: 'ok', text: `找到 ${state.databases.length} 个数据库` };
      });
      return;

    case 'new-job':
      state.jobDraft = newJobDraft();
      render();
      return;

    case 'cancel-job':
      state.jobDraft = null;
      render();
      return;

    case 'edit-job': {
      const job = state.ws?.config.jobs.find((item) => item.id === id);
      if (job) state.jobDraft = jobToDraft(job);
      render();
      return;
    }

    case 'delete-job':
      if (!window.confirm('确定删除这个任务？')) return;
      await run('删除任务', async () => {
        state.ws = await api.deleteJob(id);
        state.jobStates = await api.jobStates();
        if (state.jobDraft?.id === id) state.jobDraft = null;
        state.toast = { kind: 'ok', text: '任务已删除' };
      });
      return;

    case 'reset-job-state':
      await run('重置游标', async () => {
        state.jobStates = await api.resetJobState(id);
        state.toast = { kind: 'ok', text: '增量游标已重置，下次同步将回退全量' };
      });
      return;

    case 'scan-columns':
      await run('扫描字段', async () => {
        const form = document.querySelector<HTMLFormElement>('form[data-form="job"]');
        if (!form) return;
        readJobForm(form);
        const draft = state.jobDraft;
        if (!draft) return;
        if (!draft.source_id) throw new Error('请先选择数据源');
        if (!draft.table) throw new Error('请先填表名');
        const columns = await api.listColumns(draft.source_id, draft.schema, draft.table);
        draft.columns = columns.map((column) => ({
          source: column.name,
          target: column.name,
          sql_type: column.data_type,
          nullable: column.nullable,
          included: true,
        }));
        if (!draft.unique_key) {
          draft.unique_key = columns.length > 0 ? columns[0].name : '';
        }
        state.toast = { kind: 'ok', text: `扫描到 ${columns.length} 个字段` };
      });
      return;

    case 'refresh-log':
      await run('读取日志', async () => {
        state.logs = await api.tailLog(300);
        state.toast = { kind: 'ok', text: `读取 ${state.logs.length} 行日志` };
      });
      return;

    default:
      return;
  }
}

app.addEventListener('click', (event) => {
  const target = event.target as HTMLElement;

  const nav = target.closest<HTMLElement>('[data-nav]');
  if (nav?.dataset.nav) {
    state.view = nav.dataset.nav as View;
    state.output = [];
    state.outputTitle = '';
    render();
    return;
  }

  const button = target.closest<HTMLElement>('[data-action]');
  if (!button) return;
  void handleAction(button.dataset.action ?? '', button.dataset.id ?? '');
});

app.addEventListener('submit', (event) => {
  const form = event.target as HTMLFormElement;
  event.preventDefault();
  const kind = form.dataset.form;

  if (kind === 'feishu') {
    void run('保存飞书配置', async () => {
      state.ws = await api.saveFeishu({
        app_id: inputValue(form, 'app_id'),
        app_secret: inputValue(form, 'app_secret'),
        base_url: inputValue(form, 'base_url'),
        base_app_token: inputValue(form, 'base_app_token'),
        base_table_id: inputValue(form, 'base_table_id'),
        base_view_id: inputValue(form, 'base_view_id'),
      });
      state.toast = { kind: 'ok', text: '飞书配置已保存' };
    });
    return;
  }

  if (kind === 'source') {
    void run('保存数据源', async () => {
      const draft = state.sourceDraft ?? newSourceDraft();
      state.ws = await api.saveSource({
        id: draft.id,
        name: inputValue(form, 'name') || inputValue(form, 'server'),
        server: inputValue(form, 'server'),
        port: inputNumber(form, 'port', 1433),
        database: inputValue(form, 'database'),
        user: inputValue(form, 'user'),
        password: inputValue(form, 'password'),
        enabled: checkedIn(form, 'enabled'),
      });
      state.sourceDraft = null;
      state.toast = { kind: 'ok', text: '数据源已保存' };
    });
    return;
  }

  if (kind === 'job') {
    void run('保存任务', async () => {
      readJobForm(form);
      const draft = state.jobDraft;
      if (!draft) return;
      state.ws = await api.saveJob({
        id: draft.id,
        name: draft.name,
        source_id: draft.source_id,
        schema: draft.schema,
        table: draft.table,
        enabled: draft.enabled,
        columns: includedColumns(),
        unique_key: draft.unique_key,
        incremental_enabled: draft.incremental_enabled,
        incremental_column: draft.incremental_column,
        target_mode: draft.target_mode,
        target_table_id: draft.target_table_id,
        target_table_name: draft.target_table_name,
        auto_create_fields: draft.auto_create_fields,
      });
      state.jobDraft = null;
      state.toast = { kind: 'ok', text: '任务已保存' };
    });
  }
});

// ------------------------------------------------------------------ 启动

async function boot(): Promise<void> {
  if (!desktopRuntimeAvailable()) {
    render();
    return;
  }
  await run('读取工作区', async () => {
    state.ws = await api.workspace();
    state.jobStates = await api.jobStates();
    const config = state.ws.config;
    // 全新安装时直接落到「飞书目标」，省掉用户找入口的一步。
    if (config.sources.length === 0 && !state.ws.feishu.base_url && !state.ws.feishu.base_app_token) {
      state.view = 'feishu';
    }
  });
}

void boot();
