import { ShieldCheck } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useLocale } from '@/i18n';
import { api, put, del } from '@/lib/api';
import { toastError, toastSuccess } from '@/lib/toast';
import type { Role } from '@/types';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * Which tables a role may read, per workspace.
 *
 * This is the real access control -- not the sidebar, not domain membership.
 * The grants here are enforced in the SQL layer before a query runs, and
 * `read_guard.py` is what makes that true rather than advisory.
 *
 * Grants are keyed `(tenant_id, data_source_id, ...)`, so a workspace's second
 * database has its own matrix. Editing one while looking at the other is the
 * mistake this screen has to make impossible, which is why the data source is
 * named in the toolbar rather than assumed.
 *
 * Every table is listed, including ungranted ones. A matrix that shows only what
 * is already granted cannot answer "what else is there", which is the question
 * an auditor actually asks.
 */

/**
 * Three states, not two.
 *
 * A table grant carries four flags, and collapsing them to "granted / not"
 * threw away the distinction that matters most: a role that may *read* a table
 * and a role that may *change* it are not the same role. This screen offered
 * only the first, so the write half of the grant model was unreachable from the
 * interface -- exactly what the vanilla console's three-way control did offer.
 *
 * `write` implies `read`: there is no state where a role may update rows it
 * cannot see, and offering one would produce a grant the SQL layer treats as
 * incoherent.
 */
type Access = 'none' | 'read' | 'write';

const FLAGS: Record<Access, {
  can_read: boolean;
  can_insert: boolean;
  can_update: boolean;
  can_delete: boolean;
}> = {
  none: { can_read: false, can_insert: false, can_update: false, can_delete: false },
  read: { can_read: true, can_insert: false, can_update: false, can_delete: false },
  write: { can_read: true, can_insert: true, can_update: true, can_delete: true },
};

/** Colour per level. `write` is the one worth a warning colour. */
const TONE: Record<Access, 'neutral' | 'ok' | 'err'> = {
  none: 'neutral',
  read: 'ok',
  write: 'err',
};

/** The order the levels are offered in, least to most. */
const CYCLE: Access[] = ['none', 'read', 'write'];

interface TableGrantRow {
  role: string;
  table: string;
  can_read?: boolean;
  can_insert?: boolean;
  can_update?: boolean;
  can_delete?: boolean;
}

/** Read straight off the grant, the same derivation the vanilla console used. */
function accessOf(grant: TableGrantRow | undefined): Access {
  if (!grant || !grant.can_read) return 'none';
  return grant.can_insert || grant.can_update || grant.can_delete ? 'write' : 'read';
}

interface Column {
  name: string;
  data_type: string;
  is_primary_key: boolean;
  is_generated: boolean;
}

interface Resource {
  schema: string | null;
  table: string;
  columns: Column[];
}

interface Grants {
  version: number;
  roles: Role[];
  resources: Resource[];
  tables: TableGrantRow[];
  columns: Array<{ role: string; table: string; column: string; can_read: boolean }>;
}

/**
 * The key the catalog and the grant tables both use.
 *
 * Guarded against double-prefixing: some sources report `schema: "ecommerce"`
 * with `table: "ecommerce.addresses"` -- already qualified -- and joining them
 * blindly produced `ecommerce.ecommerce.addresses`, which matched no grant and
 * read as a nonsense table name on screen.
 */
const key = (resource: Resource) => {
  const table = resource.table;
  if (!resource.schema) return table;
  return table.startsWith(`${resource.schema}.`) ? table : `${resource.schema}.${table}`;
};

