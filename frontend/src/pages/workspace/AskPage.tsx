import * as React from 'react';

import { useAppearance } from '@/app/appearance';
import { useSession } from '@/app/session';
import { SectionLabel } from '@/components/primitives/page';
import { Button } from '@/components/ui/button';
import { useLocale } from '@/i18n';
import { api, csrfToken } from '@/lib/api';
import { identityHeaders } from '@/lib/identity';
import { toastError } from '@/lib/toast';

import { ConversationRail } from './ConversationRail';

// Registers <vanna-chat>. The chat stays a Lit element rather than becoming a
// React component: it owns a streaming SSE connection, an incremental message
// buffer and its own scroll anchoring, and it has browser tests against it.
// Re-implementing that to gain nothing but consistency of framework would be
// the most expensive line item in this rewrite and the easiest to get subtly
// wrong.
import '@/components/vanna-chat';

/** The parts of <vanna-chat> this page drives. */
type ChatElement = HTMLElement & {
  theme?: string;
  locale?: string;
  setCustomHeaders?: (headers: Record<string, string>) => void;
  /** The element's real entry point. See `ask()` below for why the name matters. */
  sendMessage?: (question?: string) => Promise<boolean> | void;
  /** Replay a stored thread. The element owns the conversation id, not us. */
  loadConversation?: (id: string, messages: unknown[]) => void;
  /** Mint a fresh conversation id and return it. */
  newConversation?: () => string;
  /** Whether to offer the admin-only slash commands. See below. */
  isAdmin?: boolean;
};

declare module 'react' {
  namespace JSX {
    interface IntrinsicElements {
      'vanna-chat': React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
    }
  }
}

interface Starter {
  id?: string;
  question: string;
}

/**
 * The conversation.
 *
 * Two columns rather than a strip of starter questions above the chat. As a
 * horizontal row the starters pushed the conversation down the page and wrapped
 * onto a second line the moment a question was long, while the width either side
 * of the card sat empty. A column also lets a question be a sentence rather than
 * something that has to fit in a pill.
 *
 * The starters column hides itself when a deployment has none, so an install
 * with no curated questions gets the full width for the conversation instead of
 * an empty rail.
 */
export default function AskPage() {
  const { t, locale } = useLocale();
  const { identity, role, isPlatformAdmin } = useSession();

  // `/memories` and `/delete` are gated server-side on `"admin" in
  // group_memberships`, which `_groups_for` produces from the workspace role.
  // Mirroring the same condition here means the menu offers what the server
  // will actually answer, rather than listing a command that replies "Access
  // Denied" -- the flag is a display decision, not the access control.
  const isAdmin = role === 'admin' || isPlatformAdmin;
  const chatRef = React.useRef<ChatElement | null>(null);

  const [starters, setStarters] = React.useState<Starter[]>([]);
  const [threadTick, setThreadTick] = React.useState(0);
  const { theme } = useAppearance();

  React.useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const body = await api<{ starters: Starter[] }>('/api/vanna/v2/starters');
        if (current) setStarters(body.starters ?? []);
      } catch {
        // No starters is a normal state, not a failure worth a banner.
      }
    })();
    return () => {
      current = false;
    };
  }, [identity?.tenant]);

  /**
   * The headers the chat cannot work out for itself.
   *
   * <vanna-chat> owns its own transport -- it POSTs to /chat_sse and /chat_poll
   * directly rather than through `api()` -- so nothing attaches the CSRF token
   * for it. Without that line the middleware rejects both and the widget reports
   * "Connection failed. Unable to reach server", which is true and says nothing
   * about why.
   */
  const headers = React.useCallback(() => {
    const all: Record<string, string> = { ...identityHeaders(identity) };
    const token = csrfToken();
    if (token) all['X-CSRF-Token'] = token;
    return all;
  }, [identity]);

  // Read through a ref so the listener below always sees current values without
  // being re-attached -- re-attaching is what would reintroduce the race.
  const headersRef = React.useRef(headers);
  headersRef.current = headers;

  /**
   * A callback ref, not an effect, and that is the whole fix.
   *
   * `vanna-ready` is dispatched from Lit's `firstUpdated`, which runs on a
   * microtask after the element is connected. `useEffect` also runs after
   * commit, and the two orderings are not guaranteed against each other -- so
   * the listener was sometimes attached *after* the event had already fired,
   * the headers were never set, and the first request to /chat_sse went out
   * with no CSRF token. The middleware rejected it and the widget reported
   * "Connection failed", which is true and says nothing about why.
   *
   * React invokes a callback ref synchronously during commit, immediately after
   * the node is inserted and before any microtask can run. That is early enough.
   */
  const attach = React.useCallback((element: ChatElement | null) => {
    chatRef.current = element;
    if (!element) return;
    const apply = () => element.setCustomHeaders?.(headersRef.current());
    element.addEventListener('vanna-ready', apply);
    // And once now, in case the element was already past firstUpdated -- which
    // happens when this component remounts against a cached element.
    apply();
  }, []);

  // Properties, not attributes: React would stringify an object prop onto a
  // custom element. Same reasoning as PlotlyChart.
  React.useEffect(() => {
    const element = chatRef.current;
    if (!element) return;
    element.theme = theme;
    element.locale = locale;
    element.isAdmin = isAdmin;
    // Re-applied on a workspace switch, which happens long after vanna-ready.
    element.setCustomHeaders?.(headers());
  }, [theme, locale, headers, isAdmin]);

  /**
   * Put a starter question into the conversation.
   *
   * `sendMessage` is the method `<vanna-chat>` actually exposes -- it is what
   * the vanilla app called. This used to call `element.ask()` and fall back to
   * dispatching a `vanna-ask` event, and the element has neither: no `ask`
   * method, no listener for that event. So clicking a starter did nothing at
   * all, silently, because both branches were no-ops on an element that
   * implements neither.
   */
  function ask(question: string) {
    const element = chatRef.current;
    if (!element?.sendMessage) {
      // The bundle failed to define the element; saying so beats a dead button.
      toastError(t('ask.chatUnavailable'));
      return;
    }
    void element.sendMessage(question);
    // The first message in a thread is what creates it and gives it its title,
    // so the rail is stale until the server has stored it. Nudged rather than
    // polled, and after a delay long enough for the write to land.
    window.setTimeout(() => setThreadTick((n) => n + 1), 2500);
  }

  return (
    <div className="grid h-full min-h-0 gap-4 p-4 lg:grid-cols-[260px_minmax(0,1fr)]">
      {/* Rendered unconditionally now. It used to be gated on there being
          starters, so a workspace with none had no side column at all -- and
          the conversation list has to be reachable either way. */}
      <aside className="hidden min-h-0 flex-col gap-3 lg:flex" aria-label={t('a11y.threads')}>
        <ConversationRail chat={chatRef} refreshKey={threadTick} />

        {starters.length > 0 ? (
          <div className="min-h-0 shrink-0 overflow-y-auto">
            <SectionLabel>{t('ask.tryLead')}</SectionLabel>
            <div className="flex flex-col gap-1.5">
              {starters.map((starter, index) => (
                <Button
                  key={starter.id ?? index}
                  className="h-auto justify-start whitespace-normal py-2 text-start"
                  onClick={() => ask(starter.question)}
                >
                  <span dir="auto">{starter.question}</span>
                </Button>
              ))}
            </div>
          </div>
        ) : null}
      </aside>

      <div className="min-h-0 overflow-hidden rounded-md border border-border bg-surface">
        <vanna-chat ref={attach as React.Ref<HTMLElement>} style={{ display: 'block', height: '100%' }} />
      </div>
    </div>
  );
}
