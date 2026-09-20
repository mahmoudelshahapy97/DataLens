# DataLens — multi-tenant natural-language querying

Ask a question in English or Arabic, get SQL, results, a chart and a summary — with
each workspace bound to its own database, its own members, and its own curated
knowledge.

```
┌──────────── browser ────────────┐
│  workspace page  ·  console     │   one origin; nginx proxies /api
└────────────────┬────────────────┘
                 │  session cookie (httpOnly) + X-Tenant-Id
┌────────────────▼────────────────────────────────────────────────┐
│  API  (FastAPI, 4 workers)                                      │
│                                                                 │
│   identity ─► authz ─► routes ─► per-workspace Agent            │
│                                    │                            │
│                                    ├─ tool registry             │
│                                    │    SQL policy, semantic    │
│                                    │    compile, row/column     │
│                                    │    rules                   │
│                                    └─ SQL runner (read-only)    │
└───────┬─────────────────────────────────────────┬───────────────┘
        │ control plane                           │ data plane
┌───────▼───────────────────┐        ┌────────────▼────────────────┐
│ PostgreSQL  (vanna_app)   │        │ Customer warehouses          │
│  tenants, members, roles  │        │  one connection per          │
│  sessions, API tokens     │        │  workspace, read-only        │
│  plans, payments          │        │  unless explicitly granted   │
│  generations, audit       │        └──────────────────────────────┘
│  shared counters          │
└───────────────────────────┘
```

**The two planes never mix.** The control plane holds our bookkeeping and needs
write access; the data plane is the customer's warehouse and is opened read-only.
Putting our tables in their database would require write credentials on their data.

---

## Quick start

Three ways to run it. Nothing is installed as a package in any of them.

### 1. Docker — the whole stack

```bash
cp .env.example .env          # every value has a working default
make secret                   # generate VANNA_SECRET_KEY, paste it into .env
docker compose up --build     # or: make up
make password                 # the generated first-run admin password
```

Open <http://localhost:3000> and sign in as `demo@example.com`. nginx serves the UI
and proxies `/api` to the backend, so the browser sees one origin.

### 2. Backend on its own — uvicorn against a virtual environment

```powershell
python -m venv .venv                       # once, at the repository root

cd backend
..\.venv\Scripts\activate                 # Windows
# source ../.venv/bin/activate            # macOS / Linux
pip install -r requirements.txt

uvicorn main:app --reload --port 8000
```

`http://127.0.0.1:8000/docs` for the API, `/health` and `/ready` for the probes.
No `pip install -e .` and no `PYTHONPATH`: `vanna` and `vanna_app` sit next to
`main.py`, so running from `backend/` is enough. The image runs the same
application as `uvicorn vanna_app.wiring:application --factory`; `main:app` is the
short form for a laptop.

### 3. Frontend on its own — the Vite dev server

```bash
cd frontend
npm install                   # or: npm ci
npm run dev                   # http://localhost:3000
```

It proxies `/api`, `/health` and `/ready` to `127.0.0.1:8000`, so start the backend
first. Editing anything under `public/` or `src/` reloads the page — a full reload
rather than a hot patch, which is the right trade here: these pages keep their state
on the server and in an httpOnly cookie, so there is nothing worth preserving across
an edit.

The component needs no build step while developing. `/assets/vanna-components.js`
is mapped onto `src/index.ts` and compiled on demand, so the URL in the HTML is the
same one nginx serves in the container.

`npm run build` produces `frontend/dist/` — the pages, their assets and the bundled
component — which is exactly what the frontend image serves.

The stack needs a PostgreSQL it can reach. The bundled compose file joins the
sandbox project's network (`../../databases`), which must be running first — it
owns the network and the eight seeded datasets.

Without an `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` the stack still runs, using a
mock LLM: enough to exercise streaming, the console and the admin flows, not enough
to write real SQL.

---

## Deployment modes

The single most important setting. It is explicit rather than inferred, because a
mode inferred from which variables happen to be set is a mode nobody decided on.

