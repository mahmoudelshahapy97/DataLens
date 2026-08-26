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

type Access = 'none' | 'read';

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
  tables: Array<{ role: string; table: string; can_read: boolean }>;
  columns: Array<{ role: string; table: string; column: string; can_read: boolean }>;
}

const key = (resource: Resource) =>
  resource.schema ? `${resource.schema}.${resource.table}` : resource.table;

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

  const granted = React.useMemo(() => {
    const set = new Set<string>();
    for (const row of data?.tables ?? []) if (row.can_read) set.add(row.table);
    return set;
  }, [data]);

  const setAccess = async (table: string, access: Access) => {
    setBusy(table);
    try {
      if (access === 'none') {
        await del(`${base}/table?role=${encodeURIComponent(role)}&table=${encodeURIComponent(table)}`);
      } else {
        await put(`${base}/table`, { role, table, can_read: true });
      }
      toastSuccess(t('common.saved'));
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
            {t('perm.grantedOf', { n: granted.size, total: data.resources.length })}
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
                const allowed = granted.has(name);
                return (
                  <Tr key={name}>
                    <Td className="font-mono text-[0.78rem]">{name}</Td>
                    <Td className="text-muted-foreground">
                      {t('perm.columnCount', { n: resource.columns.length })}
                    </Td>
                    <Td>
                      {allowed ? (
                        <Badge tone="ok">{t('perm.access.read')}</Badge>
                      ) : (
                        <Badge tone="neutral">{t('perm.access.none')}</Badge>
                      )}
                    </Td>
                    <Td>
                      <Button
                        size="sm"
                        variant={allowed ? 'danger' : 'primary'}
                        disabled={busy === name}
                        onClick={() => void setAccess(name, allowed ? 'none' : 'read')}
                      >
                        {allowed ? t('perm.revoke') : t('perm.grant')}
                      </Button>
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
