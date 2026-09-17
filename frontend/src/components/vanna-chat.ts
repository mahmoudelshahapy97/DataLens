import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { vannaDesignTokens } from '../styles/vanna-design-tokens.js';
import { VannaApiClient, ChatStreamChunk } from '../services/api-client.js';
import { ComponentManager, RichComponent } from './rich-component-system.js';
import { isRtl, translate } from '../locales/index.js';
// Defines <vanna-message>, which both message renderers in
// rich-component-system.ts create. Without this import the element is never
// upgraded: it lands in the DOM as an unknown tag with no shadow root, renders
// nothing and collapses to zero height -- so the user's own question silently
// vanished from the transcript and only the assistant's reply (which takes a
// different, plain-div path) was visible.
import './vanna-message.js';
import './vanna-status-bar.js';
import './vanna-progress-tracker.js';
import './rich-card.js';
import './rich-task-list.js';
import './rich-progress-bar.js';
import './plotly-chart.js';

@customElement('vanna-chat')
export class VannaChat extends LitElement {
  static styles = [
    vannaDesignTokens,
    css`
      *, *::before, *::after {
        box-sizing: border-box;
      }

      :host {
        display: block;
        font-family: var(--vanna-font-family-default);
        --chat-primary: var(--primary, var(--vanna-accent-primary-default));
        --chat-primary-stronger: var(--vanna-accent-primary-stronger);
        --chat-primary-foreground: rgb(255, 255, 255);
        --chat-accent-soft: var(--vanna-accent-primary-subtle);
        --chat-outline: var(--vanna-outline-default);
        --chat-surface: var(--vanna-background-root);
        --chat-muted: var(--vanna-background-default);
        --chat-muted-stronger: var(--vanna-background-higher);
        max-width: 1024px;
        margin: 0 auto;
        background: var(--vanna-background-root);
        border: 1px solid var(--vanna-outline-dimmer);
        border-radius: var(--vanna-border-radius-2xl);
        box-shadow: var(--vanna-shadow-xl);
        overflow: hidden;
        transition: box-shadow var(--vanna-duration-300) ease, transform var(--vanna-duration-300) ease;
        position: relative;
      }

      :host(:hover) {
        box-shadow: var(--vanna-shadow-2xl);
        transform: translateY(-2px);
      }

      :host([theme="dark"]) {
        --chat-primary: var(--primary, var(--vanna-accent-primary-default));
        --chat-primary-stronger: var(--vanna-accent-primary-stronger);
        --chat-primary-foreground: rgb(255, 255, 255);
        --chat-accent-soft: var(--vanna-accent-primary-subtle);
        --chat-outline: var(--vanna-outline-default);
        --chat-surface: var(--vanna-background-higher);
        --chat-muted: var(--vanna-background-default);
        --chat-muted-stronger: var(--vanna-background-highest);
        background: var(--vanna-background-higher);
        border-color: var(--vanna-outline-default);
      }

      :host(.maximized) {
        position: fixed;
        top: var(--vanna-space-6);
        left: var(--vanna-space-6);
        right: var(--vanna-space-6);
        bottom: var(--vanna-space-6);
        max-width: none;
        width: auto;
        margin: 0;
        z-index: var(--vanna-z-modal);
        border-radius: var(--vanna-border-radius-xl);
        transform: none;
        box-shadow: var(--vanna-shadow-2xl);
      }

      :host(.maximized):hover {
        transform: none;
      }

      :host(.minimized) {
        position: fixed !important;
        bottom: var(--vanna-space-6) !important;
        right: var(--vanna-space-6) !important;
        width: 64px !important;
        height: 64px !important;
        max-width: none !important;
        margin: 0 !important;
        z-index: var(--vanna-z-modal) !important;
        border-radius: var(--vanna-border-radius-full) !important;
        cursor: pointer !important;
        background: linear-gradient(135deg, var(--chat-primary-stronger), var(--chat-primary)) !important;
        border: 2px solid rgba(255, 255, 255, 0.9) !important;
        box-shadow: var(--vanna-shadow-xl) !important;
        overflow: hidden !important;
      }

      :host(.minimized):hover {
        transform: scale(1.05);
        box-shadow: var(--vanna-shadow-2xl) !important;
      }

      :host(.minimized) .chat-layout {
        display: none;
      }

      .minimized-icon {
        display: none;
      }

      :host(.minimized) .minimized-icon {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 100%;
        height: 100%;
        color: var(--chat-primary-foreground);
        font-size: 24px;
        transition: transform var(--vanna-duration-200) ease;
      }

      :host(.minimized) .minimized-icon:hover {
        transform: scale(1.1);
      }

      :host(.minimized) .minimized-icon svg {
        filter: drop-shadow(0 2px 4px rgba(0, 0, 0, 0.3));
      }

      .chat-layout {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 300px;
        height: 600px;
        max-height: 80vh;
        background: var(--chat-muted);
      }

      :host(.maximized) .chat-layout {
        height: calc(100vh - 48px);
        max-height: calc(100vh - 48px);
      }

      .chat-layout.compact {
        grid-template-columns: 1fr;
      }

      .chat-main {
        display: flex;
        flex-direction: column;
        border-right: 1px solid var(--chat-outline);
        background: var(--chat-surface);
        min-height: 0;
      }

      .chat-layout.compact .chat-main {
        border-right: none;
      }

      .chat-header {
        padding: var(--vanna-space-6) var(--vanna-space-7);
        background: linear-gradient(135deg, var(--chat-primary) 0%, var(--chat-primary-stronger) 100%);
        border-bottom: 1px solid rgba(255, 255, 255, 0.2);
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-4);
        color: var(--chat-primary-foreground);
        position: relative;
        overflow: hidden;
      }

      .chat-header::before {
        content: '';
        position: absolute;
        top: -50%;
        right: -50%;
        width: 100%;
        height: 200%;
        background: radial-gradient(circle, rgba(255, 255, 255, 0.15) 0%, transparent 70%);
        opacity: 0.6;
        pointer-events: none;
      }

      :host([theme="dark"]) .chat-header {
        border-bottom-color: rgba(255, 255, 255, 0.1);
      }

      .header-top {
        position: relative;
        z-index: 1;
        display: flex;
        align-items: center;
        gap: var(--vanna-space-4);
        width: 100%;
      }

      .header-left {
        display: flex;
        align-items: center;
        gap: var(--vanna-space-4);
        min-width: 0;
        flex: 1;
      }

      .header-top-actions {
        display: inline-flex;
        align-items: center;
        gap: var(--vanna-space-2);
        margin-left: auto;
      }

      .chat-avatar {
        width: 44px;
        height: 44px;
        border-radius: var(--vanna-border-radius-lg);
        background: rgba(255, 255, 255, 0.2);
        backdrop-filter: blur(10px);
        display: grid;
        place-items: center;
        font-weight: 600;
        font-size: 16px;
        letter-spacing: 0.02em;
        color: var(--chat-primary-foreground);
        border: 1px solid rgba(255, 255, 255, 0.3);
      }

      .header-text {
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-1);
        min-width: 0;
      }

      .chat-title {
        margin: 0;
        font-size: 18px;
        font-weight: 600;
        letter-spacing: -0.01em;
        color: var(--chat-primary-foreground);
      }

      .chat-subtitle {
        font-size: 13px;
        letter-spacing: 0.01em;
        opacity: 0.9;
        font-weight: 400;
      }

      :host([theme="dark"]) .chat-subtitle {
        opacity: 0.78;
      }

      .window-controls {
        display: inline-flex;
        gap: var(--vanna-space-2);
      }

      .window-control-btn {
        width: 32px;
        height: 32px;
        border-radius: var(--vanna-border-radius-lg);
        border: 1px solid rgba(255, 255, 255, 0.15);
        background: rgba(255, 255, 255, 0.1);
        color: var(--chat-primary-foreground);
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        transition: all var(--vanna-duration-200) ease;
        backdrop-filter: blur(8px);
        position: relative;
        overflow: hidden;
      }

      .window-control-btn::before {
        content: '';
        position: absolute;
        inset: 0;
        background: linear-gradient(135deg, rgba(255, 255, 255, 0.2), transparent);
        opacity: 0;
        transition: opacity var(--vanna-duration-200) ease;
      }

      .window-control-btn:hover {
        transform: translateY(-1px) scale(1.05);
        background: rgba(255, 255, 255, 0.2);
        box-shadow: 
          0 8px 25px -8px rgba(0, 0, 0, 0.3),
          0 0 0 1px rgba(255, 255, 255, 0.2);
        border-color: rgba(255, 255, 255, 0.3);
      }

      .window-control-btn:hover::before {
        opacity: 1;
      }

      .window-control-btn:active {
        transform: translateY(0) scale(0.95);
      }

      .window-control-btn.minimize:hover {
        background: rgba(255, 193, 7, 0.2);
        color: #ffc107;
        box-shadow: 
          0 8px 25px -8px rgba(255, 193, 7, 0.4),
          0 0 0 1px rgba(255, 193, 7, 0.3);
      }

      .window-control-btn.maximize:hover,
      .window-control-btn.restore:hover {
        background: rgba(40, 167, 69, 0.2);
        color: #28a745;
        box-shadow: 
          0 8px 25px -8px rgba(40, 167, 69, 0.4),
          0 0 0 1px rgba(40, 167, 69, 0.3);
      }

      .window-control-btn svg {
        width: 16px;
        height: 16px;
        transition: transform var(--vanna-duration-150) ease;
      }

      .window-control-btn:hover svg {
        transform: scale(1.1);
      }

      :host([theme="dark"]) .window-control-btn {
        border-color: rgba(255, 255, 255, 0.1);
        background: rgba(255, 255, 255, 0.05);
      }

      :host([theme="dark"]) .window-control-btn:hover {
        background: rgba(255, 255, 255, 0.15);
        border-color: rgba(255, 255, 255, 0.25);
      }

      .chat-messages {
        flex: 1;
        overflow-y: auto;
        overflow-x: hidden;
        padding: var(--vanna-space-6) var(--vanna-space-6) var(--vanna-space-5);
        background: linear-gradient(180deg, var(--chat-muted) 0%, var(--chat-surface) 70%);
        scroll-behavior: smooth;
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-4);
        min-height: 0;
        max-height: 100%;
        position: relative;
      }

      .chat-messages::-webkit-scrollbar {
        width: 6px;
      }

      .chat-messages::-webkit-scrollbar-track {
        background: transparent;
      }

      .chat-messages::-webkit-scrollbar-thumb {
        background: var(--vanna-outline-default);
        border-radius: var(--vanna-border-radius-full);
        border: 1px solid var(--vanna-background-root);
      }

      .chat-messages::-webkit-scrollbar-thumb:hover {
        background: var(--vanna-outline-hover);
      }

      :host([theme="dark"]) .chat-messages {
        background: radial-gradient(circle at top, rgba(99, 102, 241, 0.12), transparent 55%), var(--chat-surface);
      }

      :host([theme="dark"]) .chat-messages::-webkit-scrollbar-thumb {
        background: var(--vanna-outline-default);
        border-color: var(--vanna-background-higher);
      }

      /* Scroll indicator when there's content above */
      .chat-messages::before {
        content: '';
        position: sticky;
        top: 0;
        display: block;
        height: 1px;
        background: linear-gradient(90deg, transparent, var(--vanna-accent-primary-default), transparent);
        opacity: 0;
        transition: opacity var(--vanna-duration-300) ease;
        z-index: 10;
        margin: 0 var(--vanna-space-4) var(--vanna-space-2);
      }

      .chat-messages.has-scroll::before {
        opacity: 0.5;
      }

      .rich-components-container {
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-4);
      }

      .rich-component-wrapper {
        margin: var(--vanna-space-2) 0;
        animation: fade-in-up 0.3s ease-out;
      }

      .unknown-component {
        background: var(--vanna-background-higher);
        border: 1px solid var(--vanna-outline-default);
        border-radius: var(--vanna-border-radius-md);
        padding: var(--vanna-space-4);
        font-family: var(--vanna-font-family-mono);
        font-size: 12px;
      }

      .unknown-component p {
        margin: 0 0 var(--vanna-space-2) 0;
        color: var(--vanna-foreground-dimmer);
      }

      .unknown-component pre {
        margin: 0;
        color: var(--vanna-foreground-dimmest);
        overflow-x: auto;
      }

      .chat-input-area {
        padding: var(--vanna-space-5) var(--vanna-space-6) var(--vanna-space-6);
        background: var(--chat-surface);
        border-top: 1px solid var(--chat-outline);
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-4);
        flex-shrink: 0; /* Prevent input area from shrinking */
      }

      :host([theme="dark"]) .chat-input-area {
        border-top-color: rgba(148, 163, 184, 0.22);
      }

      /* Above the prompt, not below: the prompt sits at the bottom of the
         window, so a menu underneath it would open off-screen. */
      .command-menu {
        display: flex;
        flex-direction: column;
        margin-bottom: 8px;
        border: 1px solid var(--vanna-border, #e2e8f0);
        border-radius: 10px;
        background: var(--vanna-surface, #fff);
        box-shadow: 0 8px 24px rgb(15 23 42 / 12%);
        overflow: hidden;
      }

      :host([theme='dark']) .command-menu {
        border-color: #334155;
        background: #1e293b;
      }

      .command-item {
        display: flex;
        gap: 10px;
        align-items: baseline;
        padding: 8px 12px;
        border: 0;
        background: none;
        font: inherit;
        text-align: start;
        cursor: pointer;
        color: inherit;
      }

      .command-item.active {
        background: var(--vanna-hover, #f1f5f9);
      }

      :host([theme='dark']) .command-item.active {
        background: #334155;
      }

      .command-name {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.82rem;
        white-space: nowrap;
      }

      .command-arg,
      .command-desc {
        opacity: 0.65;
        font-size: 0.78rem;
      }

      .chat-input-container {
        display: flex;
        align-items: center;
        gap: var(--vanna-space-2);
        padding: 6px 8px 6px 18px;
        border-radius: 999px;
        background: var(--chat-muted);
        border: 1px solid var(--chat-muted-stronger);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.6);
        transition: border-color var(--vanna-duration-200) ease, box-shadow var(--vanna-duration-200) ease, background var(--vanna-duration-200) ease;
      }

      .chat-input-container:focus-within {
        border-color: var(--chat-primary);
        box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.85);
        background: rgba(255, 255, 255, 0.95);
      }

      :host([theme="dark"]) .chat-input-container {
        background: rgba(15, 23, 42, 0.65);
        border-color: rgba(100, 116, 139, 0.45);
        box-shadow: inset 0 1px 0 rgba(148, 163, 184, 0.18);
      }

      :host([theme="dark"]) .chat-input-container:focus-within {
        border-color: rgba(129, 140, 248, 0.55);
        box-shadow: 0 0 0 1px rgba(129, 140, 248, 0.45), inset 0 1px 0 rgba(148, 163, 184, 0.25);
        background: rgba(30, 41, 59, 0.88);
      }

      .message-input {
        flex: 1;
        border: none;
        background: transparent;
        font-size: 15px;
        font-family: var(--vanna-font-family-default);
        line-height: 1.5;
        color: var(--vanna-foreground-default);
        resize: none;
        min-height: 48px;
        max-height: 140px;
        padding: 12px 0;
        outline: none;
      }

      :host([theme="dark"]) .message-input {
        color: rgba(226, 232, 240, 0.95);
      }

      .message-input::placeholder {
        color: rgba(71, 85, 105, 0.8);
      }

      :host([theme="dark"]) .message-input::placeholder {
        color: rgba(148, 163, 184, 0.65);
      }

      .message-input:focus {
        outline: none;
      }

      .message-input:disabled {
        color: rgba(148, 163, 184, 0.65);
        cursor: not-allowed;
      }

      :host([theme="dark"]) .message-input:disabled {
        color: rgba(100, 116, 139, 0.55);
      }

      .send-button {
        width: 48px;
        height: 48px;
        border-radius: 999px;
        border: none;
        background: linear-gradient(135deg, var(--chat-primary-stronger), var(--chat-primary));
        color: var(--chat-primary-foreground);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        transition: transform var(--vanna-duration-200) ease, box-shadow var(--vanna-duration-200) ease, filter var(--vanna-duration-200) ease;
        box-shadow: 0 18px 38px -24px rgba(79, 70, 229, 0.8);
      }

      .send-button:hover {
        transform: translateY(-1px) scale(1.02);
        box-shadow: 0 25px 45px -24px rgba(79, 70, 229, 0.85);
      }

      .send-button:active {
        transform: translateY(0) scale(0.98);
      }

      .send-button:disabled {
        background: rgba(148, 163, 184, 0.35);
        color: rgba(71, 85, 105, 0.7);
        cursor: not-allowed;
        transform: none;
        box-shadow: none;
      }

      /* Stop reads as an interruption, not a primary action, so it drops the
         gradient and takes a neutral-but-warm tone. It occupies the send
         button's exact position deliberately: the control the user reaches for
         to stop should be where their pointer already is. */
      .send-button.stop-button {
        background: var(--chat-danger, #dc2626);
        box-shadow: 0 18px 38px -24px rgba(220, 38, 38, 0.8);
      }

      .send-button.stop-button:hover {
        box-shadow: 0 25px 45px -24px rgba(220, 38, 38, 0.85);
      }

      /* Answer actions -------------------------------------------------- */
      /* Present to assistive technology, absent visually. display:none
         would remove it from the accessibility tree too, which defeats
         the purpose. */
      .visually-hidden {
        position: absolute;
        width: 1px;
        height: 1px;
        margin: -1px;
        padding: 0;
        overflow: hidden;
        clip: rect(0 0 0 0);
        clip-path: inset(50%);
        white-space: nowrap;
        border: 0;
      }

      .answer-actions {
        display: flex;
        align-items: center;
        gap: var(--vanna-space-2, 8px);
        padding: var(--vanna-space-2, 8px) var(--vanna-space-3, 12px);
        flex-wrap: wrap;
        font-size: 0.8125rem;
        color: var(--chat-text-muted, #64748b);
      }

      .answer-actions-label {
        margin-right: auto;
      }

      .answer-action {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 4px 10px;
        border-radius: 999px;
        border: 1px solid var(--chat-border, rgba(148, 163, 184, 0.4));
        background: transparent;
        color: inherit;
        font-size: 0.75rem;
        cursor: pointer;
        transition: background var(--vanna-duration-150, 150ms) ease,
                    border-color var(--vanna-duration-150, 150ms) ease,
                    color var(--vanna-duration-150, 150ms) ease;
      }

      .answer-action:hover {
        background: var(--chat-surface-hover, rgba(148, 163, 184, 0.12));
        border-color: var(--chat-primary, #4f46e5);
      }

      /* The selected rating stays visibly selected. Feedback that does not
         persist visually reads as ignored, and users stop giving it. */
      .answer-action.active {
        background: var(--chat-primary, #4f46e5);
        border-color: var(--chat-primary, #4f46e5);
        color: var(--chat-primary-foreground, #ffffff);
      }

      .answer-actions-thanks {
        font-size: 0.75rem;
        opacity: 0.75;
      }

      :host([theme="dark"]) .answer-action {
        border-color: rgba(148, 163, 184, 0.3);
      }

      .send-button svg {
        width: 18px;
        height: 18px;
      }

      .sidebar {
        background: linear-gradient(180deg, rgba(99, 102, 241, 0.08) 0%, rgba(15, 23, 42, 0.02) 100%);
        padding: var(--vanna-space-6);
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-4);
        overflow-y: auto;
        overflow-x: hidden;
        min-height: 0;
      }

      .sidebar::-webkit-scrollbar {
        width: 6px;
      }

      .sidebar::-webkit-scrollbar-track {
        background: transparent;
      }

      .sidebar::-webkit-scrollbar-thumb {
        background: var(--vanna-outline-default);
        border-radius: var(--vanna-border-radius-full);
      }

      :host([theme="dark"]) .sidebar {
        background: linear-gradient(180deg, rgba(79, 70, 229, 0.22) 0%, rgba(15, 23, 42, 0.45) 100%);
      }

      .empty-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        color: var(--vanna-foreground-dimmer);
        padding: var(--vanna-space-12) var(--vanna-space-8);
        margin: var(--vanna-space-8) var(--vanna-space-6);
        font-size: 15px;
        font-weight: 500;
        line-height: 1.6;
        background: linear-gradient(135deg, 
          rgba(255, 255, 255, 0.95) 0%, 
          rgba(248, 250, 252, 0.9) 50%,
          rgba(241, 245, 249, 0.85) 100%);
        border-radius: var(--vanna-border-radius-2xl);
        border: 2px dashed var(--vanna-accent-primary-default);
        box-shadow: 
          var(--vanna-shadow-sm),
          inset 0 1px 0 rgba(255, 255, 255, 0.8);
        backdrop-filter: blur(8px);
        transition: all var(--vanna-duration-300) ease;
      }

      .empty-state:hover {
        border-color: var(--vanna-accent-primary-stronger);
        transform: translateY(-2px);
        box-shadow: 
          var(--vanna-shadow-lg),
          inset 0 1px 0 rgba(255, 255, 255, 0.9);
      }

      :host([theme="dark"]) .empty-state {
        color: var(--vanna-foreground-dimmer);
        background: linear-gradient(135deg, 
          rgba(24, 29, 39, 0.95) 0%, 
          rgba(31, 39, 51, 0.9) 50%,
          rgba(17, 21, 28, 0.85) 100%);
        border-color: var(--vanna-accent-primary-default);
        box-shadow: 
          var(--vanna-shadow-md),
          inset 0 1px 0 rgba(129, 140, 248, 0.2);
      }

      :host([theme="dark"]) .empty-state:hover {
        border-color: var(--vanna-accent-primary-hover);
        box-shadow: 
          var(--vanna-shadow-xl),
          inset 0 1px 0 rgba(129, 140, 248, 0.3);
      }

      .empty-state-icon {
        width: 64px;
        height: 64px;
        margin: 0 auto var(--vanna-space-6);
        opacity: 0.7;
        color: var(--vanna-accent-primary-default);
        filter: drop-shadow(0 2px 4px rgba(79, 70, 229, 0.2));
      }

      .empty-state-text {
        font-size: 16px;
        font-weight: 600;
        color: var(--vanna-foreground-default);
        margin-bottom: var(--vanna-space-2);
      }

      .empty-state-subtitle {
        font-size: 14px;
        color: var(--vanna-foreground-dimmest);
        opacity: 0.8;
        font-weight: 400;
      }

      @media (max-width: 880px) {
        .chat-layout {
          grid-template-columns: 1fr;
          height: min(600px, 85vh);
          max-height: 85vh;
        }

        .sidebar {
          display: none;
        }

        .chat-main {
          border-right: none;
        }
      }

      @media (max-width: 600px) {
        :host {
          border-radius: var(--vanna-border-radius-xl);
        }

        .chat-layout {
          height: min(500px, 80vh);
          max-height: 80vh;
        }

        .chat-header {
          border-bottom-width: 0;
          padding: var(--vanna-space-5) var(--vanna-space-5) var(--vanna-space-4);
        }

        .chat-messages {
          padding: var(--vanna-space-4) var(--vanna-space-4);
        }

        .empty-state {
          padding: var(--vanna-space-10) var(--vanna-space-6);
          margin: var(--vanna-space-6) var(--vanna-space-4);
          font-size: 14px;
        }

        .empty-state-text {
          font-size: 15px;
        }

        .empty-state-icon {
          width: 56px;
          height: 56px;
          margin-bottom: var(--vanna-space-5);
        }

        .chat-input-area {
          padding: var(--vanna-space-4) var(--vanna-space-4) var(--vanna-space-5);
        }
      }
    `
  ];

