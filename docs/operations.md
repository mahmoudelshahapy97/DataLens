# Operations

The runbook. Written for whoever is on call, which may be somebody who has never
read the source.

## What is stateful

| | Rebuildable? | |
|---|---|---|
| Control-plane database | **No** | Tenants, members, sessions, plans, payments, history, audit, grants, **business rules**. Back this up. |
| `VANNA_KNOWLEDGE_DIR` | **No** | Curated golden examples, as markdown. Back this up, or keep it in git. |
| Catalog (`$VANNA_DATA_DIR/catalog.json`) | Yes | Rescans. |
| Qdrant volume | Yes | Reindexes from the markdown. |
| Demo SQLite | Yes | Regenerates. |

Backing up the first two is the whole backup story.

```bash
pg_dump "$VANNA_APP_DATABASE_URL" --format=custom --file=vanna-$(date +%F).dump
```

Restore into an empty database and start the API; migrations bring the schema to the
running version.

**`VANNA_SECRET_KEY` must be backed up with it.** Datasource credentials are sealed
with a key derived from it. A restore with a different key leaves every workspace
bound to a connection nobody can open, and the only remedy is re-entering each
connection by hand.

## Deploying

```bash
make migrate-status     # what is pending
make up                 # build and start
make logs               # watch it come up
curl -fsS localhost:3000/ready
```

With one replica, `VANNA_AUTO_MIGRATE=true` is fine — migrations run at boot under
an advisory lock, so even concurrent starts are safe. With several, prefer a
migration job:

```bash
VANNA_AUTO_MIGRATE=false
python -m vanna_app.migrate upgrade   # as a deploy step, before the rollout
```

### Adding a migration

1. `database/migrations/NNNN_short_name.sql`, next number.
2. Assume the state the previous migration left. Only `0001` is defensively
   idempotent, because it had to adopt databases that predate migrations.
3. Each file runs in its own transaction with its ledger insert, so a failure leaves
   the schema untouched. PostgreSQL has transactional DDL; use it.
4. `make test-integration` — `test_migrations.py` applies every migration to a fresh
   database on every run.

## Probes

| | Checks | Use for |
|---|---|---|
| `/health` | Nothing external | Liveness. A liveness probe that queries the database turns a slow database into a restart loop. |
| `/ready` | Control plane reachable, migrations applied | Readiness. A replica whose control plane is down cannot sign anybody in. |

## Monitoring

`/metrics` is Prometheus, and is blocked at the nginx edge — scrape the API
container directly.

Worth alerting on:

| Metric | Why |
|---|---|
| `vanna_http_requests_total{status=~"5.."}` | The obvious one. |
| `vanna_control_plane_pool_waiters` | Sustained non-zero means `VANNA_APP_POOL_MAX` is too small, or something is holding a connection. |
| `vanna_limit_rejections_total` | A spike is either an attack or a customer who has outgrown their plan. Both want a human. |
| `vanna_llm_cost_usd_total` | The bill, per workspace and model, before it arrives. |
| `vanna_login_failures_total{reason="throttled"}` | Credential stuffing. |
| `vanna_tenant_runtimes` | Cached (workspace, database) pairs. Pinned at `VANNA_MAX_TENANT_RUNTIMES` means eviction thrash; raise it. |

Set `VANNA_LOG_FORMAT=json` for a shipper. Every line carries `request_id`,
`tenant_id` and `user_id`, so one person's afternoon is a field match rather than an
archaeology exercise. The id is also returned as `X-Request-Id`, so a user's bug
report can name it.

## Common situations

### "The API will not start"

Read the first lines of the log. `ConfigError` lists *every* problem at once and
names the variable for each. It is refusing on purpose — see the deployment-mode
table in the README.

### "Everybody is locked out"

Check `VANNA_TRUSTED_PROXIES`. If it is empty or wrong, every request appears to
come from the reverse proxy, so the per-IP login throttle spends one shared budget
and locks out all users at once. The compose default is `172.16.0.0/12`.

To clear it immediately:

```sql
DELETE FROM vanna_app.counters WHERE bucket_key LIKE 'login:%';
```

### "Nobody can sign in and there are no platform admins"

```bash
docker compose exec backend python - <<'PY'
import asyncio
from vanna_app.accounts import Accounts
from vanna_app.config import get_settings
from vanna_app.db import build_app_database
from vanna.core.auth import generate_password

settings = get_settings()
accounts = Accounts(build_app_database(settings))
password = generate_password()
asyncio.run(accounts.create("you@example.com", password, must_change=True))
print("password:", password)
PY
```

Then add the address to `VANNA_ADMIN_EMAILS` and restart.

### "A workspace is querying the wrong database"

Repointing a workspace clears its cached runtime *and* its catalog, so the next
question rescans. If the agent still describes the old tables, the catalog did not
clear:

```bash
docker compose exec backend rm /data/catalog.json
docker compose restart backend
```

### "Answers got worse after a deploy"

