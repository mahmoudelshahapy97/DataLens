import { Brain, KeyRound, Plus, Trash2 } from 'lucide-react';
import * as React from 'react';

import { useSession } from '@/app/session';
import { useConfirm } from '@/components/primitives/confirm';
import { DataTable, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader } from '@/components/primitives/page';
import { StatTile } from '@/components/primitives/stat-tile';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useLocale } from '@/i18n';
import { api, del, post } from '@/lib/api';
import { relative, until } from '@/lib/time';
import { toast, toastError, toastSuccess } from '@/lib/toast';

/**
 * This account: its plan, its password, its tokens and its sessions.
 *
 * The tokens section has the one genuinely dangerous interaction on the page.
 * A created token is shown **once** -- the server stores only its hash, so there
 * is no second chance to read it and no "show again" to build. The dialog says
 * so before the value appears rather than after it has scrolled away.
 */

interface Memory {
  memory_id: string;
  content: string;
  created_at: string;
}

interface Usage {
  enabled: boolean;
  used: number;
  limit: number;
  window: string;
  scope: string;
  plan: string;
  plan_label: string;
  max_rows: number;
  expired: boolean;
  limit_source: string;
}

interface Token {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
}

interface Session {
  id: string;
  created_at: string;
  last_seen_at: string | null;
  user_agent: string;
  current: boolean;
}