  @property() title = 'DataLens Chat';
  /**
   * Interface language. The host page owns this: the chat is one panel inside a
   * page that already has a language picker, and two independent language
   * settings on one screen is a bug, not a feature.
   */
  @property({ reflect: true }) locale = 'en';
  /**
   * Empty means "use the locale's default". A host that sets `placeholder`
   * explicitly keeps it in every language -- an explicit value from the embedder
   * is a decision, and silently translating over it would be wrong.
   */
  @property() placeholder = '';
  @property({ type: Boolean }) disabled = false;
  @property({ type: Boolean }) showProgress = true;
  @property({ type: Boolean }) allowMinimize = true;
  @property({ reflect: true }) theme = 'light';
  @property({ attribute: 'api-base' }) apiBaseUrl = '';
  @property({ attribute: 'sse-endpoint' }) sseEndpoint = '/api/vanna/v2/chat_sse';
  @property({ attribute: 'ws-endpoint' }) wsEndpoint = '/api/vanna/v2/chat_websocket';
  @property({ attribute: 'poll-endpoint' }) pollEndpoint = '/api/vanna/v2/chat_poll';
  @property() subtitle = '';
  @property() startingState: 'normal' | 'maximized' | 'minimized' = 'normal';

  /** Shorthand for a translated string in the current locale. */
  private t(key: string): string {
    return translate(this.locale, key);
  }

