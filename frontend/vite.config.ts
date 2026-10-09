/**
 * One Vite project for the whole UI.
 *
 * ## Why Vite and not Next
 *
 * nginx serves this app under `script-src 'self'` with no 'unsafe-inline'
 * (frontend/nginx.conf). Next injects its bootstrap as an inline <script> in
 * every mode it has -- SSR, and `output: 'export'` too, which still emits
 * `self.__next_f.push(...)` inline. Adopting it would have meant adding
 * 'unsafe-inline' to the policy, or standing up a Node container to mint a nonce
 * per request. Vite emits `<script type="module" src=...>` and nothing else, so
 * the policy, the nginx config, docker-compose and the cookie+CSRF auth are all
 * untouched by the rewrite.
 *
 * ## What changed from the previous config
 *
 * This was a *multi-page* build over `publicDir`: two hand-written HTML pages
 * (`/` and `/admin/`) served verbatim, with three custom plugins to make Vite's
 * dev server behave like nginx. All three are gone, and so is the reason for
 * them:
 *
 *   serveComponentFromSource   the Lit components are part of the app graph now,
 *                              not a separately-built /assets/vanna-components.js
 *   serveDirectoryIndexes      there is one page; `/admin/` is a client route
 *   servePagesWithReload       the pages are modules, so HMR reaches them
 *
 * `publicDir` survives for what genuinely is static: images, and the favicon.
 * The locale dictionaries moved *out* of it into `src/i18n` -- see the note at
 * the top of src/i18n/en.ts for why a fetched dictionary was a bug factory.
 */

import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Plugin } from 'vite';

// `__dirname` does not exist in an ES module, and package.json now says
// "type": "module" -- which is what lets Vite, Storybook and the config files
// share one module system instead of the previous commonjs/ESM split.
const HERE = path.dirname(fileURLToPath(import.meta.url));

/** The backend, for the dev proxy. Override when it is not on the default port. */
const API_TARGET = process.env.VANNA_API_URL || 'http://127.0.0.1:8000';

/**
 * What nginx proxies, so `npm run dev` and `npm run preview` agree with it.
 *
 * Shared between the two rather than written twice: `preview` serves the real
 * build output and had no proxy at all, so every page it served was one API call
 * away from failing -- which makes it useless for the one thing it is for,
 * checking the production bundle before shipping it.
 */
const API_PROXY = {
  // ws so /api/vanna/v2/chat_websocket completes a handshake here too, not only
  // through nginx.
  '/api': { target: API_TARGET, changeOrigin: true, ws: true },
  '/health': { target: API_TARGET, changeOrigin: true },
  '/ready': { target: API_TARGET, changeOrigin: true },
};

/**
 * The handful of files that genuinely are static.
 *
 * A list rather than a directory, because the directory that used to serve this
 * purpose grew a whole second application inside it and nobody noticed until it
 * was being served alongside the first.
 */
function copyStaticFiles(): Plugin {
  const FILES = ['favicon.svg'];

  return {
    name: 'vanna:static-files',
    apply: 'build',
    generateBundle() {
      for (const name of FILES) {
        const source = path.resolve(HERE, 'public', name);
        if (!fs.existsSync(source)) {
          this.warn(`static file missing: ${name}`);
          continue;
        }
        this.emitFile({ type: 'asset', fileName: name, source: fs.readFileSync(source) });
      }
    },
  };
}

export default defineConfig({
  root: HERE,

  // ## Why publicDir is off
  //
  // `public/` still holds the entire pre-rewrite application: `index.html` and
  // `assets/app.js` (the old workspace) and `admin/index.html` with
  // `assets/console.js` (the old console), plus their stylesheets and
  // dictionaries. publicDir copies its contents into dist/ *verbatim*, so the
  // build was shipping two complete applications -- and serving both:
  //
  //   /            the React app        (dist/index.html)
  //   /admin/      the old console      (dist/admin/index.html)
  //   /assets/app.js  the old workspace, ~112KB nobody loads
  //
  // Two consoles against one API is a bookmark landing on whichever one it was
  // saved from, and two of them writing the same grants. Worse, `public/
  // index.html` and the built React `index.html` collide on the same output
  // path, so which application answered `/` came down to write ordering.
  //
  // The files stay in the tree -- eight backend tests read them as text, and
  // deleting those is a separate decision -- but they stop being shipped.
  //
  // `tile-figure.js` does not need copying: it is imported from `src/lib/
  // tile-figure.ts`, so Vite bundles it into a hashed chunk. The verbatim copy
  // was served and never requested.
  publicDir: false,

  plugins: [react(), tailwindcss(), copyStaticFiles()],

  resolve: {
    alias: { '@': path.resolve(HERE, 'src') },
  },

  define: {
    __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
    // package.json is the single source of truth for the version now that there
    // is no Python package to sync it from.
    __BUILD_VERSION__: JSON.stringify(process.env.npm_package_version || '0.0.0'),
  },

  // Single page. The router owns every path, and nginx's
  // `try_files $uri $uri/ /index.html` already agrees -- with a regex block above
  // it that 404s anything looking like a missing asset, so a stray request for a
  // file is not answered with the HTML shell at status 200.
  appType: 'spa',

  server: {
    port: 3000,
    strictPort: true,
    proxy: API_PROXY,
  },

  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Hashed names, finally. The previous build kept stable filenames
    // (app.js, console.js, vanna-components.js) which forced nginx to serve
    // /assets/ as `no-cache` and revalidate every file on every load -- because
    // `immutable` on a stable name makes one bad deploy permanent for everybody
    // who loaded it. Hashed names make `immutable` safe and correct.
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
        manualChunks: {
          // Plotly is ~4.9MB unminified and dwarfs everything else. Split so a
          // change to a page does not invalidate it in every cache.
          plotly: ['plotly.js-dist-min'],
        },
      },
    },
    // lit and plotly are bundled in deliberately: the container serves the code
    // in this repository and nothing from a CDN.
  },

  preview: {
    port: 9876,
    strictPort: true,
    proxy: API_PROXY,
  },
});
