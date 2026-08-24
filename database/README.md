# database/

The schema of the **control plane** — the database Vanna keeps its own bookkeeping
in. Tenants, members, roles, sessions, saved queries, starter questions, the
generation log, the audit trail and the shared counters that make quota and rate
limiting mean the same thing across four uvicorn workers.

This is not the database anybody asks questions of. That one belongs to the
customer, is connected to read-only, and is described by the semantic manifests in
`../backend/projects/`. Keeping our bookkeeping out of it is deliberate: an
analytics connection with write access to our own tables is a much worse thing to
leak than one without.

```
database/
└── migrations/        0001_initial.sql … 0009_grant_defaults.sql
```

## How the schema changes

Numbered `.sql` files, a `schema_migrations` ledger recording which have run, and a
runner that applies the missing ones in order — `../backend/vanna_app/migrate.py`.
No second description of the schema, and no ORM metadata that has to agree with
these files.

Two properties the runner guarantees, both of which matter more than they look:

- **One writer.** Every replica takes a PostgreSQL advisory lock before reading the
  ledger, so exactly one applies and the rest wait and then find nothing to do.
  Without it, two replicas racing on boot means the loser crashes on a duplicate
  object.
- **Atomic per migration.** Each file runs in its own transaction together with its
  ledger insert, so a migration either happened *and* is recorded, or neither. A
  half-applied migration recorded as complete is the failure mode that makes people
  stop trusting migrations.

## Running them

At boot, which is the default and is what you want on a laptop:

```
VANNA_AUTO_MIGRATE=true      # docker-compose.yml sets this
```

As a deploy job, which is what you want the moment more than one replica starts at
a time:

```bash
cd backend
python -m vanna_app.migrate status     # what is applied, what is pending
python -m vanna_app.migrate upgrade    # apply the pending ones
```

Both read `VANNA_APP_DATABASE_URL`. The database itself does not need to exist
first — `vanna_app.db.ensure_database` creates it, because `CREATE DATABASE` cannot
run from inside the database being created and asking an operator to do it by hand
is a step to forget.

## Adding one

1. `database/migrations/00NN_short_name.sql` — four digits, then
   `lowercase_with_underscores`. A filename that does not match is a hard error at
   startup, not a skip: a migration silently ignored because of a typo is a schema
   difference nobody finds until it breaks something.
2. Write forward-only SQL. There is no `down`. Reversing a migration in production
   is a new migration, decided with the failure in front of you rather than
   guessed at in advance.
3. Do not renumber an existing file. The version is recorded in the ledger of every
   database that has run it, so changing it means the migration runs twice.

`tests/test_migrations.py` covers discovery, ordering and the lock, and
`tests/test_concurrent_boot.py` covers several replicas booting at once. Both run
without a database except where marked `integration`.

## Where the analytics databases come from

Not from here. `../docker-compose.yml` joins `multidb_network`, an external network
owned by the sibling sandbox project, and reaches its PostgreSQL by container name:

```bash
cd ../../../databases && docker compose up -d    # must be first: it owns the network
cd -                  && docker compose up --build
```

Starting out of order fails immediately with *"network multidb_network declared as
external, but could not be found"*, which is the intended behaviour — the
alternative is Compose quietly creating a second, empty network on which no
database name resolves.

With `VANNA_DATABASE_URL` left empty the stack skips all of that and seeds a small
SQLite file instead (`../backend/vanna_app/demo.py`), so `docker compose up` works
with an empty `.env`.
