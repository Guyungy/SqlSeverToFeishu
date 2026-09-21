/** 对桌面运行时的薄封装。
 *
 *  走 `window.__TAURI__` 而不是 `@tauri-apps/api`：
 *  - 少一个依赖，也少一处「JS 包版本和 Rust 版本对不上」的坑；
 *  - 打包产物里只有我们自己的代码，出问题好定位。
 *  `tauri.conf.json` 里的 `app.withGlobalTauri: true` 负责把它挂到 window 上。
 */

import type {
  ColumnOption,
  FeishuInput,
  JobInput,
  JobStateView,
  SourceInput,
  TableOption,
  Workspace,
} from './types';

interface TauriCore {
  invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>;
}

function core(): TauriCore | undefined {
  const global = window as unknown as { __TAURI__?: { core?: TauriCore } };
  return global.__TAURI__?.core;
}

/** 前端也可能是被人直接用浏览器打开的，这时要给出能看懂的解释。 */
export function desktopRuntimeAvailable(): boolean {
  return typeof core()?.invoke === 'function';
}

async function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const api = core();
  if (!api) {
    throw new Error('没有检测到桌面运行时：请从桌面应用里打开，不要直接双击 HTML 文件。');
  }
  return api.invoke<T>(command, args);
}

export const api = {
  workspace: () => call<Workspace>('workspace'),

  saveFeishu: (input: FeishuInput) => call<Workspace>('save_feishu', { input }),
  testFeishu: () => call<string[]>('test_feishu'),

  saveSource: (input: SourceInput) => call<Workspace>('save_source', { input }),
  deleteSource: (id: string) => call<Workspace>('delete_source', { id }),
  testSource: (id: string) => call<string[]>('test_source', { id }),
  listDatabases: (id: string) => call<string[]>('list_databases', { id }),
  listTables: (id: string) => call<TableOption[]>('list_tables', { id }),
  listColumns: (id: string, schema: string, table: string) =>
    call<ColumnOption[]>('list_columns', { id, schema, table }),

  saveJob: (input: JobInput) => call<Workspace>('save_job', { input }),
  deleteJob: (id: string) => call<Workspace>('delete_job', { id }),

  jobStates: () => call<JobStateView[]>('job_states'),
  resetJobState: (id: string) => call<JobStateView[]>('reset_job_state', { id }),

  tailLog: (lines: number) => call<string[]>('tail_log', { lines }),
};

export function describeError(error: unknown): string {
  if (typeof error === 'string') {
    return error;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}
