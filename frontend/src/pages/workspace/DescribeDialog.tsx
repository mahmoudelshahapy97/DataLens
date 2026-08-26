import * as React from 'react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useLocale } from '@/i18n';
import { api, patch } from '@/lib/api';
import { toastError, toastSuccess } from '@/lib/toast';

/**
 * Write down what a table or a column actually means.
 *
 * This is the highest-leverage screen in the product and the rewrite had no
 * equivalent: a warehouse column called `st` or `flag_2` is unanswerable until
 * somebody says what it holds, and these annotations are folded into the
 * description the agent is shown. The vanilla console had `describeTableSheet`
 * and `describeColumnSheet`; this is both, because they differ only in which
 * endpoint they PATCH and which extra fields they offer.
 *
 * Two things the API forces, and they are worth stating:
 *
 * **Annotations are keyed on the catalog**, so they belong to `(tenant,
 * data_source, table)`. The route resolves the data source itself; this screen
 * only passes the table key.
 *
 * **A GET returns the description with the code book already folded in**, so a
 * blind round-trip would write the merged text back as the raw description and
 * duplicate the labels on the next read. The dialog therefore loads the stored
 * annotation before editing rather than seeding itself from what the schema
 * screen is rendering.
 */

interface TableAnnotation {
  description?: string | null;
  display_name?: string | null;
}

interface ColumnAnnotation extends TableAnnotation {
  unit?: string | null;
  value_labels?: Record<string, string> | null;
  sensitivity?: string | null;
}

type Loaded = {
  annotation: TableAnnotation | null;
  columns: Array<{ column: string } & ColumnAnnotation> | null;
};

export function DescribeDialog({
  tenant,
  tableKey,
  /** Omit for a table; pass a column name to describe that column instead. */
  column,
  label,
  onClose,
  onSaved,
}: {
  tenant: string;
  tableKey: string;
  column?: string;
  label: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useLocale();

  const [description, setDescription] = React.useState('');
  const [displayName, setDisplayName] = React.useState('');
  const [unit, setUnit] = React.useState('');
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState(false);

  const base = `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/catalog`;

  // Load what is stored, not what the table is rendering: see the note above.
  React.useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const body = await api<Loaded>(`${base}/tables/${encodeURIComponent(tableKey)}`);
        if (!current) return;
        const source = column
          ? (body.columns ?? []).find((row) => row.column === column)
          : body.annotation;
        setDescription(source?.description ?? '');
        setDisplayName(source?.display_name ?? '');
        setUnit((source as ColumnAnnotation | undefined)?.unit ?? '');
      } catch {
        // Nothing stored yet is the common case for a first description, and it
        // is indistinguishable from a read failure here -- so start empty
        // rather than blocking the edit.
      } finally {
        if (current) setLoading(false);
      }
    })();
    return () => {
      current = false;
    };
  }, [base, tableKey, column]);

  async function save() {
    setBusy(true);
    // `null` rather than "" for a cleared field: the API treats null as "leave
    // alone" only for keys that are absent, and an empty string is a real value
    // meaning "no description".
    const body: ColumnAnnotation = {
      description: description.trim(),
      display_name: displayName.trim() || null,
    };
    if (column) body.unit = unit.trim() || null;

    const url = column
      ? `${base}/columns/${encodeURIComponent(tableKey)}/${encodeURIComponent(column)}`
      : `${base}/tables/${encodeURIComponent(tableKey)}`;

    try {
      await patch(url, body);
      toastSuccess(t('common.saved'));
      onSaved();
      onClose();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {column ? t('schema.describeColumn', { name: label }) : t('schema.describeTable', { name: label })}
          </DialogTitle>
          <DialogDescription>
            {column ? t('schema.describeColumnHelp') : t('schema.describeHelp')}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div>
            <Label htmlFor="desc-text">{t('schema.description')}</Label>
            <Textarea
              id="desc-text"
              rows={4}
              dir="auto"
              disabled={loading}
              value={description}
              onChange={(event) => setDescription(event.currentTarget.value)}
              placeholder={t('schema.descriptionHint')}
            />
          </div>

          <div>
            <Label htmlFor="desc-display">{t('schema.displayName')}</Label>
            <Input
              id="desc-display"
              dir="auto"
              disabled={loading}
              value={displayName}
              onChange={(event) => setDisplayName(event.currentTarget.value)}
            />
          </div>

          {column ? (
            <div>
              <Label htmlFor="desc-unit">{t('schema.unit')}</Label>
              <Input
                id="desc-unit"
                disabled={loading}
                value={unit}
                onChange={(event) => setUnit(event.currentTarget.value)}
                placeholder={t('schema.unitHint')}
              />
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="primary" disabled={busy || loading} onClick={() => void save()}>
            {busy ? t('common.saving') : t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