| | `demo` | `single-tenant` | `multi-tenant` |
|---|---|---|---|
| Anonymous access | yes | opt-in | **refused** |
| Control plane required | no | yes* | **yes** |
| Platform admins required | no | no | **yes** |
| `VANNA_SECURE_COOKIES` | any | any | **must be true** |
| `VANNA_TRUST_HEADERS` | any | any | **refused** |
| Public member roster | any | any | **refused** |
| `VANNA_SECRET_KEY` | optional | required* | **required** |
| Quota / rate limits | per process | shared | shared |
| CSRF protection | off | on | on |

\* unless anonymous access is explicitly chosen.

**In `multi-tenant`, a dangerous configuration does not start.** `ConfigError` lists
every problem at once, so a deployment is fixed in one pass rather than discovering
the next fault on each restart. This is deliberate and it is the difference between
this and its predecessor: almost every serious weakness in the original was a
permissive default that nobody chose, documented in a comment beside the code that
did it. A comment does not stop a deployment.

---

## Roles

Two tiers, because "admin" means two different things in a multi-tenant system.

**Platform admin** — an address in `VANNA_ADMIN_EMAILS`. Creates and deletes
workspaces, binds datasources, grants write access, sets plans, records payments,
administers any workspace.

**Workspace roles** — a row in `tenant_users`:

| | read | save queries, dashboards | manage members, starters, knowledge |
|---|---|---|---|
| `viewer` | ✓ | | |
| `analyst` | ✓ | ✓ | |
| `admin` | ✓ | ✓ | ✓ |

A workspace admin cannot change their own plan, grant their own workspace write
access, or repoint it at another database. Those are platform decisions.

Refusals are `404`, never `403`: a `403` confirms the resource exists to somebody
who has no business knowing that it does.

---

## Configuration

Every variable, its default, and what it does. Anything not listed here is not read.

### Mode and secrets

| Variable | Default | |
|---|---|---|
| `VANNA_DEPLOYMENT_MODE` | `demo` | `demo` · `single-tenant` · `multi-tenant`. An unrecognised value is treated as `multi-tenant`, so a typo fails closed. |
| `VANNA_SECRET_KEY` | — | Encrypts stored datasource credentials, signs CSRF and OIDC state. `make secret` generates one. Changing it makes existing stored credentials unreadable. |
| `VANNA_ADMIN_EMAILS` | — | Comma-separated platform admins. **Empty means nobody**, not everybody. |

### Databases

| Variable | Default | |
|---|---|---|
| `VANNA_APP_DATABASE_URL` | — | The control plane. Created on first start if absent. |
| `VANNA_DATABASE_URL` | — | Fallback data source for workspaces with no binding. Empty means the built-in SQLite demo. |
| `VANNA_AUTO_MIGRATE` | `true` | Apply migrations at boot. Set `false` and run them as a deploy job once more than one replica starts at a time. |
| `VANNA_APP_POOL_MIN` / `_MAX` | `2` / `16` | Control-plane pool. Callers wait for a connection rather than erroring. |
| `VANNA_APP_POOL_WAIT_SECONDS` | `10` | How long, before reporting saturation. |

### Authentication

| Variable | Default | |
|---|---|---|
| `VANNA_AUTH_METHODS` | `password` | `password`, `oidc`, or both. |
| `VANNA_SESSION_TTL_HOURS` | `72` | |
| `VANNA_SECURE_COOKIES` | `false` | Must be `true` behind TLS, and is required in `multi-tenant`. |
| `VANNA_TRUSTED_PROXIES` | — | CIDRs whose `X-Forwarded-For` may be believed. Empty means the header is ignored entirely. |
| `VANNA_TRUST_HEADERS` | `false` | Accept `X-User-Email` as identity. Only behind a gateway that authenticates and strips it. |
| `VANNA_ALLOW_ANONYMOUS` | `false` | No authentication at all. |
| `VANNA_ADMIN_PASSWORD` | — | First-run admin password. Unset generates one and logs it once. |
| `VANNA_LOGIN_MAX_ATTEMPTS` | `8` | Failed sign-ins per address and per IP… |
| `VANNA_LOGIN_WINDOW_SECONDS` | `300` | …within this window. Shared across workers. |
| `VANNA_PUBLIC_USER_DIRECTORY` | `false` | Publish a workspace's member list to the sign-in screen. A demo convenience and an email-address disclosure anywhere else. |