export default function AccountPage() {
  const { t, locale } = useLocale();
  const { me, signOut } = useSession();
  const confirm = useConfirm();

  const [usage, setUsage] = React.useState<Usage | null>(null);
  const [tokens, setTokens] = React.useState<Token[]>([]);
  const [sessions, setSessions] = React.useState<Session[]>([]);
  const [issued, setIssued] = React.useState<string | null>(null);

  const [current, setCurrent] = React.useState('');
  const [next, setNext] = React.useState('');
  const [again, setAgain] = React.useState('');
  const [tokenName, setTokenName] = React.useState('');
  const [memories, setMemories] = React.useState<Memory[]>([]);
  const [remembering, setRemembering] = React.useState('');

  const load = React.useCallback(async () => {
    const results = await Promise.allSettled([
      api<Usage>('/api/vanna/v2/usage'),
      api<{ tokens: Token[] }>('/api/vanna/v2/auth/tokens'),
      api<{ sessions: Session[] }>('/api/vanna/v2/auth/sessions'),
      api<{ memories: Memory[] }>('/api/vanna/v2/memories'),
    ]);
    // allSettled, not all: this page is four independent panels and one that
    // 404s -- tokens are unavailable without a control plane -- must not blank
    // the other three.
    if (results[0].status === 'fulfilled') setUsage(results[0].value);
    if (results[1].status === 'fulfilled') setTokens(results[1].value.tokens ?? []);
    if (results[2].status === 'fulfilled') setSessions(results[2].value.sessions ?? []);
    if (results[3].status === 'fulfilled') setMemories(results[3].value.memories ?? []);
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function remember(event: React.FormEvent) {
    event.preventDefault();
    const content = remembering.trim();
    if (!content) return;
    try {
      await post('/api/vanna/v2/memories', { content });
      setRemembering('');
      toastSuccess(t('recall.saved'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function forget(memory: Memory) {
    // Confirmed, like every other destructive action here: there is no undo,
    // and the agent may have been relying on this to answer correctly.
    const ok = await confirm.ask({
      title: t('recall.forgetTitle'),
      body: memory.content,
      confirmLabel: t('recall.forget'),
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/vanna/v2/memories/${encodeURIComponent(memory.memory_id)}`);
      toast(t('recall.forgotten'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function changePassword(event: React.FormEvent) {
    event.preventDefault();
    if (next !== again) {
      toastError(t('account.passwordMismatch'));
      return;
    }
    try {
      await post('/api/vanna/v2/auth/password', { current_password: current, new_password: next });
      setCurrent('');
      setNext('');
      setAgain('');
      toast(t('account.passwordChanged'));
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function createToken(event: React.FormEvent) {
    event.preventDefault();
    if (!tokenName.trim()) {
      toastError(t('account.tokenNeedsName'));
      return;
    }
    try {
      const body = await post<{ token: string }>('/api/vanna/v2/auth/tokens', {
        name: tokenName.trim(),
      });
      setIssued(body.token);
      setTokenName('');
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function revokeToken(token: Token) {
    const ok = await confirm.ask({
      title: t('account.revokeTitle'),
      body: t('account.revokeConfirm', { name: token.name || t('account.unnamedToken') }),
      confirmLabel: t('account.revoke'),
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/vanna/v2/auth/tokens/${token.id}`);
      setTokens((list) => list.filter((item) => item.id !== token.id));
      toast(t('common.revoked'));
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function endSession(session: Session) {
    if (session.current) {
      const ok = await confirm.ask({
        title: t('account.signOutThisOne', { device: session.user_agent || '' }),
        body: t('account.signOutOneConfirm'),
        danger: true,
      });
      if (ok) await signOut();
      return;
    }
    try {
      await del(`/api/vanna/v2/auth/sessions/${session.id}`);
      setSessions((list) => list.filter((item) => item.id !== session.id));
      toast(t('account.endedOne'));
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  return (
    <PageBody>
      {confirm.dialog}
      <PageHeader title={t('account.title')} description={me?.user.email} />

      {usage ? (
        <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label={t('plan.plan')} value={usage.plan_label} tone="primary" />
          <StatTile
            label={t('plan.used')}
            value={`${usage.used} / ${usage.limit}`}
            hint={usage.window}
          />
          <StatTile label={t('plan.maxRows')} value={usage.max_rows.toLocaleString()} />
          <StatTile
            label={t('plan.scope')}
            value={usage.scope}
            hint={usage.limit_source}
            tone={usage.expired ? 'bad' : 'neutral'}
          />
        </div>
      ) : null}

      {/* min-w-0: these cards hold tables, and a grid item defaults to
          `min-width: auto` -- it will not shrink below its content, so a wide
          row (a user agent string, a long token name) pushes past the card edge
          instead of scrolling inside it. */}
      <div className="grid gap-3 lg:grid-cols-2">
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{t('account.changePassword')}</CardTitle>
            <CardDescription>{t('account.passwordHelp')}</CardDescription>
          </CardHeader>
          <CardContent>
            <form className="flex flex-col gap-2.5" onSubmit={changePassword}>
              <Field
                id="acct-current"
                label={t('account.currentPassword')}
                value={current}
                onChange={setCurrent}
              />
              <Field id="acct-new" label={t('account.newPassword')} value={next} onChange={setNext} />
              <Field
                id="acct-again"
                label={t('account.repeatPassword')}
                value={again}
                onChange={setAgain}
              />
              <Button type="submit" variant="primary" className="self-start" disabled={!next}>
                {t('account.changePassword')}
              </Button>
            </form>
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{t('account.tokens')}</CardTitle>
            <CardDescription>{t('account.tokensHelp')}</CardDescription>
          </CardHeader>
          <CardContent>
            <form className="mb-3 flex items-end gap-2" onSubmit={createToken}>
              <div className="flex flex-1 flex-col gap-1.5">
                <Label htmlFor="acct-token-name">{t('account.tokenName')}</Label>
                <Input
                  id="acct-token-name"
                  value={tokenName}
                  onChange={(event) => setTokenName(event.target.value)}
                />
              </div>
              <Button type="submit">
                <Plus />
                {t('account.createToken')}
              </Button>
            </form>

            {issued ? (
              <div className="mb-3 rounded-md border border-warn/40 bg-warn/5 p-3">
                <p className="mb-1.5 text-[0.8125rem] font-medium">{t('account.tokenOnce')}</p>
                <code className="block break-all rounded bg-surface-2 p-2 font-mono text-[0.78rem]" dir="ltr">
                  {issued}
                </code>
                <Button className="mt-2" size="sm" onClick={() => setIssued(null)}>
                  {t('common.close')}
                </Button>
              </div>
            ) : null}

            {tokens.length === 0 ? (
              <p className="text-[0.8125rem] text-muted-foreground">{t('account.noTokens')}</p>
            ) : (
              <DataTable>
                <thead>
                  <Tr>
                    <Th>{t('account.tokenName')}</Th>
                    <Th>{t('account.lastUsed')}</Th>
                    <Th className="w-12" />
                  </Tr>
                </thead>
                <Tbody>
                  {tokens.map((token) => (
                    <Tr key={token.id}>
                      <Td dir="auto">
                        <KeyRound className="me-1 inline size-3.5 text-muted-foreground" />
                        {token.name || t('account.unnamedToken')}
                        {token.expires_at ? (
                          <span className="block text-[0.72rem] text-muted-foreground">
                            {t('account.expires')} {until(token.expires_at, t, locale)}
                          </span>
                        ) : null}
                      </Td>
                      <Td className="text-muted-foreground">
                        {token.last_used_at
                          ? relative(token.last_used_at, t, locale)
                          : t('account.neverUsed')}
                      </Td>
                      <Td>
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={t('account.revoke')}
                          onClick={() => void revokeToken(token)}
                        >
                          <Trash2 />
                        </Button>
                      </Td>
                    </Tr>
                  ))}
                </Tbody>
              </DataTable>
            )}
          </CardContent>
        </Card>

        <Card className="min-w-0 lg:col-span-2">
          <CardHeader>
            <CardTitle>{t('account.sessions')}</CardTitle>
            <CardDescription>{t('account.sessionsHelp')}</CardDescription>
          </CardHeader>
          <CardContent>
            {sessions.length === 0 ? (
              <p className="text-[0.8125rem] text-muted-foreground">{t('account.noSessions')}</p>
            ) : (
              <DataTable>
                <thead>
                  <Tr>
                    <Th>{t('account.signedIn')}</Th>
                    <Th>{t('account.lastUsed')}</Th>
                    <Th>{t('account.unknownAddress')}</Th>
                    <Th className="w-28" />
                  </Tr>
                </thead>
                <Tbody>
                  {sessions.map((session) => (
                    <Tr key={session.id}>
                      <Td className="text-muted-foreground">
                        {relative(session.created_at, t, locale)}
                        {session.current ? (
                          <Badge tone="ok" className="ms-2">
                            {t('account.thisBrowser')}
                          </Badge>
                        ) : null}
                      </Td>
                      <Td className="text-muted-foreground">
                        {session.last_seen_at ? relative(session.last_seen_at, t, locale) : '—'}
                      </Td>
                      <Td dir="ltr" className="max-w-xs truncate text-[0.75rem] text-muted-foreground">
                        {session.user_agent}
                      </Td>
                      <Td>
                        <Button size="sm" variant="ghost" onClick={() => void endSession(session)}>
                          {session.current
                            ? t('account.signOutHere')
                            : t('account.revoke')}
                        </Button>
                      </Td>
                    </Tr>
                  ))}
                </Tbody>
              </DataTable>
            )}
          </CardContent>
        </Card>

        {/* What the assistant knows about you.

            The store behind this is the same one the agent writes to when a
            conversation produces a fact worth keeping, and the same one
            `/memories` lists in the chat. It is surfaced here because a store
            somebody cannot read is one they cannot correct, and because "what
            does it know about me" is an account question, not a chat question. */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Brain className="size-4" />
              {t('recall.title')}
            </CardTitle>
            <CardDescription>{t('recall.blurb')}</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <form className="flex flex-col gap-2" onSubmit={(event) => void remember(event)}>
              <Label htmlFor="acct-memory">{t('recall.add')}</Label>
              <Textarea
                id="acct-memory"
                rows={2}
                dir="auto"
                maxLength={2000}
                placeholder={t('recall.hint')}
                value={remembering}
                onChange={(event) => setRemembering(event.target.value)}
              />
              <div>
                <Button type="submit" variant="primary" disabled={!remembering.trim()}>
                  <Plus className="size-4" />
                  {t('recall.remember')}
                </Button>
              </div>
            </form>

            {memories.length === 0 ? (
              <p className="text-[0.8125rem] text-muted-foreground">{t('recall.none')}</p>
            ) : (
              <DataTable>
                <Tbody>
                  {memories.map((memory) => (
                    <Tr key={memory.memory_id}>
                      <Td dir="auto">{memory.content}</Td>
                      <Td className="whitespace-nowrap text-muted-foreground">
                        {relative(memory.created_at, t, locale)}
                      </Td>
                      <Td>
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={t('recall.forget')}
                          onClick={() => void forget(memory)}
                        >
                          <Trash2 className="size-4" />
                        </Button>
                      </Td>
                    </Tr>
                  ))}
                </Tbody>
              </DataTable>
            )}
          </CardContent>
        </Card>
      </div>
    </PageBody>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {/* dir=ltr: a password is not Arabic text, and mirroring it makes what you
          typed unreadable. */}
      <Input
        id={id}
        type="password"
        dir="ltr"
        autoComplete={id === 'acct-current' ? 'current-password' : 'new-password'}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}
