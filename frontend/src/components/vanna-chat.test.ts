/**
 * Unit tests for the pure/isolatable logic in `VannaChat` -- the slash-command
 * menu (matching, admin-hiding, argument handling), the CSV export escaping
 * (including the CSV-injection guard), and abort-detection.
 *
 * These instantiate `VannaChat` directly via `new VannaChat()` without
 * appending it to the document, so `connectedCallback` never runs and Lit
 * never attempts a real shadow-DOM render. That sidesteps jsdom's shakier
 * support for adopted stylesheets entirely, and keeps these tests to what
 * they are actually about: the plain TypeScript logic on the class, not
 * rendering. Methods and state fields the component marks `private`/`@state`
 * are still reachable at runtime (TypeScript's `private` is compile-time
 * only), so this reaches them via `as any`.
 */

import { describe, expect, it } from 'vitest';
import { VannaChat } from './vanna-chat.js';

function makeChat(): any {
  return new VannaChat() as any;
}

describe('visibleCommands', () => {
  it('hides admin-only commands from a non-admin by default', () => {
    const chat = makeChat();
    chat.isAdmin = false;
    chat.currentMessage = '';

    const names = chat.visibleCommands().map((c: any) => c.name);

    expect(names).toEqual(['/help', '/status', '/setup', '/memorise']);
    expect(names).not.toContain('/memories');
    expect(names).not.toContain('/delete');
  });

  it('shows every command to an admin', () => {
    const chat = makeChat();
    chat.isAdmin = true;
    chat.currentMessage = '';

    const names = chat.visibleCommands().map((c: any) => c.name);

    expect(names).toEqual([
      '/help',
      '/status',
      '/setup',
      '/memorise',
      '/memories',
      '/delete',
    ]);
  });

  it('narrows to what has been typed so far, case-insensitively', () => {
    const chat = makeChat();
    chat.isAdmin = true;
    chat.currentMessage = '/MEM';

    const names = chat.visibleCommands().map((c: any) => c.name);

    expect(names).toEqual(['/memorise', '/memories']);
  });

  it('a non-admin typing an admin command prefix sees nothing for it', () => {
    const chat = makeChat();
    chat.isAdmin = false;
    chat.currentMessage = '/delete';

    expect(chat.visibleCommands()).toEqual([]);
  });

  it('an unrecognised prefix matches nothing', () => {
    const chat = makeChat();
    chat.currentMessage = '/nope';

    expect(chat.visibleCommands()).toEqual([]);
  });
});

describe('handleInput command-menu detection', () => {
  it('opens the menu for a bare slash prefix', () => {
    const chat = makeChat();
    chat.handleInput({ target: { value: '/mem' } } as unknown as Event);

    expect(chat.commandOpen).toBe(true);
    expect(chat.currentMessage).toBe('/mem');
  });

  it('does not open for a slash embedded mid-question', () => {
    const chat = makeChat();
    chat.handleInput({ target: { value: 'revenue w/ tax' } } as unknown as Event);

    expect(chat.commandOpen).toBe(false);
  });

  it('does not open once a space follows the command', () => {
    const chat = makeChat();
    chat.handleInput({ target: { value: '/delete abc' } } as unknown as Event);

    expect(chat.commandOpen).toBe(false);
  });

  it('closes for an empty input', () => {
    const chat = makeChat();
    chat.commandOpen = true;
    chat.handleInput({ target: { value: '' } } as unknown as Event);

    expect(chat.commandOpen).toBe(false);
  });

  it('resets the highlighted index on every keystroke', () => {
    const chat = makeChat();
    chat.commandIndex = 2;
    chat.handleInput({ target: { value: '/s' } } as unknown as Event);

    expect(chat.commandIndex).toBe(0);
  });
});

describe('chooseCommand', () => {
  it('a command with a required argument fills the prompt but does not send', () => {
    const chat = makeChat();
    let sent: string | undefined;
    chat.sendMessage = (text?: string) => {
      sent = text;
      return Promise.resolve(true);
    };
    chat.commandOpen = true;

    chat.chooseCommand({ name: '/delete', arg: '<id>' });

    expect(chat.commandOpen).toBe(false);
    expect(chat.currentMessage).toBe('/delete ');
    expect(sent).toBeUndefined();
  });

  it('a command with no argument is sent immediately', () => {
    const chat = makeChat();
    let sent: string | undefined;
    chat.sendMessage = (text?: string) => {
      sent = text;
      return Promise.resolve(true);
    };
    chat.commandOpen = true;

    chat.chooseCommand({ name: '/help' });

    expect(chat.commandOpen).toBe(false);
    expect(sent).toBe('/help');
  });
});

describe('csvEscape', () => {
  it('wraps a plain value in quotes', () => {
    const chat = makeChat();
    expect(chat.csvEscape('hello')).toBe('"hello"');
  });

  it('doubles embedded quotes', () => {
    const chat = makeChat();
    expect(chat.csvEscape('say "hi"')).toBe('"say ""hi"""');
  });

  it('collapses internal whitespace and trims', () => {
    const chat = makeChat();
    expect(chat.csvEscape('  a   b\n c  ')).toBe('"a b c"');
  });

  it.each(['=HYPERLINK("http://evil")', '+1+1', '-1', '@SUM(1)'])(
    'guards a formula-triggering leading character: %s',
    (value) => {
      const chat = makeChat();
      const escaped = chat.csvEscape(value);
      // Quoted content must start with an apostrophe so spreadsheet software
      // reads it as text, never as a formula.
      expect(escaped.startsWith(`"'${value[0]}`)).toBe(true);
    },
  );

  it('leaves an ordinary value with no leading guard character alone', () => {
    const chat = makeChat();
    expect(chat.csvEscape('123')).toBe('"123"');
  });
});

describe('wasAborted', () => {
  it('is true when the abort controller itself was aborted', () => {
    const chat = makeChat();
    chat.abortController = new AbortController();
    chat.abortController.abort();

    expect(chat.wasAborted(new Error('network error'))).toBe(true);
  });

  it('is true for a DOMException named AbortError even without a controller', () => {
    const chat = makeChat();
    chat.abortController = null;

    expect(chat.wasAborted(new DOMException('aborted', 'AbortError'))).toBe(true);
  });

  it('is false for an ordinary error with no abort in progress', () => {
    const chat = makeChat();
    chat.abortController = new AbortController();

    expect(chat.wasAborted(new Error('boom'))).toBe(false);
  });
});
