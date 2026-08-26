import { FileCog } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { useLocale } from '@/i18n';
import { api } from '@/lib/api';
import { relative } from '@/lib/time';

/**
 * The configuration catalog: cubes, instruction baselines, domains, eval sets.
 *
 * The point of moving these into the control plane was that an administrator
 * changes a cube, the write lands in `config_files`, the manifest recompiles in
 * the same transaction, and every worker picks it up within the refresh window.
 * Nothing is written to disk and nothing needs a rebuilt image.
 *
 * Read-only here. Editing is platform-admin and validated against the runtime's
 * own types before it is stored -- a saved cube that does not compile is worse
 * than a rejected one, because the deployment keeps running until the next cold
 * start and then fails somewhere else entirely. That editor is a screen of its
 * own; listing what exists, with its version and whether it parses, is the half
 * that was unreachable from any interface at all.
 */

interface ConfigFile {
  path: string;
  kind: string;
  scope: string;
  tenant_id: string;
  project: string;
  version: number;
  checksum: string;
  updated_at: string;
  updated_by: string;
  /** False means the stored text no longer compiles against the current types. */
  parsed: boolean;
}

export default function ConfigurationPage() {
  const { t, locale } = useLocale();
  const [rows, setRows] = React.useState<ConfigFile[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [filter, setFilter] = React.useState('');

  const load = React.useCallback(async () => {
    try {
      const body = await api<{ items: ConfigFile[] }>('/api/vanna/v2/admin/config/files');
      setRows(body.items ?? []);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const needle = filter.trim().toLowerCase();
  const visible = (rows ?? []).filter(
    (row) => !needle || row.path.toLowerCase().includes(needle) || row.kind.includes(needle),
  );
  const broken = (rows ?? []).filter((row) => !row.parsed).length;

  return (
    <PageBody>
      <PageHeader title={t('nav.configuration')} description={t('cfg.blurb')} />

      <Toolbar>
        <Input
          type="search"
          className="w-72"
          placeholder={t('cfg.search')}
          value={filter}
          onChange={(event) => setFilter(event.currentTarget.value)}
        />
        {broken > 0 ? <Badge tone="err">{t('cfg.broken', { n: broken })}</Badge> : null}
      </Toolbar>

      {error ? (
        <ErrorState title={t('cfg.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingRows rows={8} />
      ) : visible.length === 0 ? (
        <EmptyState icon={<FileCog className="size-7" />} title={t('cfg.none')} />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('cfg.path')}</Th>
                <Th>{t('cfg.kind')}</Th>
                <Th>{t('cfg.scope')}</Th>
                <Th>{t('cfg.version')}</Th>
                <Th>{t('ov.status')}</Th>
                <Th>{t('cfg.updated')}</Th>
              </Tr>
            </thead>
            <Tbody>
              {visible.map((file) => (
                <Tr key={file.path}>
                  <Td className="font-mono text-[0.78rem]">{file.path}</Td>
                  <Td className="font-mono text-[0.75rem]">{file.kind}</Td>
                  <Td>
                    {file.scope === 'global' ? (
                      <Badge tone="neutral">{t('cfg.global')}</Badge>
                    ) : (
                      <span className="font-mono text-[0.75rem]">
                        {file.tenant_id || file.project || file.scope}
                      </span>
                    )}
                  </Td>
                  <Td className="tabular-nums">v{file.version}</Td>
                  <Td>
                    {file.parsed ? (
                      <Badge tone="ok">{t('cfg.parses')}</Badge>
                    ) : (
                      <Badge tone="err">{t('cfg.doesNotParse')}</Badge>
                    )}
                  </Td>
                  <Td className="text-muted-foreground">
                    {relative(file.updated_at, t, locale)}
                    <span className="block text-[0.72rem]">{file.updated_by}</span>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}
    </PageBody>
  );
}
