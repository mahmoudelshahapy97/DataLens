import { MessageSquarePlus, Pencil, Trash2 } from 'lucide-react';
import * as React from 'react';

import { useConfirm } from '@/components/primitives/confirm';
import { SectionLabel } from '@/components/primitives/page';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useLocale } from '@/i18n';
import { api, del, patch } from '@/lib/api';
import { relative } from '@/lib/time';
import { toastError } from '@/lib/toast';
import { cn } from '@/lib/utils';

/**
 * Past conversations, and the way to start a new one.
 *
 * The vanilla app had this and the rewrite did not, which left the chat with no
 * memory an operator could see: every reload dropped you into a fresh thread
 * with no way back to the one you were reading, and no way to deliberately
 * start a clean one either. The dictionary already carried `thread.none` and
 * `thread.noMatches` -- the strings were ported and the feature was not.
 *
 * The chat element owns the conversation id, so this does not try to. It calls
 * `loadConversation(id, messages)` to replay a thread and `newConversation()`
 * to mint a fresh id, which is exactly what the vanilla `openThread` and
 * `newThread` did. Setting the id here instead would let the widget keep
 * posting to the previous conversation, and the two transcripts merge
 * server-side with nothing on screen to show it.
 */

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  data_source_id: string | null;
  message_count: number;
}

interface ChatHandle {
  loadConversation?: (id: string, messages: unknown[]) => void;
  newConversation?: () => string;
}

export function ConversationRail({
  chat,
  /** Bumped by the host after a message is sent, so the list re-reads. */
  refreshKey = 0,
}: {
  chat: React.RefObject<ChatHandle | null>;
  refreshKey?: number;
}) {
  const { t, locale } = useLocale();
  const confirm = useConfirm();

  const [rows, setRows] = React.useState<ConversationSummary[]>([]);
  const [filter, setFilter] = React.useState('');
  const [active, setActive] = React.useState<string | null>(null);
  const [persisted, setPersisted] = React.useState(true);

  const load = React.useCallback(async () => {
    try {
      const body = await api<{ conversations: ConversationSummary[]; persisted: boolean }>(
        '/api/vanna/v2/conversations?limit=50',
      );
      setRows(body.conversations ?? []);
      setPersisted(body.persisted !== false);
    } catch {
      // A missing history is not worth a banner over the chat itself; the rail
      // simply shows its empty state.
      setRows([]);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function open(row: ConversationSummary) {
    try {
      const body = await api<{ messages: unknown[] }>(
        `/api/vanna/v2/conversations/${encodeURIComponent(row.id)}`,
      );
      chat.current?.loadConversation?.(row.id, body.messages ?? []);
      setActive(row.id);
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  function start() {
    const id = chat.current?.newConversation?.();
    setActive(id ?? null);
    // Not reloaded: an empty thread has nothing to list until it has a message.
  }

  async function rename(row: ConversationSummary) {
    const title = window.prompt(t('thread.rename'), row.title);
    if (title === null) return;
    const trimmed = title.trim();
    if (!trimmed) return;
    try {
      await patch(`/api/vanna/v2/conversations/${encodeURIComponent(row.id)}`, {
        title: trimmed,
      });
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function remove(row: ConversationSummary) {
    const ok = await confirm.ask({
      title: t('thread.deleteTitle'),
      body: t('thread.deleteBody', { title: row.title }),
      confirmLabel: t('common.delete'),
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/vanna/v2/conversations/${encodeURIComponent(row.id)}`);
      if (active === row.id) start();
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  const needle = filter.trim().toLowerCase();
  const visible = needle
    ? rows.filter((row) => row.title.toLowerCase().includes(needle))
    : rows;

  return (
    <div className="flex min-h-0 flex-col gap-2">
      {confirm.dialog}

      <Button variant="primary" onClick={start}>
        <MessageSquarePlus />
        {t('thread.new')}
      </Button>

      {rows.length > 6 ? (
        <Input
          type="search"
          placeholder={t('thread.search')}
          value={filter}
          onChange={(event) => setFilter(event.currentTarget.value)}
        />
      ) : null}

      <SectionLabel>{t('a11y.threads')}</SectionLabel>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {!persisted ? (
          <p className="px-2 text-[0.75rem] text-muted-foreground">{t('thread.notPersisted')}</p>
        ) : visible.length === 0 ? (
          <p className="px-2 text-[0.75rem] text-muted-foreground">
            {needle ? t('thread.noMatches') : t('thread.none')}
          </p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {visible.map((row) => (
              <li key={row.id} className="group relative">
                <button
                  type="button"
                  onClick={() => void open(row)}
                  className={cn(
                    'w-full rounded-md px-2 py-1.5 pe-14 text-start hover:bg-rail-hover',
                    active === row.id ? 'bg-primary-soft text-primary-ink' : '',
                  )}
                >
                  <span className="block truncate text-[0.8125rem]" dir="auto">
                    {row.title || t('thread.untitled')}
                  </span>
                  <span className="block text-[0.7rem] text-muted-foreground">
                    {t('thread.messages', { n: row.message_count })} ·{' '}
                    {relative(row.updated_at || row.created_at, t, locale)}
                  </span>
                </button>

                {/* Revealed on hover/focus rather than always shown: at rest the
                    rail is a list of titles, not a list of controls. */}
                <span className="absolute end-1 top-1 hidden gap-0.5 group-hover:flex group-focus-within:flex">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={t('thread.rename')}
                    onClick={() => void rename(row)}
                  >
                    <Pencil className="size-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={t('common.delete')}
                    onClick={() => void remove(row)}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
