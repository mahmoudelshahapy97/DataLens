import { Pencil, Plus, Trash2 } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { usePrompt } from '@/components/primitives/confirm';
import { toast, toastError } from '@/lib/toast';

import { WorkspaceEditor } from './WorkspaceEditor';
import { useLocale } from '@/i18n';
import { api, del } from '@/lib/api';
import { relative } from '@/lib/time';
import type { WorkspaceUsage } from '@/types';

/**
 * Every workspace on the platform.
 *
 * This was read-only, on the reasoning that creating a workspace and repointing
 * one at another database are the two operations that can hand a customer
 * somebody else's data, and that both were "still done in the previous console".
 * They are not: /admin/ redirects here now, so that reasoning had quietly become
 * an absence rather than a deferral.
 *
 * The risk it named is real, so the friction moved rather than disappearing:
 * repointing a live workspace sits behind a deliberate unlock in the editor, and
 * deleting one requires typing its id. See `WorkspaceEditor` for why the
 * connection string in particular earns that.
 */
export default function WorkspacesPage() {
  const { t, locale } = useLocale();
  const prompt = usePrompt();
  const [rows, setRows] = React.useState<WorkspaceUsage[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [editing, setEditing] = React.useState<WorkspaceUsage | 'new' | null>(null);

  const load = React.useCallback(async () => {
    try {
      const body = await api<{ tenants: WorkspaceUsage[] }>('/api/vanna/v2/admin/tenants');
      setRows(body.tenants ?? []);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const number = (value: number) => new Intl.NumberFormat(locale).format(value || 0);

  async function remove(workspace: WorkspaceUsage) {
    // Typed confirmation, not a yes/no. Deleting a workspace cascades its
    // members, starters, saved queries and dashboards; a dialog you can dismiss
    // by reflex is the wrong shape for that.
    const typed = await prompt.ask({
      title: t('ws.deleteTitle'),
      body: t('ws.deleteConfirm', { workspace: workspace.name }),
      label: t('ws.deleteType', { id: workspace.id }),
      placeholder: workspace.id,
      confirmLabel: t('common.delete'),
    });
    if (typed === null) return;
    if (typed.trim() !== workspace.id) {
      toastError(t('ws.deleteMismatch'));
      return;
    }
    try {
      await del(`/api/vanna/v2/admin/tenants/${encodeURIComponent(workspace.id)}`);
      setRows((current) => current?.filter((w) => w.id !== workspace.id) ?? current);
      toast(t('common.deleted'));
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  return (
    <PageBody>
      {prompt.dialog}
      <PageHeader
        title={t('tab.tenants')}
        description={t('wsp.blurb')}
        actions={
          <Button variant="primary" onClick={() => setEditing('new')}>
            <Plus />
            {t('ws.create')}
          </Button>
        }
      />

      {error ? (
        <ErrorState title={t('wsp.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingRows rows={8} />
      ) : rows.length === 0 ? (
        <EmptyState title={t('wsp.none')} />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('ov.workspace')}</Th>
                <Th>{t('ws.dataSource')}</Th>
                <Th>{t('kpi.members')}</Th>
                <Th>{t('kpi.questions')}</Th>
                <Th>{t('ws.writes')}</Th>
                <Th>{t('ws.ownKey')}</Th>
                <Th>{t('ov.lastActivity')}</Th>
                <Th className="w-24" />
              </Tr>
            </thead>
            <Tbody>
              {rows.map((workspace) => (
                <Tr key={workspace.id}>
                  <Td>
                    <span className="font-medium">{workspace.name}</span>
                    {workspace.is_active ? null : (
                      <Badge tone="err" className="ms-2">{t('ov.inactive')}</Badge>
                    )}
                    <span className="block font-mono text-[0.72rem] text-muted-foreground">
                      {workspace.id}
                    </span>
                  </Td>
                  {/* Credential-free by construction: `describe_data_source`
                      renders the shape of the connection, never its password. */}
                  <Td className="font-mono text-[0.75rem]">{workspace.data_source}</Td>
                  <Td>{number(workspace.usage.members)}</Td>
                  <Td>{number(workspace.usage.questions)}</Td>
                  <Td>
                    {workspace.allow_writes ? (
                      <Badge tone="warn">{t('wsp.on')}</Badge>
                    ) : (
                      <Badge tone="neutral">{t('wsp.off')}</Badge>
                    )}
                  </Td>
                  <Td>
                    {workspace.allow_byo_key === false ? (
                      <Badge tone="neutral">{t('wsp.off')}</Badge>
                    ) : (
                      <Badge tone="ok">{t('wsp.on')}</Badge>
                    )}
                  </Td>
                  <Td className="text-muted-foreground">
                    {workspace.usage.last_activity
                      ? relative(workspace.usage.last_activity, t, locale)
                      : t('ov.never')}
                  </Td>
                  <Td>
                    <div className="flex justify-end gap-0.5">
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={t('common.edit')}
                        title={t('common.edit')}
                        onClick={() => setEditing(workspace)}
                      >
                        <Pencil />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={t('common.delete')}
                        title={t('common.delete')}
                        onClick={() => void remove(workspace)}
                      >
                        <Trash2 />
                      </Button>
                    </div>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}

      {editing ? (
        <WorkspaceEditor
          workspace={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
        />
      ) : null}
    </PageBody>
  );
}
