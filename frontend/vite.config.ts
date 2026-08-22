/**
 * One Vite project for the whole UI.
 *
 * The UI is two things with different needs, and this file keeps both honest
 * without a second build pipeline:
 *
 *   public/   The pages. Dependency-free markup, CSS and ES modules, served
 *             verbatim at the URLs they already reference (`/assets/app.js`,
 *             `/locales/en.json`, `/admin/`). Nothing here is bundled, which is
 *             why a CSP without 'unsafe-inline' is possible -- see nginx.conf.
 *   src/      The <vanna-chat> Lit component, TypeScript, bundled to one file at
 *             `/assets/vanna-components.js`.
 *
 * `public/` is Vite's publicDir, and that is load-bearing rather than a default
 * nobody thought about. Files under publicDir are served **as-is**; files under
 * `root` go through the transform pipeline. An earlier version of this config made
 * public/ the root, which meant Vite recognised `app.css` as a CSS *module* and
 * served it as JavaScript that injects a <style> at runtime -- so
 * `<link rel="stylesheet" href="/assets/app.css">` got `Content-Type:
 * text/javascript`, the browser refused it, and every page rendered unstyled while
 * returning 200 for every request. publicDir is what keeps a .css file a .css file.
 *
 * The API is not part of either. /api, /health and /ready proxy to the backend on
 * :8000, so the browser sees one origin in development exactly as it does behind
 * nginx in the container -- no CORS, no configured base URL, and session cookies
 * that just work.
 */

import { defineConfig, type Plugin, type ViteDevServer } from 'vite';
import fs from 'node:fs/promises';
import path from 'node:path';

const HERE = __dirname;
const PUBLIC_DIR = path.resolve(HERE, 'public');

/** The component entry, as a root-relative URL Vite can transform. */
const COMPONENT_MODULE = '/src/index.ts';

/** Where the pages load the component from, in development and in the build. */
const COMPONENT_URL = '/assets/vanna-components.js';

/** The backend, for the dev proxy. Override when it is not on the default port. */
const API_TARGET = process.env.VANNA_API_URL || 'http://127.0.0.1:8000';

/**
 * What nginx proxies, so `npm run dev` and `npm run preview` agree with it.
 *
 * Shared between the two rather than written twice: `preview` serves the real build
 * output and had no proxy at all, so every page it served was one API call away from
 * failing -- which makes it useless for the one thing it is for, checking the
 * production bundle before shipping it.
 */
const API_PROXY = {
  // ws so /api/vanna/v2/chat_websocket completes a handshake here too, not only
  // through nginx.
  '/api': { target: API_TARGET, changeOrigin: true, ws: true },
  '/health': { target: API_TARGET, changeOrigin: true },
  '/ready': { target: API_TARGET, changeOrigin: true },
};

/**
 * Serve the TypeScript component at the URL the built bundle will have.
 *
 * Without this the pages 404 on `/assets/vanna-components.js` in development,
 * because that file only exists after a build -- and the fix people reach for,
 * running `vite build --watch` alongside, gives up HMR and puts build output inside
 * public/. Rewriting the request onto the source keeps one URL in the HTML and the
 * real module graph in development.
 *
 * Middleware added with `server.middlewares.use()` inside configureServer runs
 * *before* Vite's own, which is what this needs: the rewrite has to happen before
 * the publicDir handler gets a chance to 404 on a file that is not there.
 */
function serveComponentFromSource(): Plugin {
  return {
    name: 'vanna:component-from-source',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        if (req.url) {
          const [pathname, query] = req.url.split('?');
          if (pathname === COMPONENT_URL) {
            req.url = COMPONENT_MODULE + (query ? `?${query}` : '');
          }
        }
        next();
      });
    },
  };
}

/**
 * Directory URLs resolve to their index, and `/admin` redirects to `/admin/`.
 *
 * nginx does both (`location = /admin { return 301 /admin/; }` and
 * `try_files $uri $uri/ /admin/index.html`). Doing them here too means a relative
 * link that works in the container works in development, rather than 404ing only
 * for whoever is running `npm run dev`.
 */