### Single sign-on

| Variable | |
|---|---|
| `VANNA_OIDC_ISSUER` | Discovery is read from `<issuer>/.well-known/openid-configuration`. |
| `VANNA_OIDC_CLIENT_ID` / `_SECRET` | |
| `VANNA_OIDC_SCOPES` | Default `openid email profile`. |
| `VANNA_OIDC_ROLE_CLAIM` | Claim to map onto a workspace role. Optional. |
| `VANNA_OIDC_AUTO_PROVISION_TENANT` | Workspace new SSO identities join. Off by default: joining a workspace is a decision. |

### Mail

| Variable | |
|---|---|
| `VANNA_SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` / `_FROM` | With no host, messages are written to the log instead of sent — so the reset and invitation flows work in development. |
| `VANNA_PUBLIC_BASE_URL` | Where links in emails point. |

### Limits

| Variable | Default | |
|---|---|---|
| `VANNA_DAILY_QUOTA` | `200` | Questions per workspace per rolling day. Overridden by the plan. |
| `VANNA_RATE_LIMIT_PER_MIN` | `20` | Per user. |
| `VANNA_MAX_ROWS` | `1000` | Row cap per query. |
| `VANNA_QUERY_TIMEOUT` | `60` | Seconds. |
| `VANNA_MAX_TENANT_RUNTIMES` | `2` | Cached agents, one per **(workspace, database)** pair; each holds a connection pool. Raise it if you register several databases: two workspaces with two databases each need four, and below that every question evicts a runtime and pays to rebuild it -- reconnecting, rebuilding the semantic layer and re-reading the catalog. The ceiling is what your warehouse permits: `workers x (APP_POOL_MAX + MAX_TENANT_RUNTIMES x WAREHOUSE_POOL_MAX)`. |
| `VANNA_TENANT_RUNTIME_TTL_SECONDS` | `1800` | Idle eviction. |
| `VANNA_GENERATION_RETENTION_DAYS` | `365` | Question text is customer data. `0` keeps it forever. |
| `VANNA_ALLOW_WRITES` | `false` | Master switch. A workspace also needs `allow_writes`, *and* the caller must be an admin of it. |

### Answer checking

Two optional steps in the agent's turn. **Both cost model calls, and those
calls are metered and billed like any other**, so they are settings rather than
constants.

| Variable | Default | |
|---|---|---|
| `VANNA_ENABLE_CRITIC` | `true` | Check each answer against the question before the user sees it. Runs only on a turn that actually queried, so a greeting or a schema question costs nothing extra. Budget roughly one extra call per data question. |
| `VANNA_MAX_CRITIC_RETRIES` | `1` | How many times the critic may send a turn back. Each rejection costs a further full turn. `0` disables the critic. |
| `VANNA_ENABLE_PLANNER` | `false` | Draft an approach before answering a multi-step question, and show it. Fires on questions containing words like "compare", "trend" or "why". Off because, unlike the critic, there is no cheap signal for when it earns its call. |

### Everything else

