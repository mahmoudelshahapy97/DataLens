import { Key, PencilLine, RefreshCw, Table2 } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { useSession } from '@/app/session';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useLocale } from '@/i18n';
import { api, post } from '@/lib/api';
import { toast, toastError } from '@/lib/toast';

import { DescribeDialog } from './DescribeDialog';

/**
 * What the agent can see of the warehouse.
 *
 * The list is not the database -- it is *this caller's* view of it. A table with
 * no grant is not shown here because it is not shown to the agent either: it is
 * dropped from the caller's world entirely, so naming it is an unknown-column
 * error rather than a permission error. Two people can open this page and see
 * different tables, which is the same property dashboards have.
 *
 * The layer switch matters when a workspace has a semantic manifest. `active` is
 * what the agent is actually given -- models, with their access rules folded in;
 * `physical` is the raw scan. When they disagree, the active layer is the truth.
 */

interface Column {
  name: string;
  data_type: string;
  nullable: boolean;
  is_primary_key?: boolean;
  description?: string | null;
}

interface TableInfo {
  name: string;
  schema: string | null;
  description: string | null;
  row_count_estimate: number | null;
  columns: Column[];
}

interface SchemaResponse {
  dialect: string;
  data_source: string;
  semantic: boolean;
  layer: string;
  tables: TableInfo[];
}