  /**
   * Slash commands, offered the way an editor offers them.
   *
   * These are not questions -- `DefaultWorkflow.try_handle` intercepts them
   * before the LLM is ever called, so a near miss is not a bad answer, it is a
   * sentence sent to a model as if it were a question. They were reachable only
   * by knowing they existed and typing them exactly; the ask was for a `/` menu
   * "like in claude and codex". So typing `/` at the start of an empty prompt
   * opens the list, and the list is the documentation.
   *
   * `admin` marks a command the server refuses to non-admins ("Access Denied");
   * those are hidden rather than offered and then rejected.
   */
  @property({ type: Boolean }) isAdmin = false;

  @state() private commandOpen = false;
  /** Index into `visibleCommands()`, moved with the arrow keys. */
  @state() private commandIndex = 0;

  @state() private currentMessage = '';
  @state() private status: 'idle' | 'working' | 'error' | 'success' = 'idle';
  /** True while a response is streaming; drives the send/stop toggle. */
  @state() private isStreaming = false;
  /** Rating already given for the current turn, so the UI can confirm it. */
  @state() private lastFeedback: 'positive' | 'negative' | null = null;
  /** Identifies the turn a rating applies to. */
  @state() private lastRequestId = '';
  @state() private lastQuestion = '';
  /** Lets the user cancel an in-flight response. */
  private abortController: AbortController | null = null;
  /** Transient "Copied" confirmation on the copy button. */
  @state() private copiedLabel = '';
  /**
   * Text announced to screen readers.
   *
   * Streamed content arrives by mutating the DOM, which assistive technology
   * does not narrate on its own -- so without an explicit live region a
   * screen-reader user gets silence from the moment they press send until they
   * manually re-read the page, with no indication anything happened.
   */
  @state() private announcement = '';
  @state() private statusMessage = '';
  @state() private statusDetail = '';
  private _windowState: 'normal' | 'maximized' | 'minimized' = 'normal';