| Variable | Default | |
|---|---|---|
| `VANNA_LLM_PROVIDER` | `auto` | `auto` · `anthropic` · `openai` · `mock`. |
| `VANNA_INDEX_BACKEND` | `lexical` | Dependency-free BM25, or a vector integration fused in via RRF. |
| `VANNA_PROJECT_DIR` / `VANNA_PROJECTS_DIR` | | Semantic manifests on disk. One manifest describes one database, so it binds to one workspace. Not read when `VANNA_CONFIG_SOURCE=database`. |
| `VANNA_CONFIG_SOURCE` | `disk` | `disk` reads the YAML tree; `database` reads the `config_files` catalog. See below. |
| `VANNA_CONFIG_REFRESH_SECONDS` | `5` | How long a worker may serve cached configuration. Each of the four workers has its own cache. |
| `VANNA_CONFIG_BOOTSTRAP` | `false` | Import the shipped files at boot, into an **empty** catalog only. |
| `VANNA_CORS_ORIGINS` | `http://localhost:3000` | Explicit list; `*` is refused. |
| `LOG_LEVEL` / `VANNA_LOG_FORMAT` | `INFO` / `text` | `json` for a log shipper. |
| `VANNA_METRICS_ENABLED` | `true` | Prometheus at `/metrics`, blocked at the edge. |
| `VANNA_SENTRY_DSN` | — | Optional error reporting. |

### Where configuration lives

Semantic projects, the instruction library and the domain definitions are YAML and
JSON files in this repository. They can also live in the control plane, in
`config_files` — which is what lets an administrator change a cube from the console
instead of rebuilding an image. `docker-compose.yml` runs that way by default.

```
files ──import──▶ config_files ──▶ ConfigStore + cache ──▶ runtime
                       ▲
        console / API ─┘
```

With `VANNA_CONFIG_SOURCE=database`, **disk is never consulted** — not as a
fallback, not when a row is missing. A workspace whose project is in the catalog
but whose manifest is not is an error, not a quiet reversion to whatever the image
shipped with. Switching back is one variable.

```bash
python backend/tools/seed_database.py                       # migrate, import, regenerate database/
python backend/tools/import_config_files.py --dry-run       # what would change
python backend/tools/export_sql_schema.py                   # sql_schema.sql + seed/
```

An edit through `PUT /api/vanna/v2/admin/config/file` (platform admin) is validated
with the runtime's own types, versioned in `config_versions` with who changed it,
and recompiles `target/mdl.json` in the same transaction — because the runtime
reads the compiled manifest, not the cubes. `database/README.md` has the rest,
including why a full data export refuses to write inside the repository.

---

### Charts

Every tile is a Plotly figure, and there is one piece of code that decides what a
tile looks like: `frontend/public/assets/shared/tile-figure.js`. It turns a tile
and its result into `{ traces, layout, config }` — a chart for a chart tile, an
`indicator` for a metric, a `table` trace for rows. Text tiles are the exception;
prose is markup.

The dashboard page imports that module. An **exported** dashboard *inlines* it,
along with Plotly itself, so the file works from a `file://` path with no network:

```
export.html   ~1.3 MB
├── plotly-export.min.js   1.2 MB, six trace types (bar, pie, scatter,
│                          heatmap, indicator, table) rather than the 4.9 MB
│                          full distribution
├── tile-figure.js         the page's own figure builder
└── <script type="application/json">   the rows
```

Sharing the builder is the point. The export used to draw its own SVG
approximations with their own axis-picking rules — it ignored `sort_by`, `limit`
and `color_by`, and drew a bar chart when the tile asked for a heatmap — so the
file somebody circulated showed a different chart than the screen it came from.

**Interactivity needs Plotly's own stylesheet inside the shadow root.**
`<plotly-chart>` renders into a shadow root, Plotly injects its CSS into
`document.head`, and a shadow root does not inherit document styles — so the rule
`.main-svg { pointer-events: none }` is missing and the topmost overlay swallows
every pointer event. The chart draws correctly and the modebar buttons still work,
so it reads as a styling quirk rather than a dead plot. `_adoptPlotlyStyles` copies
the rules in, and it has to read them from `sheet.cssRules`: Plotly builds the
sheet with `insertRule`, so the `<style>` element's `textContent` is empty and
cloning the node copies nothing.

