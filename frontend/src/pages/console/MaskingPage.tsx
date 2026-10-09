import { EyeOff } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
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
import { api, put } from '@/lib/api';
import { toast, toastError } from '@/lib/toast';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * Obscuring a column that is still readable.
 *
 * This screen has to be honest about something awkward: **masking is the weakest
 * of the three things that can happen to a column**, and the strongest option is
 * not on this page at all -- it is `can_read = false` on the Permissions screen,
 * which drops the column from the caller's world entirely so that naming it is
 * an unknown-column error with nothing to probe.
 *
 * A mask exists because the alternative people actually reach for is granting the
 * column outright and hoping. So each strategy states what it leaks, next to the
 * control that turns it on, rather than in documentation nobody opens.
 *
 * The enforcement is not here and not in the result set: the expression is
 * rewritten inside the model's CTE, so a user's subquery sits above it and cannot
 * undo it.
 */

interface ColumnGrant {
  role: string;
  table: string;
  column: string;
  can_read: boolean;
  can_filter: boolean;
  can_aggregate: boolean;
  can_write: boolean;
  mask: string;
}

interface GrantsResponse {
  version: number;
  roles: string[];
  columns: ColumnGrant[];
}

/** Ordered most protective first, which is the order somebody should read them. */
const STRATEGIES = ['null', 'hash', 'partial', 'none'] as const;

function toneFor(mask: string): 'good' | 'warn' | 'neutral' {
  if (mask === 'none') return 'neutral';
  if (mask === 'null' || mask === 'hash') return 'good';
  return 'warn'; // 'partial' leaks more than the others -- a real severity, not a category.
}

export default function MaskingPage() {
  const { t } = useLocale();
  const tenant = useConcreteScope();

  const [role, setRole] = React.useState('analyst');
  const [roles, setRoles] = React.useState<string[]>(['admin', 'analyst', 'viewer']);
  const [columns, setColumns] = React.useState<ColumnGrant[]>([]);
  const [filter, setFilter] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState<string | null>(null);

  const load = React.useCallback(
    async (alive: () => boolean = () => true) => {
      if (!tenant) return;
      try {
        const body = await api<GrantsResponse>(
          `/api/vanna/v2/admin/tenants/${tenant}/grants?role=${encodeURIComponent(role)}`,
        );
        if (!alive()) return;
        setRoles(body.roles ?? ['admin', 'analyst', 'viewer']);
        // Only readable columns can carry a mask -- an unreadable one is already
        // gone, and offering it here would present protection that is not there.
        setColumns((body.columns ?? []).filter((column) => column.can_read));
        setError(null);
      } catch (caught) {
        if (!alive()) return;
        setError((caught as Error).message);
      } finally {
        if (alive()) setLoading(false);
      }
    },
    [tenant, role],
  );

  React.useEffect(() => {
    setLoading(true);
    let current = true;
    void load(() => current);
    return () => {
      current = false;
    };
  }, [load]);

  async function setMask(grant: ColumnGrant, mask: string) {
    const key = `${grant.table}.${grant.column}`;
    setSaving(key);
    try {
      await put(`/api/vanna/v2/admin/tenants/${tenant}/grants/column`, {
        role: grant.role,
        table: grant.table,
        column: grant.column,
        can_read: grant.can_read,
        can_filter: grant.can_filter,
        can_aggregate: grant.can_aggregate,
        can_write: grant.can_write,
        mask,
      });
      setColumns((current) =>
        current.map((c) => (c.table === grant.table && c.column === grant.column ? { ...c, mask } : c)),
      );
      toast(t('common.saved'));
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setSaving(null);
    }
  }

  const visible = React.useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return columns;
    return columns.filter(
      (column) =>
        column.table.toLowerCase().includes(needle) ||
        column.column.toLowerCase().includes(needle),
    );
  }, [columns, filter]);

  // `|| 'none'` matters: a backend that predates the mask column omits the
  // field entirely, and `undefined !== 'none'` counted every column as masked.
  const masked = columns.filter((column) => (column.mask || 'none') !== 'none').length;

  return (
    <PageBody>
      <PageHeader title={t('nav.masking')} description={t('mask.sub')} />

      {/* Stated once, plainly, above the controls. Somebody arriving here is
          about to decide how much of a column to reveal, and the strongest
          option lives on a different screen. */}
      <Card className="mb-4 border-warn/30 bg-warn/5 p-3">
        <p className="text-[0.8125rem]">{t('mask.weakerThanWithholding')}</p>
      </Card>

      <Toolbar>
        <ScopePicker id="mask-scope" />

        <div className="flex items-center gap-2">
          <Label htmlFor="mask-role">{t('perm.role')}</Label>
          <Select value={role} onValueChange={setRole}>
            <SelectTrigger id="mask-role" className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {roles.map((option) => (
                <SelectItem key={option} value={option}>
                  {option}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <Input
          className="min-w-[200px] flex-1"
          type="search"
          value={filter}
          placeholder={t('schema.filter')}
          aria-label={t('schema.filter')}
          onChange={(event) => setFilter(event.target.value)}
        />

        {/* A column being masked is the desired state, not a warning. */}
        <Badge tone={masked ? 'info' : 'neutral'}>
          {masked} {t('mask.masked')}
        </Badge>
      </Toolbar>

      {loading ? (
        <LoadingRows rows={10} />
      ) : error ? (
        <ErrorState title={t('history.reload')} detail={error} onRetry={() => void load()} />
      ) : visible.length === 0 ? (
        <EmptyState
          icon={<EyeOff className="size-7" />}
          title={columns.length ? t('schema.noMatch') : t('mask.noColumns')}
          hint={columns.length ? undefined : t('mask.noColumnsHint')}
        />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('perm.table')}</Th>
                <Th>{t('schema.colColumn')}</Th>
                <Th className="w-52">{t('mask.strategy')}</Th>
                <Th>{t('mask.leaks')}</Th>
              </Tr>
            </thead>
            <Tbody>
              {visible.map((grant) => {
                const key = `${grant.table}.${grant.column}`;
                return (
                  <Tr key={key}>
                    <Td className="font-mono text-[0.78rem]">{grant.table}</Td>
                    <Td className="font-mono text-[0.78rem]">{grant.column}</Td>
                    <Td>
                      <Select
                        value={grant.mask || 'none'}
                        disabled={saving === key}
                        onValueChange={(next) => void setMask(grant, next)}
                      >
                        <SelectTrigger aria-label={`${t('mask.strategy')} ${key}`}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {STRATEGIES.map((strategy) => (
                            <SelectItem key={strategy} value={strategy}>
                              {t(`mask.${strategy}`)}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </Td>
                    {/* What each choice actually gives away, on the row that
                        made the choice. */}
                    <Td>
                      <div className="flex items-center gap-2">
                        <Badge tone={toneFor(grant.mask || 'none')}>
                          {t(`mask.${grant.mask || 'none'}`)}
                        </Badge>
                        <span className="text-[0.78rem] text-muted-foreground">
                          {t(`mask.${grant.mask || 'none'}.leaks`)}
                        </span>
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
