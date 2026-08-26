/**
 * The API's shapes, as the interface consumes them.
 *
 * Hand-written rather than generated. The backend has no OpenAPI-to-TypeScript
 * step and adding one would put a build tool between a route change and the
 * screen that reads it; these are the fields the UI actually uses, and a field
 * the UI does not use is a field it should not claim to know about.
 */

/** `GET /api/vanna/v2/me` */
export interface Me {
  user: { id: string; email: string; name: string; role: Role };
  tenant: { id: string; name: string; database?: string | null } | null;
  /**
   * Workspace ids, not objects. `tenants_for_email` returns bare ids
   * (`tenancy.py`: `SELECT tenant_id ... ORDER BY tenant_id`), and this was
   * declared as `{tenant_id, name, role}[]` -- so anything rendering `.name`
   * off it got `undefined`. The workspace switcher reads this list, which is
   * why the shape has to be right before one is built: display names come from
   * `GET /tenants`, not from here.
   */
  memberships: string[];
  is_platform_admin: boolean;
  is_admin: boolean;
  control_plane: boolean;
  deployment_mode: 'demo' | 'single-tenant' | 'multi-tenant';
}

/**
 * A row in `tenant_users`. Three, CHECK-constrained in SQL.
 *
 * Note this is the *workspace* role. A platform admin is a separate tier -- an
 * address in VANNA_ADMIN_EMAILS -- and is carried by `is_platform_admin`, not by
 * a fourth value here. Conflating them is how a workspace admin ends up able to
 * repoint their own workspace at another database.
 */
export type Role = 'admin' | 'analyst' | 'viewer';

/** `GET /api/vanna/v2/admin/overview` */
export interface Overview {
  /** The workspace these numbers describe. Empty means every workspace. */
  scope: string;
  window_days: number;
  kpis: OverviewKpis;
  series: ActivityPoint[];
  recent: AuditEvent[];
  /** The audit vocabulary, served so the filter cannot drift from the writer. */
  actions: string[];
  /** Platform-wide only. */
  workspaces?: WorkspaceUsage[];
  /** Platform admin only -- omitted, not zeroed, for anybody else. */
  data_sources?: DataSourceHealth[];
  spend?: Spend;
}

export interface OverviewKpis {
  workspaces: number;
  active_workspaces?: number;
  members: number;
  questions: number;
  succeeded: number;
  /** `null` when nothing was asked. Not zero -- see `_rate` in routes/overview.py. */
  success_rate: number | null;
  active_users: number;
  liked: number;
  disliked: number;
  last_activity: string | null;
  cost_usd?: number;
}

export interface ActivityPoint {
  day: string;
  questions: number;
  succeeded: number;
}

/**
 * A row of `list_tenants_with_usage`.
 *
 * The counts are nested under `usage` rather than sitting beside `id` and
 * `name`. Reading them from the wrong level yields a confident zero, which is
 * indistinguishable from a workspace nobody used.
 */
export interface WorkspaceUsage {
  id: string;
  name: string;
  description?: string;
  is_active: boolean;
  data_source?: string;
  allow_writes?: boolean;
  allow_byo_key?: boolean;
  daily_quota?: number;
  max_rows?: number;
  usage: {
    tenant_id: string;
    window_days: number;
    questions: number;
    succeeded: number;
    liked: number;
    disliked: number;
    active_users: number;
    members: number;
    last_activity: string | null;
  };
}

/**
 * `last_ok` is tri-state and the third state is the dangerous one: `null` means
 * nobody has ever checked, which must never render the same as "working".
 */
export interface DataSourceHealth {
  tenant_id?: string;
  data_source_id: string;
  label: string;
  last_ok: boolean | null;
  last_checked_at: string | null;
  last_error: string | null;
}

export interface Spend {
  window_days: number;
  cost_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
  questions: number;
  by_model?: Array<{ model: string; questions: number; cost_usd: number }>;
}