Rebuild the bundle with `make plotly-bundle` (needs Docker; the backend image has
no Node in it, so the output is committed). `backend/tests/test_dashboard_export.py`
fails if the vendored copy drifts from the frontend one, and
`backend/tests/test_dashboard_export_ui.py` opens a real export in a real browser with
the network switched off. `backend/tests/e2e/test_charts_in_browser.py` hovers a bar and
drags to zoom on the running stack — the only place a dead plot shows up.

---

## Operating it

```bash
make migrate-status    # schema version, anything pending
make migrate           # apply
make seal-secrets      # encrypt datasource credentials written before encryption
make logs              # follow the API
```

**Probes.** `/health` is liveness and deliberately does not touch the database — a
liveness probe that queries the warehouse turns a slow database into a restart loop.
`/ready` is readiness and *does* check the control plane, because a replica whose
control plane is unreachable cannot sign anybody in and must not receive traffic.

**Scaling.** The API runs four workers. That is possible only because quota, rate
limiting and login throttling live in the control plane; when they were per-process
dictionaries, two workers enforcing "200 per day" independently permitted 400. A
second *host* additionally needs shared storage for `VANNA_KNOWLEDGE_DIR`.

**Backups.** The control-plane database is the only stateful thing that cannot be
rebuilt. The catalog rescans, the vector index reindexes from the markdown, and the
demo SQLite regenerates.

See [docs/operations.md](docs/operations.md) for the runbook and
[docs/security.md](docs/security.md) for the threat model.

---

## Development

```bash
make check                 # lint + unit tests, no database needed
make test-integration      # needs PostgreSQL; see DB_URL in the Makefile
make test-all
```

`backend/tests/test_tenant_isolation.py` is the file to read first. It drives the real
application and asserts, for every route that names a workspace, that a member of
another workspace gets a 404 — the property the product is sold on, and the one
there was previously no automated proof of.

**Domain knowledge: three mechanisms, and which owns what**

Getting this wrong is how a workspace ends up with two rules that contradict each
other, so the ownership is worth stating plainly.

| | lives in | scope | applied by |
|---|---|---|---|
| **Platform baseline** | `backend/instructions/baseline.yml` | every workspace, always | merged at read time; never copied into a tenant |
| **Workspace rules** | `backend/domains/domains.yml` | one workspace | `make provision` |
| **Starter library** | `backend/instructions/packs/*.yml` | opt-in, any workspace | `make enable-packs`, or the console |
| **Starter questions** | `backend/domains/domains.yml` | one workspace | `make provision` |

A **workspace rule** names real tables and columns — that is what earns it a place in
`domains.yml`. A **pack** names none, which is what makes it reusable: `data-hygiene`
and `banking-conventions` suit any schema, which is why they are offered rather than
imposed. If a rule you are about to write names no table, it belongs in a pack.

```bash
make provision-list                       # what the file says, without applying it
make provision                            # idempotent; run it twice, the second adds 0
make enable-packs  E2E_PASSWORD=...       # one pack per workspace
```

Two sharp edges, both of which have already caused bugs here:

**Provisioning cannot correct a rule.** It deduplicates on exact text, so *editing* a
rule's wording adds a second rule and leaves the original live in every database
already provisioned. Append new rules; to withdraw an old one, comment it out in the
file *and* switch the stored row off — `make retire-wrong-rules` does exactly that for
the eight rules that named columns their database does not have. Disabling rather than
deleting is deliberate: the stored text is what the deduplication recognises, so a
deleted row comes straight back on the next `provision`.

**No pack may be enabled on two workspaces.** Copied pack rules are byte-identical
wherever they land, and `backend/tests/e2e/test_domains_in_browser.py` asserts that any text
two workspaces share is a *platform* rule. A pack on both trips that test with a
message about content, pointing nowhere near pack enablement. The mapping in
`backend/tools/enable_domain_packs.py` gives each pack one workspace and leaves `pagila` and
`world` — the pair that test compares — with none.

`backend/tests/test_domain_content.py` enforces all of this offline, in about three seconds.
It exists because a malformed pack is otherwise discovered as a container that will
not boot: `InstructionLibrary.load()` runs during startup, and nothing else loaded the
shipped files.