  @property({ reflect: false })
  get windowState() {
    return this._windowState;
  }

  set windowState(value: 'normal' | 'maximized' | 'minimized') {
    console.log('windowState setter called with:', value);
    console.trace('Call stack:');
    const oldValue = this._windowState;
    this._windowState = value;
    this.requestUpdate('windowState', oldValue);
  }

  private apiClient!: VannaApiClient;
  /** Headers the host set, kept so they survive an API client rebuild. */
  private customHeaders: Record<string, string> | null = null;
  private conversationId: string;
  private componentManager: ComponentManager | null = null;
  private componentObserver: MutationObserver | null = null;

  constructor() {
    super();
    // Note: Don't create apiClient here - attributes haven't been set yet!
    // It will be created lazily in getApiClient() or firstUpdated()
    this.conversationId = this.generateId();
  }

  /**
   * Ensure API client is created/updated with current endpoint values
   */
  private ensureApiClient() {
    // Always recreate to ensure we have the latest endpoint values
    console.log('[VannaChat] Creating API client with:', {
      baseUrl: this.apiBaseUrl,
      sseEndpoint: this.sseEndpoint,
      wsEndpoint: this.wsEndpoint,
      pollEndpoint: this.pollEndpoint
    });

    this.apiClient = new VannaApiClient({
      baseUrl: this.apiBaseUrl,
      sseEndpoint: this.sseEndpoint,
      wsEndpoint: this.wsEndpoint,
      pollEndpoint: this.pollEndpoint
    });

    // Re-apply headers the host set earlier. The client is recreated whenever
    // the base URL changes, and without this the identity headers a host
    // application configured once would silently vanish mid-session -- the
    // requests keep working, they just stop being authenticated as anyone.
    if (this.customHeaders) {
      this.apiClient.setCustomHeaders(this.customHeaders);
    }
  }

  firstUpdated() {
    // Create API client now that attributes have been set
    this.ensureApiClient();

    // Initialize component manager with rich components container (fallback)
    const richContainer = this.shadowRoot?.querySelector('.rich-components-container') as HTMLElement;
    if (richContainer) {
      this.componentManager = new ComponentManager(richContainer);
      
      // Watch for changes in the rich components container to manage empty state
      this.componentObserver = new MutationObserver(() => {
        // Update empty state visibility
        this.updateEmptyState();
      });
      
      this.componentObserver.observe(richContainer, {
        childList: true,
        subtree: true,
        attributes: false
      });
    }

    // Set initial window state from startingState property
    if (this.startingState !== 'normal') {
      this._windowState = this.startingState;
    }

    // Set initial CSS class
    this.classList.add(this._windowState);

    // Announce readiness *before* the first network call, so a host listening
    // for this event can call setCustomHeaders() synchronously and have the
    // starter request already carry the user's identity. Firing it afterwards
    // would make the very first request to the backend an anonymous one.
    this.dispatchEvent(new CustomEvent('vanna-ready', {
      detail: { chat: this },
      bubbles: true,
      composed: true
    }));

    // Request starter UI from backend
    this.requestStarterUI();
  }