export default function SchemaPage() {
  const { t } = useLocale();
  const { isWorkspaceAdmin, me } = useSession();
  // null = closed. `column: null` means the table itself.
  const [describing, setDescribing] = React.useState<{ column: string | null } | null>(null);

  const [data, setData] = React.useState<SchemaResponse | null>(null);
  const [filter, setFilter] = React.useState('');
  const [selected, setSelected] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [scanning, setScanning] = React.useState(false);

  const load = React.useCallback(async (alive: () => boolean = () => true) => {
    try {
      const body = await api<SchemaResponse>('/api/vanna/v2/schema');
      if (!alive()) return;
      setData(body);
      setSelected((current) => current ?? body.tables?.[0]?.name ?? null);
      setError(null);
    } catch (caught) {
      if (!alive()) return;
      setError((caught as Error).message);
    } finally {
      if (alive()) setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    let current = true;
    void load(() => current);
    return () => {
      current = false;
    };
  }, [load]);

  async function rescan() {
    setScanning(true);
    try {
      // `schema.scanned` is "Scanned {tables} tables, {columns} columns" -- the
      // numbers are in the response and were being dropped, so the toast read
      // with the placeholders still in it.
      const report = await post<{ tables_scanned?: number; columns_profiled?: number }>(
        '/api/vanna/v2/schema/rescan',
        {},
      );
      toast(
        t('schema.scanned', {
          tables: report?.tables_scanned ?? 0,
          columns: report?.columns_profiled ?? 0,
        }),
      );
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setScanning(false);
    }
  }

  const tables = data?.tables ?? [];
  const visible = React.useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return tables;
    return tables.filter(
      (table) =>
        table.name.toLowerCase().includes(needle) ||
        table.columns.some((column) => column.name.toLowerCase().includes(needle)),
    );
  }, [tables, filter]);

  const active = visible.find((table) => table.name === selected) ?? visible[0] ?? null;

  return (
    <PageBody>
      <PageHeader
        title={t('schema.title')}
        description={data?.semantic ? t('schema.subSemantic') : t('schema.subPhysical')}
        actions={
          isWorkspaceAdmin ? (
            <Button onClick={() => void rescan()} disabled={scanning}>
              <RefreshCw />
              {scanning ? t('schema.scanning') : t('schema.rescan')}
            </Button>
          ) : null
        }
      />

      <Toolbar>
        <Input
          className="min-w-[220px] flex-1"
          type="search"
          value={filter}
          placeholder={t('schema.filter')}
          aria-label={t('schema.filter')}
          onChange={(event) => setFilter(event.target.value)}
        />
        {data ? (
          <>
            <Badge tone="neutral">{data.dialect}</Badge>
            {data.semantic ? <Badge tone="admin">{t('schema.showSemantic')}</Badge> : null}
            <Badge tone="neutral">
              {tables.length} {t('schema.title')}
            </Badge>
          </>
        ) : null}
      </Toolbar>

      {loading ? (
        <LoadingRows rows={10} />
      ) : error ? (
        <ErrorState title={t('history.reload')} detail={error} onRetry={() => void load()} />
      ) : tables.length === 0 ? (
        <EmptyState
          icon={<Table2 className="size-7" />}
          title={t('schema.nothingScanned')}
          hint={t('schema.scanToBuild')}
        />
      ) : visible.length === 0 ? (
        <EmptyState title={t('schema.noMatch')} />
      ) : (
        <div className="grid gap-3 lg:grid-cols-[260px_minmax(0,1fr)]">
          <nav
            className="max-h-[70vh] overflow-y-auto rounded-md border border-border bg-surface"
            aria-label={t('schema.title')}
          >
            <ul>
              {visible.map((table) => (
                <li key={table.name}>
                  <button
                    type="button"
                    onClick={() => setSelected(table.name)}
                    className={[
                      'flex w-full items-center justify-between gap-2 border-b border-border-soft px-3 py-2 text-start text-[0.8125rem]',
                      table.name === active?.name
                        ? 'bg-primary-soft font-medium text-primary'
                        : 'hover:bg-rail-hover',
                    ].join(' ')}
                  >
                    <span className="truncate font-mono">{table.name}</span>
                    <span className="shrink-0 text-[0.75rem] text-muted-foreground">
                      {table.columns.length}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </nav>

          {active ? (
            <div className="min-w-0">
              <div className="mb-3 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="font-mono text-[0.95rem] font-semibold">{active.name}</h3>
                  <p className="mt-1 text-[0.8125rem] text-muted-foreground" dir="auto">
                    {active.description || t('schema.noDescription')}
                  </p>
                </div>
                {/* Admin-only, and only over the physical layer: an annotation is
                    keyed on a catalog table, and a semantic model is not one --
                    the same reason the vanilla console hid these buttons. */}
                {isWorkspaceAdmin && data?.layer !== 'active' ? (
                  <Button className="shrink-0" onClick={() => setDescribing({ column: null })}>
                    <PencilLine />
                    {t('schema.describe')}
                  </Button>
                ) : null}
              </div>

              <ScrollX className="rounded-md border border-border">
                <DataTable>
                  <thead>
                    <Tr>
                      <Th>{t('schema.colColumn')}</Th>
                      <Th>{t('schema.colType')}</Th>
                      <Th className="w-20">{t('schema.colKey')}</Th>
                      <Th>{t('schema.colNotes')}</Th>
                      {isWorkspaceAdmin && data?.layer !== 'active' ? <Th className="w-10" /> : null}
                    </Tr>
                  </thead>
                  <Tbody>
                    {active.columns.map((column) => (
                      <Tr key={column.name}>
                        <Td className="font-mono text-[0.78rem]">{column.name}</Td>
                        <Td className="font-mono text-[0.78rem] text-muted-foreground">
                          {column.data_type}
                        </Td>
                        <Td>
                          {column.is_primary_key ? (
                            <Badge tone="admin">
                              <Key className="size-3" />
                              PK
                            </Badge>
                          ) : null}
                        </Td>
                        <Td dir="auto" className="text-muted-foreground">
                          {column.description || ''}
                        </Td>
                        {isWorkspaceAdmin && data?.layer !== 'active' ? (
                          <Td className="w-10">
                            <Button
                              variant="ghost"
                              size="icon"
                              aria-label={t('schema.describeColumn', { name: column.name })}
                              onClick={() => setDescribing({ column: column.name })}
                            >
                              <PencilLine className="size-3.5" />
                            </Button>
                          </Td>
                        ) : null}
                      </Tr>
                    ))}
                  </Tbody>
                </DataTable>
              </ScrollX>
            </div>
          ) : null}
        </div>
      )}
      {describing && active && me?.tenant ? (
        <DescribeDialog
          tenant={me.tenant.id}
          tableKey={active.schema ? `${active.schema}.${active.name}` : active.name}
          column={describing.column ?? undefined}
          label={describing.column ?? active.name}
          onClose={() => setDescribing(null)}
          onSaved={() => void load()}
        />
      ) : null}
    </PageBody>
  );
}
