# Vanna frontend — builds the UI, serves it through nginx.
#
# Build context is `frontend/` itself, so this image is self-contained: nothing
# outside this directory is needed to produce it. That was not true before, when
# the build reached up to the repository root for the Python package version.
#
# nginx also reverse-proxies /api to the backend container, which means the browser
# sees a single origin. That is worth more than it looks: same-origin removes CORS
# from the picture entirely and lets session cookies flow to the API without
# SameSite complications. `npm run dev` proxies the same three prefixes, so the two
# environments agree.

# ---------------------------------------------------------------- builder ---
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS builder

WORKDIR /build

# Dependency layer first, so source edits do not trigger a reinstall.
COPY package.json package-lock.json* ./

# `npm ci` needs a lockfile and is reproducible; fall back to `install` when the
# lockfile is absent so a fresh checkout still builds.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# tsconfig, the Vite config and the single HTML entry before the sources they
# describe. `index.html` is the Vite entry now rather than a page served verbatim
# out of publicDir, so it has to arrive before the build reads it -- and it is the
# one file whose absence produces an empty dist/ with a successful exit code.
COPY tsconfig.json vite.config.ts index.html ./
COPY src/ ./src/
COPY public/ ./public/

# `tsc` runs before bundling, so a type error fails the image build rather than
# shipping broken JavaScript to users.
RUN npm run build

# ---------------------------------------------------------------- runtime ---
FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10 AS runtime

# The whole site: index.html plus /assets/, which now holds hash-named bundles
# rather than the old fixed app.js / console.js / vanna-components.js. The
# translation dictionaries are no longer here at all -- they are modules inside
# the bundle; see the note at the top of src/i18n/en.ts.
COPY --from=builder /build/dist/ /usr/share/nginx/html/

COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
