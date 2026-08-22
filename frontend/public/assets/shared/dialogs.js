/**
 * Accessible replacements for `window.confirm` and `window.prompt`.
 *
 * Both pages already own a focus-trapped `#overlay`/`#sheet` and use it for every
 * real dialog; the destructive actions were the exception, going through native
 * dialogs instead. Those cannot be translated, styled, mirrored for RTL, or made
 * to name the thing being destroyed -- and "Delete this example permanently?" in
 * hard-coded English sits oddly in an app that is otherwise fully translated.
 *
 * Separate module rather than more of `core.js`: these need the page's `t()`, and
 * `core.js` deliberately takes translation as an argument everywhere (see
 * `relative`/`until`) so it stays free of a dependency on either page's
 * dictionary. Passing `t` in keeps that property.
 *
 * Both return a promise, so a caller reads top to bottom:
 *
 *     if (!(await confirmSheet(t, {title: …, body: …}))) return;
 */

import { announce, esc, trapFocus } from './core.js';

/**
 * Button classes for both pages at once.
 *
 * The workspace styles `button.btn` and the console styles `button.act`; each
 * stylesheet ignores the other's class, so carrying both is what lets one dialog
 * look native on either page. `.actions` is defined identically in both.
 */
const OK = 'btn act primary';
const NO = 'btn act';
const DANGER = 'btn act danger';

/** The dialog currently open, so a second call cannot orphan the first. */
let release = null;

function close(overlay) {
  overlay.classList.remove('on');
  overlay.setAttribute('aria-hidden', 'true');
  if (release) {
    release();
    release = null;
  }
}

function open(html, label) {
  const overlay = document.getElementById('overlay');
  const sheet = document.getElementById('sheet');
  sheet.innerHTML = html;
  sheet.setAttribute('aria-label', label);
  overlay.classList.add('on');
  overlay.removeAttribute('aria-hidden');
  return { overlay, sheet };
}

/**
 * Ask for confirmation. Resolves true only if the user confirms.
 *
 * `danger` styles the confirm button as destructive; use it whenever the action
 * cannot be undone. `body` should name what is about to happen to *what* --
 * "Delete Sales report?" tells a screen-reader user far more than "Are you sure?".
 */
export function confirmSheet(t, { title, body = '', confirmLabel = '', danger = false } = {}) {
  return new Promise((resolve) => {
    const { overlay, sheet } = open(
      `<h3 style="margin-top:0">${esc(title)}</h3>
       ${body ? `<p>${esc(body)}</p>` : ''}
       <div class="actions">
         <button class="${NO}" id="dlg-no">${esc(t('common.cancel'))}</button>
         <button class="${danger ? DANGER : OK}" id="dlg-yes">
           ${esc(confirmLabel || t('common.confirm'))}
         </button>
       </div>`,
      title
    );

    const finish = (answer) => {
      close(overlay);
      resolve(answer);
    };

    sheet.querySelector('#dlg-yes').onclick = () => finish(true);
    sheet.querySelector('#dlg-no').onclick = () => finish(false);
    // Escape and the backdrop both mean "no". Anything else would make dismissing
    // a destructive dialog ambiguous, which is the one place it must not be.
    overlay.onclick = (event) => { if (event.target === overlay) finish(false); };
    release = trapFocus(sheet, { onEscape: () => finish(false) });
    announce(title, 'assertive');
  });
}

/**
 * Ask for a line of text. Resolves the trimmed string, or null if cancelled.
 *
 * An empty submission resolves null rather than `''`: every caller here treats a
 * blank name as "no change", and returning the empty string made each one repeat
 * that check.
 */
export function promptSheet(
  t,
  { title, label = '', value = '', confirmLabel = '', placeholder = '' } = {}
) {
  return new Promise((resolve) => {
    const { overlay, sheet } = open(
      `<h3 style="margin-top:0">${esc(title)}</h3>
       <label for="dlg-input">${esc(label || title)}</label>
       <input id="dlg-input" value="${esc(value)}" placeholder="${esc(placeholder)}" />
       <div class="actions">
         <button class="${NO}" id="dlg-cancel">${esc(t('common.cancel'))}</button>
         <button class="${OK}" id="dlg-ok">${esc(confirmLabel || t('common.save'))}</button>
       </div>`,
      title
    );

    const input = sheet.querySelector('#dlg-input');
    const finish = (answer) => {
      close(overlay);
      resolve(answer);
    };
    const submit = () => {
      const next = (input.value || '').trim();
      finish(next || null);
    };

    sheet.querySelector('#dlg-ok').onclick = submit;
    sheet.querySelector('#dlg-cancel').onclick = () => finish(null);
    // Enter submits. A one-field dialog that ignores Enter feels broken.
    input.onkeydown = (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit();
      }
    };
    overlay.onclick = (event) => { if (event.target === overlay) finish(null); };
    release = trapFocus(sheet, { onEscape: () => finish(null) });
    input.select();
  });
}
