/**
 * Tests for `ComponentManager`'s update path, which streamed answers depend on.
 *
 * A streamed answer arrives as one `create` followed by many `update`s that all
 * carry the same component id, so the client patches a single bubble instead of
 * appending one per delta. That only works if the manager can apply more than
 * one update to the same component -- which it could not: the default renderer
 * `update()` swaps the DOM node, and the manager kept its original reference,
 * so the second update targeted a detached node and silently did nothing.
 *
 * These drive `ComponentManager` directly against a jsdom container rather than
 * going through `<vanna-chat>`, which would pull in Lit's shadow-DOM rendering
 * and tell us nothing extra about the logic under test.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { ComponentManager } from './rich-component-system.js';

function makeManager(): { manager: any; container: HTMLElement } {
  const container = document.createElement('div');
  document.body.appendChild(container);
  return { manager: new ComponentManager(container) as any, container };
}

function textComponent(id: string, content: string, markdown = false) {
  return { id, type: 'text', lifecycle: 'create', data: { content, markdown } };
}

function rendered(container: HTMLElement, id: string): string {
  const el = container.querySelector(`[data-component-id="${id}"]`);
  return el ? el.textContent!.trim() : '';
}

describe('ComponentManager successive updates', () => {
  let manager: any;
  let container: HTMLElement;

  beforeEach(() => {
    ({ manager, container } = makeManager());
  });

  it('renders the first update', () => {
    manager.processUpdate({ operation: 'create', component: textComponent('t1', 'Hel') });
    manager.processUpdate({
      operation: 'update',
      target_id: 't1',
      component: textComponent('t1', 'Hello'),
    });

    expect(rendered(container, 't1')).toBe('Hello');
  });

  it('renders the second and third updates too', () => {
    // The regression: updates past the first were dropped, so a streamed answer
    // froze at whatever had arrived by the first delta.
    manager.processUpdate({ operation: 'create', component: textComponent('t1', 'Hel') });
    for (const content of ['Hello', 'Hello th', 'Hello there, world']) {
      manager.processUpdate({
        operation: 'update',
        target_id: 't1',
        component: textComponent('t1', content),
      });
    }

    expect(rendered(container, 't1')).toBe('Hello there, world');
  });

  it('keeps one element in the DOM across many updates', () => {
    manager.processUpdate({ operation: 'create', component: textComponent('t1', 'a') });
    for (const content of ['ab', 'abc', 'abcd']) {
      manager.processUpdate({
        operation: 'update',
        target_id: 't1',
        component: textComponent('t1', content),
      });
    }

    expect(container.querySelectorAll('[data-component-id="t1"]').length).toBe(1);
  });

  it('preserves element identity when patching text in place', () => {
    manager.processUpdate({ operation: 'create', component: textComponent('t1', 'a') });
    const first = container.querySelector('[data-component-id="t1"]');

    manager.processUpdate({
      operation: 'update',
      target_id: 't1',
      component: textComponent('t1', 'ab'),
    });

    // Not merely equivalent -- the same node, so scroll anchoring and any
    // in-progress text selection survive the next delta.
    expect(container.querySelector('[data-component-id="t1"]')).toBe(first);
  });

  it('updates markdown text as well as plain', () => {
    manager.processUpdate({
      operation: 'create',
      component: textComponent('t1', 'The total is', true),
    });
    manager.processUpdate({
      operation: 'update',
      target_id: 't1',
      component: textComponent('t1', 'The total is **42**', true),
    });

    const el = container.querySelector('[data-component-id="t1"]')!;
    expect(el.querySelector('strong')?.textContent).toBe('42');
  });

  it('removes a component the server takes back', () => {
    // The agent streams a preamble before it knows the response is a tool call;
    // for users who should not see tool chatter, it is removed afterwards.
    manager.processUpdate({
      operation: 'create',
      component: textComponent('t1', 'Let me check that.'),
    });
    manager.processUpdate({ operation: 'remove', target_id: 't1' });

    expect(container.querySelector('[data-component-id="t1"]')).toBeNull();
  });

  it('ignores an update for a component it never created', () => {
    expect(() =>
      manager.processUpdate({
        operation: 'update',
        target_id: 'never-seen',
        component: textComponent('never-seen', 'x'),
      })
    ).not.toThrow();
  });
});
