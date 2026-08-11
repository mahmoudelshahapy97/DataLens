# Vanna web UI — builds the <vanna-chat> component, serves it and the admin
# console through nginx.
#
# nginx also reverse-proxies /api to the API container, which means the browser
# sees a single origin. That is worth more than it looks: same-origin removes
# CORS from the picture entirely and lets session cookies flow to the API
# without SameSite complications.

# ---------------------------------------------------------------- builder ---
FROM node:20-alpine AS builder

WORKDIR /build

# Dependency layer first, so source edits do not trigger a reinstall.
COPY frontends/webcomponent/package.json frontends/webcomponent/package-lock.json* ./

# `npm ci` needs a lockfile and is reproducible; fall back to `install` when the
# lockfile is absent so a fresh checkout still builds.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY frontends/webcomponent/ ./

# `npm run build` starts with scripts/sync-version.js, which reads the Python
# package version from `../../../pyproject.toml` -- repo root when the
# component sits at `frontends/webcomponent/`. The build context here is the
# component alone, so that resolves to `/pyproject.toml`. Place it there rather
# than skipping the step, so the bundle keeps carrying the real version.
COPY pyproject.toml /pyproject.toml

# The build runs `tsc` before bundling, so a type error fails the image build
# rather than shipping broken JavaScript to users.
RUN npm run build

# ---------------------------------------------------------------- runtime ---
FROM nginx:1.27-alpine AS runtime

# Built web component bundle
COPY --from=builder /build/dist/ /usr/share/nginx/html/assets/

# Chat page and the admin console (plain static files, no build step)
COPY docker/web/index.html /usr/share/nginx/html/index.html
COPY frontends/admin-console/index.html /usr/share/nginx/html/admin/index.html

# Interface translations, fetched at runtime by the page. A missing file here
# does not break the app -- t() falls back to the key -- so the failure mode is
# an interface that quietly reverts to English, which is easy to miss. Keep this
# COPY in step with docker/web/locales/.
COPY docker/web/locales/ /usr/share/nginx/html/locales/

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