  /**
   * Request starter UI (buttons, welcome messages) from backend
   */
  private async requestStarterUI(): Promise<void> {
    try {
      const request = {
        message: "",
        conversation_id: this.conversationId,
        request_id: this.generateId(),
        metadata: {
          starter_ui_request: true
        }
      };

      // Stream the starter UI response
      await this.handleStreamingResponse(request);
    } catch (error) {
      console.error('Error requesting starter UI:', error);
      // Fail silently - starter UI is optional
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    
    // Clean up mutation observer
    if (this.componentObserver) {
      this.componentObserver.disconnect();
      this.componentObserver = null;
    }
  }

  updated(changedProperties: Map<string, any>) {
    super.updated(changedProperties);

    // Update host classes based on window state
    if (changedProperties.has('windowState')) {
      console.log('windowState changed to:', this._windowState);
      this.classList.remove('normal', 'maximized', 'minimized');
      this.classList.add(this._windowState);
      console.log('Applied CSS classes:', this.className);
    }

    // Text direction is set on the host, not inside the shadow root, so it
    // inherits into the shadow tree the way `dir` is meant to. The interface
    // mirrors; SQL, tables and charts inside the answer do not -- those carry
    // their own dir, because a mirrored query is unreadable in any language.
    if (changedProperties.has('locale')) {
      this.setAttribute('dir', isRtl(this.locale) ? 'rtl' : 'ltr');
      this.setAttribute('lang', this.locale);
    }
  }

  /**
   * The commands `DefaultWorkflow` answers without calling the model.
   *
   * `arg` is the placeholder shown after the name for a command that needs one:
   * `/delete` alone is not a command, it is `/delete <memory id>`, so choosing
   * it leaves the caret in the prompt instead of sending.
   */
  private static readonly COMMANDS: ReadonlyArray<{
    name: string;
    key: string;
    arg?: string;
    admin?: boolean;
  }> = [
    { name: '/help', key: 'cmd.help' },
    { name: '/status', key: 'cmd.status' },
    { name: '/setup', key: 'cmd.setup' },
    { name: '/memorise', key: 'cmd.memorise', arg: '<text>' },
    { name: '/memories', key: 'cmd.memories', admin: true },
    { name: '/delete', key: 'cmd.delete', arg: '<id>', admin: true },
  ];

  /** The commands this user may run, narrowed to what they have typed. */
  private visibleCommands() {
    const typed = this.currentMessage.trim().toLowerCase();
    return VannaChat.COMMANDS.filter(
      (command) =>
        (this.isAdmin || !command.admin) && command.name.startsWith(typed),
    );
  }

  /**
   * Put a command in the prompt, and send it if it is complete.
   *
   * A command that takes an argument is never sent from here -- `/delete` with
   * no id is an error message, so the menu writes `/delete ` and gets out of
   * the way.
   */
  private chooseCommand(command: { name: string; arg?: string }) {
    this.commandOpen = false;
    if (command.arg) {
      this.currentMessage = command.name + ' ';
      const input = this.shadowRoot?.querySelector('.message-input') as
        | HTMLTextAreaElement
        | null;
      if (input) {
        input.value = this.currentMessage;
        input.focus();
      }
      return;
    }
    void this.sendMessage(command.name);
  }

  private handleInput(e: Event) {
    const input = e.target as HTMLInputElement;
    this.currentMessage = input.value;
    // Open only while the whole prompt is the command being typed. A slash
    // inside a question ("revenue w/ tax") is a slash, not a command.
    this.commandOpen = /^\/\S*$/.test(input.value);
    this.commandIndex = 0;
  }

  private handleKeyPress(e: KeyboardEvent) {
    if (this.commandOpen) {
      const options = this.visibleCommands();
      if (options.length === 0) {
        this.commandOpen = false;
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const step = e.key === 'ArrowDown' ? 1 : options.length - 1;
        this.commandIndex = (this.commandIndex + step) % options.length;
        return;
      } else if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        this.chooseCommand(options[this.commandIndex] ?? options[0]);
        return;
      } else if (e.key === 'Escape') {
        e.preventDefault();
        this.commandOpen = false;
        return;
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      this.sendMessage();
    }
  }

  /** The menu itself. Rendered above the prompt, like every editor's. */
  private renderCommandMenu() {
    if (!this.commandOpen) return '';
    const options = this.visibleCommands();
    if (options.length === 0) return '';
    return html`
      <div class="command-menu" role="listbox" aria-label=${this.t('cmd.title')}>
        ${options.map(
          (command, index) => html`
            <button
              type="button"
              role="option"
              aria-selected=${index === this.commandIndex}
              class="command-item ${index === this.commandIndex ? 'active' : ''}"
              @mouseenter=${() => {
                this.commandIndex = index;
              }}
              @mousedown=${(event: Event) => {
                // mousedown, not click: the prompt must not lose focus first.
                event.preventDefault();
                this.chooseCommand(command);
              }}
            >
              <span class="command-name"
                >${command.name}${command.arg
                  ? html` <span class="command-arg">${command.arg}</span>`
                  : ''}</span
              >
              <span class="command-desc">${this.t(command.key)}</span>
            </button>
          `,
        )}
      </div>
    `;
  }

  /**
   * Send a message programmatically (can be called from buttons or external code)
   * Returns a Promise that resolves with success status
   */

  /**
   * True when an error is the result of the user pressing stop.
   *
   * Browsers signal an aborted fetch with a DOMException named 'AbortError',
   * but a stream cancelled mid-read can surface differently depending on the
   * engine, so the signal's own state is checked as well. Getting this wrong
   * would send a cancelled request down the polling fallback path -- i.e.
   * re-running exactly what the user just stopped.
   */
  private wasAborted(error: unknown): boolean {
    if (this.abortController?.signal.aborted) return true;
    return error instanceof DOMException && error.name === 'AbortError';
  }

  /**
   * Announce *message* to assistive technology.
   *
   * The blank-then-set is deliberate: a live region only fires when its
   * content *changes*, so re-announcing identical text (two queries in a row
   * both returning "12 rows") would be silently dropped without the reset.
   */
  private announce(message: string): void {
    this.announcement = '';
    requestAnimationFrame(() => {
      this.announcement = message;
    });
  }

  /**
   * Copy the assistant's most recent answer to the clipboard.
   *
   * Falls back to a hidden textarea and `execCommand` because the async
   * clipboard API is unavailable on insecure origins -- and an embedded
   * analytics widget frequently runs on an internal host without HTTPS, which
   * is exactly where this would otherwise silently do nothing.
   */
  async copyLastAnswer(): Promise<void> {
    const text = this.lastAnswerText();
    if (!text) return;

    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const scratch = document.createElement('textarea');
        scratch.value = text;
        scratch.style.position = 'fixed';
        scratch.style.opacity = '0';
        document.body.appendChild(scratch);
        scratch.select();
        document.execCommand('copy');
        document.body.removeChild(scratch);
      }
      this.copiedLabel = 'Copied';
      this.announce(this.t('chat.copied'));
      setTimeout(() => {
        this.copiedLabel = '';
      }, 1800);
    } catch (error) {
      console.warn('Copy failed:', error);
      this.announce(this.t('chat.copyFailed'));
    }
  }

  /** Plain text of everything rendered for the current answer. */
  private lastAnswerText(): string {
    const container = this.renderRoot?.querySelector?.(
      '.rich-components-container'
    );
    return (container as HTMLElement)?.innerText?.trim() ?? '';
  }


  /**
   * A one-line description of the answer, for the live region.
   *
   * Reading the entire answer aloud would be hostile -- a 200-row table
   * narrated cell by cell is unusable. This states what arrived so the user
   * can decide whether to navigate into it.
   */
  private answerSummaryForScreenReader(): string {
    const container = this.renderRoot?.querySelector?.(
      '.rich-components-container'
    ) as HTMLElement | null;
    if (!container) return '';

    const parts: string[] = [];

    // Prefer the count the result carried over the number of <tr>s on screen:
    // the grid caps how many rows it renders, so counting the DOM announces the
    // page size and contradicts the row count printed beside it.
    const grids = container.querySelectorAll<HTMLElement>('[data-row-count]');
    if (grids.length) {
      const rows = Number(grids[grids.length - 1].dataset.rowCount || 0);
      parts.push(`${rows} row${rows === 1 ? '' : 's'} returned`);
    } else {
      const tables = container.querySelectorAll('table');
      if (tables.length) {
        const shown = Math.max(0, (tables[tables.length - 1] as HTMLTableElement).rows.length - 1);
        parts.push(`${shown} row${shown === 1 ? '' : 's'} shown`);
      }
    }
    if (container.querySelector('plotly-chart, .js-plotly-plot')) {
      parts.push('a chart was produced');
    }
    return parts.length ? parts.join(', ') + '.' : this.t('status.seeResponse');
  }

  /** Cancel the in-flight response. */
  stopStreaming(): void {
    if (!this.abortController) return;
    this.abortController.abort();
    this.abortController = null;
    this.isStreaming = false;
    this.setStatus('idle', this.t('status.stopped'), this.t('status.cancelled'));
    this.announce(this.t('status.stopped'));
    this.dispatchEvent(
      new CustomEvent('vanna-stopped', {
        detail: { conversationId: this.conversationId },
        bubbles: true,
        composed: true,
      })
    );
  }

  /**
   * Record the user's verdict on the last answer.
   *
   * Clicking the same rating twice clears it, so a misclick is recoverable.
   * The rating is reflected optimistically -- the click is the user's, and
   * making them wait on a network round trip to see their own input
   * acknowledged feels broken. Delivery failure is logged, not surfaced.
   */
  async submitFeedback(rating: 'positive' | 'negative'): Promise<void> {
    const next = this.lastFeedback === rating ? null : rating;
    this.lastFeedback = next;

    this.dispatchEvent(
      new CustomEvent('vanna-feedback', {
        detail: {
          rating: next,
          conversationId: this.conversationId,
          requestId: this.lastRequestId,
          question: this.lastQuestion,
        },
        bubbles: true,
        composed: true,
      })
    );

    if (next === null) return;

    await this.apiClient.submitFeedback({
      conversation_id: this.conversationId,
      request_id: this.lastRequestId,
      rating: next,
      question: this.lastQuestion,
      sql: this.lastSqlSeen() || undefined,
    });
  }

  /**
   * The most recent SQL rendered in this conversation, if any.
   *
   * Attaching it to the feedback lets the backend promote a positively-rated
   * question/SQL pair straight into the verified example store, which is the
   * whole point of collecting the rating.
   */
  private lastSqlSeen(): string | null {
    const blocks = this.renderRoot?.querySelectorAll?.('[data-vanna-sql]');
    if (!blocks || blocks.length === 0) return null;
    return (blocks[blocks.length - 1] as HTMLElement).textContent?.trim() || null;
  }

  /**
   * Download the most recent result table as CSV.
   *
   * Everything happens client-side from data already rendered -- no second
   * query, so the export cannot disagree with what the user is looking at,
   * and it costs the warehouse nothing.
   */
  exportLastTableAsCsv(): void {
    const tables = this.renderRoot?.querySelectorAll?.('table');
    if (!tables || tables.length === 0) return;
    const table = tables[tables.length - 1] as HTMLTableElement;

    const rows: string[] = [];
    for (const row of Array.from(table.rows)) {
      const cells = Array.from(row.cells).map((cell) =>
        this.csvEscape(cell.textContent ?? '')
      );
      rows.push(cells.join(','));
    }

    // A UTF-8 BOM plus CRLF endings. Excel mangles non-ASCII text without the
    // BOM and mis-parses rows without CRLF, and Excel is where exported CSVs
    // actually end up.
    const blob = new Blob(['﻿' + rows.join('\r\n')], {
      type: 'text/csv;charset=utf-8;',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `vanna-results-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  /**
   * Quote a CSV field.
   *
   * The leading apostrophe on values starting with =, +, -, or @ is a CSV
   * injection guard: spreadsheet software interprets those as formulas, so a
   * cell containing `=HYPERLINK(...)` from the database would execute on open.
   * Prefixing forces it to be read as text.
   */
  private csvEscape(value: string): string {
    const cleaned = value.replace(/\s+/g, ' ').trim();
    const guarded = /^[=+\-@]/.test(cleaned) ? `'${cleaned}` : cleaned;
    return `"${guarded.replace(/"/g, '""')}"`;
  }

  sendMessage(messageText?: string): Promise<boolean> {
    console.log('sendMessage called with:', messageText);

    // Use provided message or fall back to current input
    // Check if messageText is actually a string (not an event object)
    const textToSend = (typeof messageText === 'string') ? messageText : this.currentMessage;

    console.log('Will send:', textToSend);

    if (!textToSend.trim() || this.disabled) {
      console.log('Message empty or disabled, not sending');
      return Promise.resolve(false);
    }

    return this._sendMessageInternal(textToSend);
  }

  private async _sendMessageInternal(messageText: string): Promise<boolean> {
    console.log('_sendMessageInternal called with:', messageText);

    // Auto-maximize window when user sends a message (if not already maximized or minimized)
    if (this.windowState !== 'maximized' && this.windowState !== 'minimized') {
      this.maximizeWindow();
    }

    // Create user message as a rich component and send to ComponentManager
    const userRichComponent: RichComponent = {
      id: `user-message-${Date.now()}`,
      type: 'user-message',
      lifecycle: 'create',
      data: {
        content: messageText,
        sender: 'user'
      },
      children: [],
      timestamp: new Date().toISOString(),
      visible: true,
      interactive: false
    };

    // Add user message to ComponentManager for chronological ordering
    if (this.componentManager) {
      const update = {
        operation: 'create' as const,
        target_id: userRichComponent.id,
        component: userRichComponent,
        timestamp: userRichComponent.timestamp
      };
      this.componentManager.processUpdate(update);
    }

    // Update empty state after a brief delay to let ComponentManager render
    setTimeout(() => this.updateEmptyState(), 0);

    console.log('Added user message as rich component to ComponentManager:', userRichComponent);

    // Update the view
    this.requestUpdate();

    // Update status to working (initial frontend status before backend responds)
    this.setStatus('working', this.t('status.sending'), '');

    // Clear input only if we're sending from the input field
    if (messageText === this.currentMessage) {
      this.currentMessage = '';
      const input = this.shadowRoot?.querySelector('.message-input') as HTMLTextAreaElement;
      if (input) {
        input.value = '';
        input.style.height = 'auto';
      }
    }

    // Dispatch event for external listeners
    this.dispatchEvent(new CustomEvent('message-sent', {
      detail: { message: { content: messageText, type: 'user' } },
      bubbles: true,
      composed: true
    }));

    try {
      // Create the request
      const request = {
        message: messageText,
        conversation_id: this.conversationId,
        request_id: this.generateId(),
        metadata: {}
      };

      // Remember what this turn is, so a rating can be attributed to it, and
      // clear any previous rating -- a new question deserves its own verdict.
      this.lastRequestId = request.request_id;
      this.lastQuestion = messageText;
      this.lastFeedback = null;
      this.announce(this.t('status.sent'));

      // Stream the response
      await this.handleStreamingResponse(request);
      return true; // Success

    } catch (error) {
      console.error('Error sending message:', error);
      this.setStatus('error', 'Failed to send message', error instanceof Error ? error.message : 'Unknown error');

      // Add error message
      this.addMessage(
        `Sorry, I encountered an error: ${error instanceof Error ? error.message : 'Unknown error'}`,
        'assistant'
      );
      return false; // Failure
    }
  }

  private getTitleInitials(): string {
    const title = (this.title || '').trim();
    if (!title) {
      return 'VA';
    }

    const parts = title.split(/\s+/).filter(Boolean);
    if (parts.length === 1) {
      return parts[0].charAt(0).toUpperCase() || 'V';
    }

    const first = parts[0].charAt(0);
    const last = parts[parts.length - 1].charAt(0);
    const initials = `${first}${last}`.toUpperCase();
    return initials || 'VA';
  }

  private minimizeWindow(e?: Event) {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    console.log('minimizeWindow called, current state:', this._windowState);
    this.windowState = 'minimized';
    console.log('minimizeWindow set state to:', this._windowState);
    this.dispatchEvent(new CustomEvent('window-state-changed', {
      detail: { state: 'minimized' },
      bubbles: true,
      composed: true
    }));
  }

  private maximizeWindow(e?: Event) {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    this.windowState = 'maximized';
    this.dispatchEvent(new CustomEvent('window-state-changed', {
      detail: { state: 'maximized' },
      bubbles: true,
      composed: true
    }));
  }

  private restoreWindow(e?: Event) {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    this.windowState = 'normal';
    this.dispatchEvent(new CustomEvent('window-state-changed', {
      detail: { state: 'normal' },
      bubbles: true,
      composed: true
    }));
  }


  addMessage(content: string, type: 'user' | 'assistant') {
    // Create message as a rich component and send to ComponentManager
    const richComponent: RichComponent = {
      id: `${type}-message-${Date.now()}`,
      type: `${type}-message`,
      lifecycle: 'create',
      data: {
        content: content,
        sender: type
      },
      children: [],
      timestamp: new Date().toISOString(),
      visible: true,
      interactive: false
    };

    if (this.componentManager) {
      const update = {
        operation: 'create' as const,
        target_id: richComponent.id,
        component: richComponent,
        timestamp: richComponent.timestamp
      };
      this.componentManager.processUpdate(update);
    }
  }

  setStatus(status: typeof this.status, message: string, detail?: string) {
    this.status = status;
    this.statusMessage = message;
    this.statusDetail = detail || '';
  }

  clearStatus() {
    this.statusMessage = '';
    this.statusDetail = '';
    this.status = 'idle';
  }

  getProgressTracker(): HTMLElement | null {
    return this.shadowRoot?.querySelector('vanna-progress-tracker') || null;
  }

  private async handleStreamingResponse(request: any) {
    // Ensure API client exists and is up to date
    if (!this.apiClient || this.apiClient.baseUrl !== this.apiBaseUrl) {
      this.ensureApiClient();
    }

    // Note: Status bar updates are now controlled by backend via StatusBarUpdateComponent
    // Frontend only shows initial "Sending message..." status (set in _sendMessageInternal)
    // and handles connection errors below

    try {
      // Use SSE streaming by default. The abort signal lets the user stop
      // a long-running response instead of waiting it out.
      this.abortController = new AbortController();
      this.isStreaming = true;
      const stream = this.apiClient.streamChat(
        request,
        this.abortController.signal
      );

      for await (const chunk of stream) {
        await this.processChunk(chunk);
      }

      // The turn is over: re-enable the send button and reveal the rating
      // controls. Cleared here rather than in a finally, because the polling
      // fallback below is still part of the same logical turn.
      this.isStreaming = false;
      this.abortController = null;
      this.announce(this.t('status.ready') + ' ' + this.answerSummaryForScreenReader());

      // Backend is responsible for final status via StatusBarUpdateComponent
      // No frontend status clearing here

    } catch (error) {
      // A user-initiated stop is not a failure and must not trigger the
      // polling fallback -- retrying the request they just cancelled is
      // the opposite of what they asked for.
      if (this.wasAborted(error)) {
        this.isStreaming = false;
        this.abortController = null;
        this.setStatus('idle', this.t('status.stopped'), this.t('status.cancelled'));
        return true;
      }
      console.warn('SSE streaming failed, falling back to polling:', error);

      try {
        // Fallback to polling - show user we're retrying
        this.setStatus('working', this.t('status.retrying'), this.t('status.fallback'));
        const response = await this.apiClient.sendPollMessage(request);

        for (const chunk of response.chunks) {
          await this.processChunk(chunk);
        }

        // Backend is responsible for final status via StatusBarUpdateComponent

      } catch (pollError) {
        // Only set error status if polling also fails (connection error)
        this.setStatus('error', this.t('status.failed'), this.t('status.unreachable'));
        throw pollError;
      }
    }
  }

  private async processChunk(chunk: ChatStreamChunk) {
    // Dispatch chunk event for external listeners
    this.dispatchEvent(new CustomEvent('chunk-received', {
      detail: { chunk },
      bubbles: true,
      composed: true
    }));

    console.log('Processing chunk:', chunk); // Debug log

    // Handle rich components via ComponentManager
    if (chunk.rich && this.componentManager) {
      console.log('Processing rich component via ComponentManager:', chunk.rich); // Debug log
      
      if (chunk.rich.id && chunk.rich.lifecycle) {
        // Standard rich component with lifecycle
        const component = chunk.rich as RichComponent;
        const update = {
          operation: chunk.rich.lifecycle as any,
          target_id: chunk.rich.id,
          component: component,
          timestamp: new Date().toISOString()
        };
        this.componentManager.processUpdate(update);
      } else if (chunk.rich.type === 'component_update') {
        // Component update format
        this.componentManager.processUpdate(chunk.rich as any);
      } else {
        // Generic rich component
        const component = chunk.rich as RichComponent;
        const update = {
          operation: 'create' as const,
          target_id: component.id || `component-${Date.now()}`,
          component: component,
          timestamp: new Date().toISOString()
        };
        this.componentManager.processUpdate(update);
      }
      
      return;
    }

    // Update progress tracker for legacy components (keep for backward compatibility)
    const progressTracker = this.getProgressTracker();
    if (progressTracker && 'addStep' in progressTracker) {
      (progressTracker as any).addStep({
        id: `chunk-${Date.now()}`,
        title: this.getChunkTitle(chunk),
        status: 'completed',
        timestamp: chunk.timestamp
      });
    }

    // Handle different chunk types (legacy components)
    const componentType = chunk.rich?.type;
    switch (componentType) {
      case 'text':
        // Text chunks are handled in the main loop
        break;

      case 'thinking':
        // Legacy: Status bar updates now handled by backend via StatusBarUpdateComponent
        // This case is kept for backward compatibility but doesn't update status
        break;

      case 'tool_execution':
        // Legacy: Status bar updates now handled by backend via StatusBarUpdateComponent
        // This case is kept for backward compatibility but doesn't update status
        break;

      case 'error':
        throw new Error(chunk.rich.data?.message || 'Unknown error from agent');

      default:
        // Handle other component types as needed
        console.log('Received chunk:', componentType, chunk.rich);
    }
  }


  private getChunkTitle(chunk: ChatStreamChunk): string {
    const componentType = chunk.rich?.type;
    switch (componentType) {
      case 'text':
        return this.t('status.working');
      case 'thinking':
        return 'Thinking';
      case 'tool_execution':
        return `Tool: ${chunk.rich.data?.tool_name || 'Unknown'}`;
      default:
        return `Processing ${componentType || 'component'}`;
    }
  }

  private generateId(): string {
    return `${Date.now()}-${Math.random().toString(36).substring(2, 11)}`;
  }

  /**
   * Update the API base URL and recreate the client
   */
  updateApiBaseUrl(baseUrl: string) {
    this.apiBaseUrl = baseUrl;
    this.ensureApiClient();
  }

  /**
   * Get the API client instance for direct access
   */
  getApiClient(): VannaApiClient {
    if (!this.apiClient) {
      this.ensureApiClient();
    }
    return this.apiClient;
  }

  /**
   * Switch to another conversation, optionally replaying its messages.
   *
   * The conversation id is generated once in the constructor and never changes,
   * which is correct for a single-threaded widget and wrong for a host that
   * offers a thread list -- without this, switching threads would keep posting
   * to the previous conversation and silently merge the two transcripts.
   *
   * Passing `messages` re-renders the stored exchange so a reopened thread
   * reads as it did when it was live.
   */
  loadConversation(
    conversationId: string,
    messages: Array<{ role: string; content: string }> = []
  ) {
    this.conversationId = conversationId;
    this.clearMessages();

    for (const message of messages) {
      if (!message.content) continue;
      this.appendRestoredMessage(
        message.content,
        message.role === 'user' ? 'user' : 'assistant'
      );
    }

    this.updateEmptyState();
    this.requestUpdate();

    // The banner belongs to the *session*, not to any one thread: it reports
    // what this deployment can do, so it is as true of a replayed conversation
    // as of a new one. It is deliberately not part of the stored transcript --
    // the messages the server keeps are the conversation itself -- so it has to
    // be asked for again after a replay rather than restored with them.
    void this.requestStarterUI();
  }

  /**
   * Start an empty conversation under a fresh id.
   *
   * The previous conversation is *not* deleted -- it is stored server-side and
   * still listed in the rail. Only this view is cleared, and a fresh id means
   * the next message opens a new thread instead of appending to the old one.
   *
   * The starter card is requested again afterwards. Without that, clearing the
   * transcript also wiped the setup banner and the suggested actions, so "new
   * conversation" looked like it had emptied the product rather than opened a
   * clean thread.
   */
  newConversation(): string {
    this.conversationId = this.generateId();
    this.clearMessages();
    this.updateEmptyState();
    this.requestUpdate();
    void this.requestStarterUI();
    return this.conversationId;
  }

  /**
   * Render one stored message.
   *
   * Restored messages are plain text on purpose. The rich components a live
   * answer produces -- tables, charts -- are backed by result files that may be
   * long gone, so replaying them would show empty frames where data used to be.
   */
  private appendRestoredMessage(content: string, sender: 'user' | 'assistant') {
    if (!this.componentManager) return;

    const component: RichComponent = {
      id: `restored-${sender}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      type: sender === 'user' ? 'user-message' : 'text',
      lifecycle: 'create',
      // Both branches use `content`. The assistant branch passed `{ text }`,
      // but `TextComponentRenderer` destructures `content` from `data` -- so
      // every restored answer rendered an empty div and a replayed conversation
      // showed the questions with nothing between them. The transcript was
      // stored correctly all along; only the replay dropped it.
      data: sender === 'user' ? { content, sender } : { content, markdown: true },
      children: [],
      timestamp: new Date().toISOString(),
      visible: true,
      interactive: false,
    };

    this.componentManager.processUpdate({
      operation: 'create' as const,
      target_id: component.id,
      component,
      timestamp: component.timestamp,
    });
  }

  /**
   * Set custom headers for authentication or other purposes.
   *
   * Safe to call before the component has rendered: the headers are stored and
   * applied to the API client as soon as it exists.
   */
  setCustomHeaders(headers: Record<string, string>) {
    this.customHeaders = { ...headers };
    if (this.apiClient) {
      this.apiClient.setCustomHeaders(this.customHeaders);
    }
  }

  /**
   * Update empty state visibility based on whether there are components
   */
  private updateEmptyState() {
    const emptyState = this.shadowRoot?.querySelector('#empty-state') as HTMLElement;
    const richContainer = this.shadowRoot?.querySelector('.rich-components-container') as HTMLElement;
    
    if (emptyState && richContainer) {
      // Show empty state if rich container has no children
      const hasContent = richContainer.children.length > 0;
      emptyState.style.display = hasContent ? 'none' : 'flex';
    }
  }

  /**
   * Update scroll indicator based on scroll position
   */
  private updateScrollIndicator() {
    const messagesContainer = this.shadowRoot?.querySelector('.chat-messages');
    if (!messagesContainer) return;
    
    // Check if there's content scrolled above
    const hasScrolledContent = messagesContainer.scrollTop > 10;
    
    // Update scroll indicator class
    messagesContainer.classList.toggle('has-scroll', hasScrolledContent);
  }

  /**
   * Scroll to the top of the last message/component that was added
   * This always scrolls regardless of current scroll position
   */
  scrollToLastMessage() {
    const messagesContainer = this.shadowRoot?.querySelector('.chat-messages');
    const richContainer = this.shadowRoot?.querySelector('.rich-components-container');
    
    if (!messagesContainer || !richContainer) return;

    // Get the last child element (the most recently added component)
    const lastComponent = richContainer.lastElementChild as HTMLElement;
    if (!lastComponent) return;

    // Scroll so the top of the last component is visible
    lastComponent.scrollIntoView({ behavior: 'smooth', block: 'start' });
    
    // Update scroll indicator after scrolling
    setTimeout(() => this.updateScrollIndicator(), 100);
  }

  /**
   * Rating and export controls for the answer just delivered.
   *
   * Only rendered once a turn has completed and produced something -- offering
   * to rate an empty conversation, or asking for a verdict while the answer is
   * still streaming, both invite meaningless clicks.
   */
  private renderAnswerActions() {
    if (this.isStreaming || !this.lastRequestId) return '';

    return html`
      <div class="answer-actions" role="group" aria-label="Answer actions">
        <span class="answer-actions-label">Was this answer correct?</span>
        <button
          class="answer-action ${this.lastFeedback === 'positive' ? 'active' : ''}"
          type="button"
          aria-pressed=${this.lastFeedback === 'positive'}
          title="Correct — this may be saved as a verified example"
          @click=${() => this.submitFeedback('positive')}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <path d="M1 21h4V9H1v12zm22-11a2 2 0 0 0-2-2h-6.31l.95-4.57.03-.32a1.5 1.5 0 0 0-.44-1.06L14.17 1 7.59 7.59A2 2 0 0 0 7 9v10a2 2 0 0 0 2 2h9a2 2 0 0 0 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/>
          </svg>
          <span>Yes</span>
        </button>
        <button
          class="answer-action ${this.lastFeedback === 'negative' ? 'active' : ''}"
          type="button"
          aria-pressed=${this.lastFeedback === 'negative'}
          title="Incorrect — flag this answer for review"
          @click=${() => this.submitFeedback('negative')}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <path d="M15 3H6a2 2 0 0 0-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2a2 2 0 0 0 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59A2 2 0 0 0 17 15V5a2 2 0 0 0-2-2zm4 0v12h4V3h-4z"/>
          </svg>
          <span>No</span>
        </button>
        <button
          class="answer-action"
          type="button"
          title="Copy the answer to the clipboard"
          @click=${this.copyLastAnswer}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <path d="M16 1H4a2 2 0 0 0-2 2v14h2V3h12V1zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2zm0 16H8V7h11v14z"/>
          </svg>
          <span>${this.copiedLabel || 'Copy'}</span>
        </button>
        <button
          class="answer-action"
          type="button"
          title="Download the result table as CSV"
          @click=${this.exportLastTableAsCsv}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/>
          </svg>
          <span>CSV</span>
        </button>
        ${this.lastFeedback
          ? html`<span class="answer-actions-thanks">Thanks — recorded.</span>`
          : ''}
      </div>
    `;
  }

  /**
   * Clear all messages (useful for testing)
   */
  clearMessages() {
    if (this.componentManager) {
      this.componentManager.clear();
    }
    this.updateEmptyState();
    this.requestUpdate();
  }

  /**
   * Add multiple messages at once (useful for testing scrolling)
   */
  addTestMessages(count: number = 10) {
    for (let i = 1; i <= count; i++) {
      setTimeout(() => {
        const type = i % 2 === 0 ? 'assistant' : 'user';
        const content = `This is test message number ${i}. Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.`;
        this.addMessage(content, type);
      }, i * 100); // Stagger the messages to simulate real timing
    }
  }

  render() {
    return html`
      <!-- Minimized icon - shown only when minimized via CSS and allowMinimize is true -->
      ${this.allowMinimize ? html`
        <div class="minimized-icon" @click=${this.restoreWindow}>
          <svg viewBox="0 0 24 24" fill="currentColor" width="32" height="32">
            <path d="M20 2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h14l4 4V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/>
          </svg>
        </div>
      ` : ''}

      <!-- Main chat interface -->
      <div class="chat-layout ${this.showProgress ? '' : 'compact'}">
        <div class="chat-main">
          <div class="chat-header">
            <div class="header-top">
              <div class="header-left">
                <div class="chat-avatar" aria-hidden="true">${this.getTitleInitials()}</div>
                <div class="header-text">
                  <h2 class="chat-title">${this.title}</h2>
                </div>
              </div>
              <div class="header-top-actions">
                <div class="window-controls">
                  ${this.allowMinimize ? html`
                    <button
                      class="window-control-btn minimize"
                      @click=${this.minimizeWindow}
                      title="Minimize">
                      <svg viewBox="0 0 24 24" fill="currentColor">
                        <path d="M5 12h14v2H5z"/>
                      </svg>
                    </button>
                  ` : ''}
                  ${this.windowState === 'maximized' ? html`
                    <button
                      class="window-control-btn restore"
                      @click=${this.restoreWindow}
                      title="Restore">
                      <svg viewBox="0 0 24 24" fill="currentColor">
                        <path d="M8 8v2h2V8h6v6h-2v2h4V6H8zm-2 4v8h8v-2H8v-6H6z"/>
                      </svg>
                    </button>
                  ` : html`
                    <button
                      class="window-control-btn maximize"
                      @click=${this.maximizeWindow}
                      title="Maximize">
                      <svg viewBox="0 0 24 24" fill="currentColor">
                        <path d="M5 5v14h14V5H5zm2 2h10v10H7V7z"/>
                      </svg>
                    </button>
                  `}
                </div>
              </div>
            </div>
          </div>

          <div class="chat-messages">
            <!-- Empty state - shown when no components exist -->
            <div class="empty-state" id="empty-state">
              <div class="empty-state-icon">
                <svg viewBox="0 0 24 24" fill="currentColor">
                  <path d="M20 2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h14l4 4V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/>
                </svg>
              </div>
              <div class="empty-state-text">${this.t('chat.emptyTitle')}</div>
              <div class="empty-state-subtitle">${this.t('chat.emptySubtitle')}</div>
            </div>

            <!-- Rich Components Container - all content renders here via ComponentManager -->
            <div class="rich-components-container"></div>
          </div>

          <!--
            Screen-reader announcements. Visually hidden rather than
            display:none, because a display:none region is ignored by
            assistive technology entirely. "polite" waits for a pause in
            speech instead of interrupting mid-sentence.
          -->
          <div class="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
            ${this.announcement}
          </div>

          <div class="chat-input-area">
            <vanna-status-bar
              .status=${this.status}
              .message=${this.statusMessage}
              .detail=${this.statusDetail}
              theme=${this.theme}>
            </vanna-status-bar>

            ${this.renderAnswerActions()}

            ${this.renderCommandMenu()}

            <div class="chat-input-container">
              <textarea
                class="message-input"
                .placeholder=${this.placeholder || this.t('chat.placeholder')}
                .disabled=${this.disabled}
                @input=${this.handleInput}
                @keydown=${this.handleKeyPress}
                rows="1"
              ></textarea>
              ${this.isStreaming
                ? html`
                    <button
                      class="send-button stop-button"
                      type="button"
                      aria-label=${this.t('chat.stop')}
                      title=${this.t('chat.stop')}
                      @click=${this.stopStreaming}
                    >
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                        <rect x="6" y="6" width="12" height="12" rx="2" />
                      </svg>
                    </button>
                  `
                : html`
                    <button
                      class="send-button"
                      type="button"
                      aria-label=${this.t('chat.send')}
                      .disabled=${this.disabled || !this.currentMessage.trim()}
                      @click=${this.sendMessage}
                    >
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
                      </svg>
                    </button>
                  `}
            </div>
          </div>
        </div>

        ${this.showProgress ? html`
          <div class="sidebar">
            <vanna-progress-tracker theme=${this.theme}></vanna-progress-tracker>
          </div>
        ` : ''}
      </div>
    `;
  }
}