Check `index_backend` on `/api/vanna/v2/schema`. A configured-but-unavailable vector
index degrades to keyword-only BM25, which is invisible from the outside — that
field exists so the degradation is visible rather than silent.

### "The control plane is saturated"

`ControlPlaneUnavailable` with "all N connections were busy" means requests waited
longer than `VANNA_APP_POOL_WAIT_SECONDS`. Raise `VANNA_APP_POOL_MAX` (PostgreSQL's
own `max_connections` is the ceiling), then look for a slow query:

```sql
SELECT pid, now() - query_start AS age, left(query, 120)
  FROM pg_stat_activity
 WHERE datname = 'vanna_app' AND state <> 'idle'
 ORDER BY age DESC;
```

### "A customer wants their data erased"

Deleting a workspace deliberately leaves `generations` behind, so the record of what
was asked survives. Erasure is separate and explicit:

```
DELETE /api/vanna/v2/admin/tenants/{id}/data?confirm={id}
```

Platform admin, requires the id typed back, removes generations, conversations,
dashboards, saved queries and audit rows. It is recorded in `admin_audit` first.

### "Qdrant will not start after an upgrade"

A newer Qdrant refuses segments written by an older one. The collection is derived,
so throwing it away is cheap:

```bash
docker compose stop qdrant && docker volume rm vanna_qdrant-data
docker compose up -d qdrant && vanna knowledge reindex
```

## Scaling

**Vertically first.** The API runs four workers; raise them before adding hosts.

**Horizontally**, two things need attention:

1. `VANNA_KNOWLEDGE_DIR` still holds golden examples as local files. Two hosts need
   shared storage, or they will disagree about which examples the agent has seen.
   Business rules moved to the control plane in `0008` and no longer care.
2. Set `VANNA_AUTO_MIGRATE=false` and run migrations as a deploy step.

Quota, rate limiting and login throttling are already shared, which is what made
more than one worker possible at all.

**Warehouse connections** are the other ceiling: each cached runtime holds a pool.
`VANNA_MAX_TENANT_RUNTIMES × pool size × replicas` must fit inside what the
customer's warehouse permits.

Read that first factor carefully now that a workspace can register more than one
database. The cache is keyed on the **(workspace, database) pair**, so a workspace
querying three databases occupies three slots and opens three pools. The bound did
not change meaning by accident -- a runtime is bound to one connection, so two
databases genuinely need two of them -- but a deployment sized when the limit
counted workspaces is now sized for fewer workspaces than it was. Multiply by the
average number of databases per workspace, or raise the limit.

## Rotating `VANNA_SECRET_KEY`

There is no online rotation. The sequence:

1. Note every workspace's connection details, `make down`.
2. Set the new key.
3. `make up`, then re-enter each connection through the console.

Plan for it before you need it — this is the least pleasant procedure here, and the
reason the key belongs in a secrets manager from the first day.

## Upgrading past migration 0009

Three changes alter behaviour rather than only the schema. None needs action on a
new deployment; all three matter on one that is already running.

**Business rules move into the database.** They were `knowledge/<tenant>/rules/*.md`
and are now rows. The first time each workspace is used after the upgrade, its
markdown rules are imported once and the workspace is stamped
`tenants.instructions_imported_at`. The files are left where they are — they are
somebody's only copy until they are satisfied it worked.

The stamp is why the import is safe to leave alone. The condition that looks
obvious — import when the table is empty — would resurrect every rule an
administrator had deliberately deleted, on the next restart.

Check it landed:

```sql
SELECT t.id, t.instructions_imported_at, count(i.id) AS rules
FROM vanna_app.tenants t
LEFT JOIN vanna_app.instructions i ON i.tenant_id = t.id
GROUP BY t.id, t.instructions_imported_at ORDER BY t.id;
```

If you provision workspaces from `domains/domains.yml`, re-run
`python -m vanna_app.domains provision` after the upgrade. It writes to the
database now; rules it wrote before the upgrade are picked up by the import.

**The workspace role becomes a group.** A caller's `group_memberships` used to be
`["user"]` for everyone who was not an admin, so a grant written for `analyst` or
`viewer` matched nobody. It is now `["user", "<role>"]`. Consequences, all checked:

- Analysts gain the dashboard authoring tools, which the tool registration always
  claimed they had.
- A grant row for `analyst` or `viewer` becomes live. Nothing wrote one before, so
  this only applies if you created them by hand — the API logs a warning at
  startup naming any it finds.
- If you maintain your own semantic manifest, check whether any
  `row_level_access_controls` entry names a group. Such a rule starts matching.
  None of the shipped projects does.

**Grants can now govern reads.** Off by default and per role. Switching on
`enforce_reads` for a role narrows the schema the model is shown and refuses SQL
naming anything outside it — so apply a preset, or grant some tables, before
enabling it. The API refuses to enable it for a role with nothing granted, and the
Permissions screen says the same thing in front of the checkbox.
