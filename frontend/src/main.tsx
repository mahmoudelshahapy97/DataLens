import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './app/App';
import { applyTheme } from './lib/theme';
import './styles/tailwind.css';

// Before the first render, so the page never paints light and then flips. The
// attribute is read from localStorage['vanna.theme'] -- the same key the vanilla
// build used, so an existing user keeps their choice across the rewrite.
applyTheme();

const container = document.getElementById('root');
if (!container) throw new Error('#root is missing from index.html');

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