export default function PermissionsPage() {
  const { t } = useLocale();
  const tenant = useConcreteScope();

  // Defaults to `analyst`: it is the role whose access anybody actually comes
  // here to tune. Admins already have what they need and viewers are the floor.
  const [role, setRole] = React.useState<Role>('analyst');
  const [filter, setFilter] = React.useState('');
  const [data, setData] = React.useState<Grants | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);

  const base = `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/grants`;

  const load = React.useCallback(async () => {
    if (!tenant) return;
    setData(null);
    try {
      setData(await api<Grants>(`${base}?role=${encodeURIComponent(role)}`));
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
    // `base` is derived from `tenant`, so listing it would re-run this on every
    // render without adding anything.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenant, role]);

  React.useEffect(() => {
    void load();
  }, [load]);

  // The access *level* per table, not merely whether a grant exists.
  const access = React.useMemo(() => {
    const map = new Map<string, Access>();
    for (const row of data?.tables ?? []) map.set(row.table, accessOf(row));
    return map;
  }, [data]);

  const setAccess = async (table: string, next: Access) => {
    setBusy(table);
    try {
      if (next === 'none') {
        await del(
          `${base}/table?role=${encodeURIComponent(role)}&table=${encodeURIComponent(table)}`,
        );
      } else {
        // `autofill_columns` fills in the table's column grants so granting a
        // wide table does not mean ticking ninety of them by hand; it never
        // overwrites a column already decided. It defaults to true server-side
        // and is passed explicitly so the behaviour is visible here rather than
        // inherited silently.
        await put(`${base}/table`, {
          role,
          table,
          ...FLAGS[next],
          autofill_columns: true,
        });
      }
      toastSuccess(t('perm.setTo', { state: t(`perm.access.${next}`) }));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const needle = filter.trim().toLowerCase();
  const visible = (data?.resources ?? []).filter(
    (resource) => !needle || key(resource).toLowerCase().includes(needle),
  );

  return (
    <PageBody>
      <PageHeader
        title={t('tab.permissions')}
        description={t('perm.blurb', { workspace: tenant })}
      />

      <Toolbar>
        <ScopePicker id="perm-scope" />

        <div className="flex items-center gap-2">
          <Label htmlFor="perm-role">{t('mem.role')}</Label>
          <Select value={role} onValueChange={(next) => setRole(next as Role)}>
            <SelectTrigger id="perm-role" className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(data?.roles ?? ['admin', 'analyst', 'viewer']).map((name) => (
                <SelectItem key={name} value={name}>{name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <Input
          type="search"
          className="w-64"
          placeholder={t('perm.search')}
          value={filter}
          onChange={(event) => setFilter(event.currentTarget.value)}
        />

        {data ? (
          <span className="text-[0.8125rem] text-muted-foreground">
            {t('perm.summary', {
              read: [...access.values()].filter((a) => a === 'read').length,
              write: [...access.values()].filter((a) => a === 'write').length,
              total: data.resources.length,
            })}
          </span>
        ) : null}
      </Toolbar>

      {error ? (
        <ErrorState title={t('perm.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : data === null ? (
        <LoadingRows rows={10} />
      ) : visible.length === 0 ? (
        <EmptyState icon={<ShieldCheck className="size-7" />} title={t('perm.noTables')} />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('perm.table')}</Th>
                <Th>{t('perm.columns')}</Th>
                <Th>{t('ov.status')}</Th>
                <Th />
              </Tr>
            </thead>
            <Tbody>
              {visible.map((resource) => {
                const name = key(resource);
                const current = access.get(name) ?? 'none';
                return (
                  <Tr key={name}>
                    <Td className="font-mono text-[0.78rem]">{name}</Td>
                    <Td className="text-muted-foreground">
                      {t('perm.columnCount', { n: resource.columns.length })}
                    </Td>
                    <Td>
                      <Badge tone={TONE[current]}>{t(`perm.access.${current}`)}</Badge>
                    </Td>
                    <Td>
                      {/* Three explicit buttons rather than one cycling toggle.
                          A control that steps none -> read -> write is two
                          clicks away from what you meant and gives no way to
                          see the third state without entering it; granting
                          write access by accident is not a mistake worth
                          designing in. `aria-pressed` so the current state is
                          announced, not just coloured. */}
                      <div className="flex gap-1" role="group" aria-label={name}>
                        {CYCLE.map((level) => (
                          <Button
                            key={level}
                            size="sm"
                            aria-pressed={current === level}
                            variant={
                              current === level
                                ? level === 'write'
                                  ? 'danger'
                                  : 'primary'
                                : 'outline'
                            }
                            disabled={busy === name || current === level}
                            onClick={() => void setAccess(name, level)}
                          >
                            {t(`perm.access.${level}`)}
                          </Button>
                        ))}
                      </div>
                    </Td>
                  </Tr>
                );
              })}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}
    </PageBody>
  );
}