**In a real browser**

Everything above drives the application through ASGI, which verifies behaviour but
not *delivery*. A page can pass every server-side test and still be dead: a strict
CSP blocking your own bundle, a module served under a MIME type the browser refuses
to execute, a layout that has nowhere to go at 390px. Only a browser sees those.

```bash
docker compose up -d
make password                                  # the admin password

make seed          E2E_PASSWORD=...            # fill the lists with real content
make seed-questions E2E_PASSWORD=...           # real Q&A history (slow, LLM calls)
make screenshots   E2E_PASSWORD=...            # every screen into ./artifacts
make test-e2e      E2E_PASSWORD=...            # the whole browser suite
make qa-json       E2E_PASSWORD=...            # export the Q&A history to qa.json
```

`make seed` is worth running first and is not only cosmetic. A fresh install renders
its empty state everywhere, so a screenshot run photographs a dozen variations on
"no data yet" — and an empty list is indistinguishable from a fetch that quietly
failed. It asks the application, over its own API, to do what an analyst would: run
questions, keep the good ones, assemble them into dashboards. Nothing is written
behind the app's back.

`make seed-questions` exists separately because it is slow. History rows carry a
question only when one was *asked*: the text is captured by a lifecycle hook on the
agent's `before_message`, so anything that posts SQL directly — including the app's
own Run SQL button — records the statement and leaves the question blank. Real Q&A
means real chat turns, at an LLM call each.

`make qa-json` writes one record per exchange — `question`, `answer`, `query`,
`database` — joining the generation store (which has the SQL) to the conversation
store (which has the prose the user read). No single endpoint holds all four. Rows
with no question are skipped rather than exported blank: they came from `run-sql`,
which never runs the hook that captures a question.

It spans every workspace by default (`QA_TENANT=all`), so one file covers all eight
seeded databases — the music store, the wholesaler, the DVD rental chain, the world
atlas, the HR system, the clinic, the shop and the hotel. `backend/tools/ask_demo_questions.py`
keeps a separate bank of questions per workspace for the same reason: a question
about invoices means nothing to the world atlas.

**Write questions** are a separate bank, asked with `make seed-writes`, because they
go through `propose_write`/`confirm_write` rather than `run_sql`. Most of them are
*meant* to be refused — over the row cap, against an ungranted table, or not
expressible as a plan at all — and refusals need no grants, so that half is safe to
run anywhere. For the rest, `make grant-writes` grants DML on three small tables
(`genre`, `playlist`, and `customer` for UPDATE only); `make revoke-writes` undoes it.
The ledger — `invoice`, `invoice_line`, `track` — stays read-only on purpose.

Two things to know before relying on write Q&A. Writes need `VANNA_ALLOW_WRITES=true`
*and* the workspace's own `allow_writes`, and they record no generation, so the
history screen never shows them. And the export cannot currently fill `query` for
them: `GET /api/vanna/v2/writes` returns only the second-person review queue and
deliberately omits `conversation_id`, so there is no way to join an executed write
back to the question that caused it. Exporting writes properly needs that join
exposed — the statement is stored, in `pending_writes.statement_preview`; it is just
not reachable through the API.

**Per-role screenshots.** `make screenshots` photographs the app as the platform
admin, who can see everything -- which is the least informative view of a product sold
on different people seeing different things. `make role-accounts` then
`make role-screenshots` captures four more:

```
artifacts/admin/      workspace admin  -- the console, tab by tab
artifacts/analysis/   analyst          -- can save work, no console
artifacts/viewer/     viewer           -- reads everything, writes nothing
artifacts/user/       outsider         -- a real user of another workspace
```

`backend/tests/e2e/test_roles.py` also asserts the boundary each role sits behind, because a
screenshot proves a page rendered and cannot prove anybody was refused. The three
refusals are deliberately different: a viewer writing gets **403** naming their role,
a member who is not an admin gets **404** from the admin routes, and an outsider is
refused everything. One stated property does not currently hold -- see the strict
`xfail` at the bottom of that file.

