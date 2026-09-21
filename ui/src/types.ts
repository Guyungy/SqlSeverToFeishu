/** 与 `rust/src/config.rs` 里的结构一一对应。
 *
 *  字段名刻意保持 snake_case：配置文件的 JSON 就是这么存的，
 *  两端不做任何重命名，改字段时 grep 一次就能同时找到两边。
 */

export interface DataSource {
  id: string;
  name: string;
  server: string;
  port: number;
  database: string;
  user: string;
  password_env: string;
  enabled: boolean;
}

export interface ColumnMapping {
  source: string;
  target: string;
  sql_type: string;
  nullable: boolean;
}

export interface Incremental {
  enabled: boolean;
  column: string;
}

export interface TargetConfig {
  mode: string;
  table_id: string;
  table_name: string;
  auto_create_fields: boolean;
}

export interface SyncJob {
  id: string;
  name: string;
  source_id: string;
  schema: string;
  table: string;
  enabled: boolean;
  columns: ColumnMapping[];
  unique_key: string;
  incremental: Incremental;
  target: TargetConfig;
}

export interface RuntimeSettings {
  query_timeout: number;
  null_policy: string;
  timezone_offset: number;
  dashboard_port: number;
  schedule_enabled: boolean;
  schedule_interval_minutes: number;
}

export interface SyncConfig {
  version: number;
  sources: DataSource[];
  jobs: SyncJob[];
  settings: RuntimeSettings;
}

/** 回显给界面的飞书配置。刻意不含 App Secret：密钥只写不读。 */
export interface FeishuView {
  app_id: string;
  base_url: string;
  base_app_token: string;
  base_table_id: string;
  base_view_id: string;
  app_secret_set: boolean;
}

export interface Workspace {
  base_dir: string;
  config_path: string;
  env_path: string;
  state_path: string;
  log_path: string;
  feishu: FeishuView;
  config: SyncConfig;
}

export interface FeishuInput {
  app_id: string;
  app_secret: string;
  base_url: string;
  base_app_token: string;
  base_table_id: string;
  base_view_id: string;
}

export interface SourceInput {
  id: string;
  name: string;
  server: string;
  port: number;
  database: string;
  user: string;
  password: string;
  enabled: boolean;
}

export interface JobInput {
  id: string;
  name: string;
  source_id: string;
  schema: string;
  table: string;
  enabled: boolean;
  columns: ColumnMapping[];
  unique_key: string;
  incremental_enabled: boolean;
  incremental_column: string;
  target_mode: string;
  target_table_id: string;
  target_table_name: string;
  auto_create_fields: boolean;
}

export interface TableOption {
  schema: string;
  name: string;
  kind: string;
}

export interface ColumnOption {
  name: string;
  data_type: string;
  nullable: boolean;
}

export interface JobStateView {
  id: string;
  fingerprint: string;
  cursor: unknown;
}