/** A row of `admin_audit` -- what an operator did. */
export interface AuditEvent {
  id: string;
  actor_email: string | null;
  actor_ip: string | null;
  action: string;
  tenant_id: string | null;
  target: string | null;
  details: unknown;
  request_id?: string | null;
  created_at: string;
}

/** A row of `audit_events` -- what the agent did, and what was refused. */
export interface AccessEvent {
  event_id: string;
  event_type: string;
  user_email: string | null;
  tool_name: string | null;
  access_granted: boolean | null;
  request_id: string | null;
  payload: unknown;
  created_at: string;
}

/** A row of `GET /admin/accounts` -- identity is global, membership is not. */
export interface Account {
  email: string;
  full_name: string;
  is_active: boolean;
  auth_provider: string;
  /** A temporary password: the account must change it before it can do anything. */
  must_change: boolean;
  last_login_at: string | null;
  password_changed_at: string | null;
  created_at: string;
}

/** A row of `tenant_users`: one person's membership of one workspace. */
export interface Member {
  id: string;
  tenant_id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  last_seen_at: string | null;
  created_at: string;
}

export interface Usage {
  tenant_id: string;
  window_days: number;
  questions: number;
  succeeded: number;
  liked: number;
  disliked: number;
  active_users: number;
  members: number;
  last_activity: string | null;
}

export interface Plan {
  name: string;
  label: string;
  description: string;
  daily_quota: number;
  max_rows: number;
}

export interface Billing {
  subscription: { plan: string; expires_at: string | null } | null;
  plan: string;
  /** `*_source` says whether a limit came from the plan, an override, or the default. */
  limits: {
    daily_quota: number;
    max_rows: number;
    quota_source: string;
    rows_source: string;
  };
  usage: Usage;
  payments: Array<{
    id: string;
    amount_cents: number;
    currency: string;
    note: string;
    created_at: string;
  }>;
  /** Only a platform admin may change a plan; a workspace admin may read it. */
  can_change: boolean;
  available_plans: Plan[];
}

export interface StarterQuestion {
  id: string;
  question: string;
  sort_order: number;
}

/** A database a workspace may be asked about. Credential-free by construction. */
export interface DataSource {
  data_source_id: string;
  label: string;
  is_default: boolean;
  last_ok: boolean | null;
  last_checked_at: string | null;
  last_error: string | null;
}

export interface SavedQuery {
  id: string;
  title: string;
  question: string;
  sql: string;
  created_by: string;
  created_at: string;
}

export interface Dashboard {
  id: string;
  tenant_id: string;
  title: string;
  description: string;
  tiles: Tile[];
  parameters: Parameter[];
  created_by: string;
  created_at: string;
  updated_at: string;
}

export type TileKind = 'chart' | 'table' | 'metric' | 'text';
export type ChartType = 'bar' | 'line' | 'area' | 'pie' | 'scatter' | 'heatmap';

export interface ChartSpec {
  type?: ChartType;
  x?: string;
  y?: string[];
  color_by?: string;
  stacked?: boolean;
  sort_by?: string;
  descending?: boolean;
  limit?: number;
  x_label?: string;
  y_label?: string;
}

export interface GridPosition {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type TileQuery =
  | { source: 'saved'; saved_query_id: string }
  | { source: 'sql'; sql: string }
  | {
      source: 'cube';
      cube: string;
      measures: string[];
      dimensions: string[];
      time_dimension?: string;
      granularity?: string;
      filters: string[];
    };

export interface Tile {
  id: string;
  kind: TileKind;
  title: string;
  description: string;
  query?: TileQuery;
  chart?: ChartSpec;
  text?: string;
  grid: GridPosition;
  refresh_seconds?: number;
}

export type ParameterType = 'date' | 'date_range' | 'integer' | 'enum';

export interface Parameter {
  name: string;
  type: ParameterType;
  label: string;
  default?: unknown;
  options: string[];
  minimum?: number;
  maximum?: number;
}

export interface TileResult {
  tile_id: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  error?: string | null;
  warnings: string[];
}