`make screenshots` writes `artifacts/`, which is **tracked**, by request: the point
is to be able to see the evidence without running the suite against a live stack.
The images are derived files and every re-record rewrites them, so each run adds its
own few megabytes to history -- see the note on the rule in `.gitignore`.
Pass `E2E_TENANT` to point the seeders at a different workspace — this account
belongs to nine, each bound to its own warehouse, and the session default is not
necessarily the one your browser has open.

**Layout**

Three directories, one per thing you can run.

```
backend/              Python. Nothing here is pip-installed; uvicorn imports it.
  main.py               uvicorn main:app --reload
  vanna_app/            the application: control plane, accounts, billing, routes
    config.py             every environment variable, validated once, at startup
    authz.py              who may do what -- one implementation, used everywhere
    identity.py           who is calling; the resolver every route shares
    platform.py           the per-workspace agent cache
    migrate.py            the migration runner; reads ../database/migrations/
    routes/               the HTTP surface, one module per area
  vanna/                the library: agent, tools, semantic layer, integrations
    core/                 interfaces and the agent loop
    capabilities/         schema catalog, retrieval index, sql runner, knowledge
    semantic/             manifests, the compiler, cube rewriting
    integrations/         one package per LLM, vector store and warehouse
    dashboards/           render and export; vendor/ holds the browser code
  instructions/         the platform instruction baseline and starter packs
  domains/domains.yml   one workspace per seeded database
  projects/             semantic manifests, one per database (chinook, acme)
  evals/                the LLM comparison harness
  tests/                unit (no database) and integration (marked)
    e2e/                  the same product in a real browser. Needs VANNA_E2E_URL.
      test_screenshots.py    photographs every screen into artifacts/
  tools/                standalone scripts. Not imported by the app.
    plotly_export_bundle/ the Plotly build embedded in exported dashboards
  requirements.in       hand-edited; the source of truth for dependencies
  requirements.txt      pinned; generated from requirements.in by `make lock`

frontend/             Node. One Vite project.
  public/               served verbatim, at the URLs it references
    index.html            the workspace page
    admin/index.html      the operator console
    assets/app.js         and console.js, and shared/core.js -- escaping, fetch,
                          CSRF, accessibility, in one copy
    locales/              interface translations, fetched at runtime
  src/                  the <vanna-chat> element, TypeScript, bundled
  nginx.conf            serves the above and proxies /api to the backend

database/             SQL.
  migrations/           numbered, applied under an advisory lock. Source of truth.
  sql_schema.sql        generated: every applied migration, concatenated
  seed/configuration.sql  generated: the configuration catalog

docs/                 operations.md (the runbook), security.md (the threat model)
artifacts/            screenshots and video, tracked deliberately
```

`tests/` and `tools/` live under `backend/` because that is what they mostly are:
the suite imports `vanna` and `vanna_app`, and the scripts either import them too or
drive the running app. Both still reach up into `../../frontend/` -- the UI tests
read `frontend/public/` from disk, and `mock_component_backend.py` serves
`frontend/dist/assets` -- so the three directories are cleanly separated, not
independent. `pytest` is run from the repository root, where `pyproject.toml` puts
`backend/` on `sys.path` and points `testpaths` at `backend/tests`.

**There is no package.** `backend/vanna/` and `backend/vanna_app/` are imported
from source. That is the whole reason `backend/` is flat: both are top-level
packages, so the working directory is the only thing on `sys.path` that matters,
and `pip install -e .` is not a step anybody has to remember. The version lives in
`backend/vanna/__init__.py`, and the application's own in
`backend/vanna_app/__init__.py` -- deliberately two numbers, because the library and
the deployment of it are versioned separately. The developer command line that used
to ship as `python -m vanna` has been removed; nothing in the deployed application
imported it.

---

## Licence

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Derived from
[vanna-ai/vanna](https://github.com/vanna-ai/vanna).
