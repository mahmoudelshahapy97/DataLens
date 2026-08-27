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
import { post } from '@/lib/api';
import { toastError, toastSuccess } from '@/lib/toast';

/**
 * Save a statement so it can be reused -- and put on a dashboard.
 *
 * Nothing could create a saved query. `SavedPage` listed and deleted them, and
 * the only writer was the seed script, so a workspace that had never been seeded
 * had none and no way to get one. That broke the whole dashboard-building path,
 * because a tile holds a *reference* to a saved query rather than a copy of its
 * SQL: no saved queries meant no tiles, which is why a new dashboard could only
 * ever say "No tiles".
 *
 * The vanilla app opened this from three places -- a History row, the Saved
 * page, and the SQL result panel. The first two are wired here; History is the
 * important one, because it already holds every question asked with the SQL it
 * produced, so the statement worth keeping is usually one already in it.
 */
export function SaveQueryDialog({
  question = '',
  sql = '',
  onClose,
  onSaved,
}: {
  question?: string;
  sql?: string;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const { t } = useLocale();

  // Seeded from the row, then the user's own edits win -- so opening this on a
  // history row is one field away from done.
  const [title, setTitle] = React.useState('');
  const [asked, setAsked] = React.useState(question);
  const [statement, setStatement] = React.useState(sql);
  const [busy, setBusy] = React.useState(false);

  // The server requires both, so the button should not pretend otherwise.
  const ready = title.trim().length > 0 && statement.trim().length > 0;

  async function save() {
    setBusy(true);
    try {
      await post('/api/vanna/v2/saved-queries', {
        title: title.trim(),
        sql: statement.trim(),
        question: asked.trim(),
      });
      toastSuccess(t('saved.created'));
      onSaved?.();
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
          <DialogTitle>{t('saved.newQuery')}</DialogTitle>
          <DialogDescription>{t('saved.newQueryHelp')}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div>
            <Label htmlFor="sv-title">{t('saved.fieldTitle')}</Label>
            <Input
              id="sv-title"
              autoFocus
              dir="auto"
              value={title}
              placeholder={t('saved.titleHint')}
              onChange={(event) => setTitle(event.currentTarget.value)}
            />
          </div>

          <div>
            <Label htmlFor="sv-question">{t('saved.fieldQuestion')}</Label>
            <Input
              id="sv-question"
              dir="auto"
              value={asked}
              onChange={(event) => setAsked(event.currentTarget.value)}
            />
          </div>

          <div>
            <Label htmlFor="sv-sql">SQL</Label>
            {/* Editable: the statement a model produced is often nearly right,
                and the point of saving it is to keep the corrected version. */}
            <Textarea
              id="sv-sql"
              rows={8}
              className="font-mono text-[0.78rem]"
              value={statement}
              onChange={(event) => setStatement(event.currentTarget.value)}
            />
          </div>
        </div>

        <DialogFooter>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="primary" disabled={busy || !ready} onClick={() => void save()}>
            {busy ? t('common.saving') : t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