function serveDirectoryIndexes(): Plugin {
  /** `/admin` -> `/admin/`. Needed in dev and in preview alike. */
  const redirectBareAdmin = (req: { url?: string }, res: any, next: () => void) => {
    if (!req.url) return next();
    const [pathname, query] = req.url.split('?');
    if (pathname === '/admin') {
      res.statusCode = 301;
      res.setHeader('Location', `/admin/${query ? `?${query}` : ''}`);
      return res.end();
    }
    next();
  };

  return {
    name: 'vanna:directory-index',
    apply: 'serve',

    configureServer(server) {
      server.middlewares.use(redirectBareAdmin);
      // Point a directory request at its index, so the page handler below sees an
      // .html path. Only needed in dev: there the pages live in public/ rather than
      // under root, so Vite's own mpa fallback looks for them in the wrong place.
      server.middlewares.use((req, _res, next) => {
        if (req.url) {
          const [pathname, query] = req.url.split('?');
          if (pathname.endsWith('/')) {
            req.url = `${pathname}index.html${query ? `?${query}` : ''}`;
          }
        }
        next();
      });
    },

    // Preview serves dist/, where the pages *are* at the served root, so the mpa
    // fallback resolves `/` and `/admin/` on its own. The bare `/admin` redirect is
    // the one thing it does not do, and nginx does -- so a link that works in the
    // container should not 404 for whoever is checking the build.
    configurePreviewServer(server) {
      server.middlewares.use(redirectBareAdmin);
    },
  };
}

/**
 * Run the two pages through Vite's HTML transform, and reload on any change under
 * public/.
 *
 * Needed because publicDir files are served verbatim -- which is exactly what keeps
 * `app.css` a stylesheet, and also means Vite never gets to touch the HTML. Two
 * things it does there are load-bearing, and both were missing:
 *
 *   `/@vite/client`  no client means no WebSocket, so nothing reaches the browser:
 *                    `npm run dev` served every file correctly and then sat there
 *                    while you edited. That is most of the reason to run a dev
 *                    server at all.
 *   `define` globals `__BUILD_VERSION__` and `__BUILD_TIME__` are statically
 *                    replaced in the build, but in dev Vite *defines them as
 *                    globals* -- by injecting them into the HTML it transforms.
 *                    Without that injection `src/index.ts` threw
 *                    "__BUILD_VERSION__ is not defined" on every page load. The
 *                    module's imports are hoisted, so the components still
 *                    registered and the page looked fine; only the uncaught
 *                    exception said otherwise, which is why a check watching
 *                    console messages rather than page errors missed it.
 *
 * So this hands the HTML to `server.transformIndexHtml` rather than injecting a
 * script tag by hand: one call, and every HTML transform Vite would normally apply
 * happens, including any a future plugin adds.
 *
 * The reload is full rather than a hot patch, deliberately. These pages hold their
 * state on the server and in an httpOnly cookie, there is no client-side store worth
 * preserving, and a plain reload cannot leave the page in a half-updated state that
 * looks like a bug in the code you just wrote.
 */
function servePagesWithReload(): Plugin {
  const reload = (server: ViteDevServer) => {
    // `server.hot` in Vite 6+, `server.ws` before it. Both exist in 7; prefer the
    // current one so this does not break on the removal.
    const channel = server.hot ?? server.ws;
    channel.send({ type: 'full-reload', path: '*' });
  };

  return {
    name: 'vanna:pages-with-reload',
    apply: 'serve',
    configureServer(server) {
      // public/ is not in the module graph, so its files are not watched for HMR.
      // Excluding .html because Vite watches HTML under root itself -- reloading on
      // it here too sent two events for one edit, which the browser answers with a
      // visible double reload.
      server.watcher.add(PUBLIC_DIR);
      server.watcher.on('change', (file) => {
        const full = path.resolve(file);
        if (full.startsWith(PUBLIC_DIR + path.sep) && !full.endsWith('.html')) {
          reload(server);
        }
      });

      server.middlewares.use(async (req, res, next) => {
        if (!req.url) return next();
        const [pathname] = req.url.split('?');
        if (!pathname.endsWith('.html')) return next();

        // Resolve first, then confirm the result is still inside public/: a request
        // for /../../etc/passwd.html must not escape the document root.
        const file = path.resolve(PUBLIC_DIR, `.${pathname}`);
        if (file !== PUBLIC_DIR && !file.startsWith(PUBLIC_DIR + path.sep)) {
          return next();
        }

        let raw: string;
        try {
          raw = await fs.readFile(file, 'utf8');
        } catch {
          // Missing page: hand it back so the normal 404 happens rather than
          // inventing one here.
          return next();
        }

        try {
          const html = await server.transformIndexHtml(req.url, raw, req.originalUrl);
          res.setHeader('Content-Type', 'text/html;charset=utf-8');
          // Same reasoning as nginx: HTML keeps its URL forever, so a cached copy
          // means every fix looks like it did not ship.
          res.setHeader('Cache-Control', 'no-store');
          res.end(html);
        } catch (err) {
          // A transform failure is a real error and must not fall through to the
          // untransformed file -- that would serve a page missing its define
          // globals and look like a bug in the application.
          next(err);
        }
      });
    },
  };
}

export default defineConfig({
  root: HERE,
  publicDir: PUBLIC_DIR,

  // Multi-page, not single-page. Two real pages at two real URLs, so a directory
  // request resolves to its index and everything else that is missing 404s.
  //
  // Deliberately not 'spa': that rewrites *any* unmatched path to index.html with
  // status 200, which is the failure mode nginx has three separate location blocks
  // to avoid -- a fetch for a missing dictionary resolves to HTML, fails to parse,
  // and the interface silently falls back to rendering raw keys like "nav.account".
  //
  // Deliberately not 'custom' either, which was the previous setting: it disables
  // the fallback entirely, and since the middleware below only applies to `serve`,
  // `npm run preview` answered `/` and `/admin/` with 404. Preview exists to check
  // the real build output, and one that cannot serve its own front page is a trap.
  appType: 'mpa',

  define: {
    __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
    // package.json is the single source of truth for the version now that there is
    // no Python package to sync it from.
    __BUILD_VERSION__: JSON.stringify(process.env.npm_package_version || '0.0.0'),
  },

  // Order matters: the directory rewrite turns `/admin/` into `/admin/index.html`
  // before the page handler looks for an .html suffix.
  plugins: [serveDirectoryIndexes(), servePagesWithReload(), serveComponentFromSource()],

  optimizeDeps: {
    // Pointed at the component sources explicitly. With no HTML entry under root
    // for Vite to crawl, it would otherwise not discover lit or plotly until the
    // first request for a component -- at which point it pre-bundles them and
    // reloads the page under you.
    entries: ['src/**/*.ts'],
  },

  server: {
    port: 3000,
    strictPort: true,
    proxy: API_PROXY,
  },

  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // public/ is copied into dist/, so dist/ is the whole site and the image needs
    // one COPY rather than a list of files that drifts every time one is added.
    copyPublicDir: true,
    lib: {
      entry: path.resolve(HERE, 'src/index.ts'),
      formats: ['es'],
    },
    rollupOptions: {
      output: {
        // lib.fileName cannot carry a directory, and the pages ask for the bundle
        // under /assets/. Say it here instead. Plotly is large enough that Rollup
        // splits it out; the chunk has to land beside the entry for the relative
        // import between them to resolve.
        entryFileNames: 'assets/vanna-components.js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
    // lit and plotly are bundled in deliberately: the container serves the code in
    // this repository and nothing from a CDN.
  },

  preview: {
    port: 9876,
    strictPort: true,
    proxy: API_PROXY,
  },
});
