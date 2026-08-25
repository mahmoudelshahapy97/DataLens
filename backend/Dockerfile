# Vanna backend — FastAPI + uvicorn
#
# Build context is the repository root, not `backend/`: the schema migrations live
# in `database/migrations/` and the image has to carry them, because
# VANNA_AUTO_MIGRATE runs them at boot. Everything else it needs is under
# `backend/`.
#
# Two stages so build tooling (compilers, headers needed by psycopg2) never reaches
# the runtime image. The result is smaller and has a much smaller attack surface: a
# compiler in a production container is a gift to anyone who gets code execution.
#
# Base images are pinned by digest, not just by tag. `python:3.12-slim` is a moving
# target -- two builds a month apart produce different images from the same
# Dockerfile, which is the end of "it worked in staging" as a useful sentence.
# Update these deliberately, in a reviewed diff.

# ---------------------------------------------------------------- builder ---
FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65 AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build-only dependencies. psycopg2 compiles against libpq headers; the runtime
# stage needs only the shared library.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# The lockfile, and nothing else. There is no `pip install .` step any more: the
# application is not a distributable package, it is source that uvicorn imports, so
# the only thing pip has to do here is the dependencies. That also means editing a
# single Python file no longer invalidates this layer -- the slowest step is now
# reached only when requirements.txt actually changes.
COPY backend/requirements.txt ./requirements.txt

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Every version is pinned in requirements.txt. Unpinned installs meant two builds a
# month apart shipped different libraries, so a regression could arrive without a
# single line of this repository changing.
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r requirements.txt

# ---------------------------------------------------------------- runtime ---
FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65 AS runtime

# PYTHONPATH=/app because the two packages are imported from source rather than
# installed. WORKDIR alone is not enough: uvicorn's --workers spawns processes
# whose sys.path[0] is not guaranteed to be the working directory, and a
# ModuleNotFoundError from worker 2 of 4 is a confusing way to learn that.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    VANNA_DATA_DIR=/data \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. Container escapes are much less useful from an
# unprivileged account, and nothing here needs root.
RUN useradd --create-home --uid 10001 vanna

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# The library and the application, as two top-level packages. Exactly the layout
# `backend/` has on disk, so an import that works locally works here.
COPY backend/vanna/     /app/vanna/
COPY backend/vanna_app/ /app/vanna_app/

# The platform baseline and the starter packs. Content, not code: it is reviewed
# as a diff and edited by people who do not touch Python, which is why it lives
# outside the package -- the same argument `backend/domains/domains.yml` makes.
#
# Read-only at runtime, and the loader validates it at boot: a malformed baseline
# that starts the process and quietly applies nothing is the exact failure the
# baseline exists to prevent.
COPY backend/instructions/ /app/instructions/
COPY backend/domains/      /app/domains/

# The semantic projects, for the same reason and in the same place.
#
# Compose also bind-mounts these at /projects for VANNA_CONFIG_SOURCE=disk. They
# are copied in as well because the boot-time configuration import reads the tree
# under /app, and without them a `database`-mode first boot would import the
# instruction packs, find no projects, and leave every semantic workspace quietly
# answering from its physical catalog instead.
COPY backend/projects/     /app/projects/

# The schema. Applied at boot when VANNA_AUTO_MIGRATE is true, and by
# `python -m vanna_app.migrate upgrade` as a deploy job when it is not. Landed at
# the same relative position it has in the repository, so the resolver in
# migrate.py finds it by the same rule in both places.
COPY database/migrations/ /app/database/migrations/

# Writable state: the demo SQLite database, the schema catalog, and the markdown
# knowledge directory. Mounted as a volume in compose so it survives a rebuild.
#
# Created here, not just mounted, and owned by the runtime user: Docker initialises
# an *empty* named volume from the image path it covers, ownership included, so
# without this the volume arrives owned by root and the process -- uid 10001 -- cannot
# write the catalog it just scanned.
RUN mkdir -p /data && chown -R vanna:vanna /data /app

USER vanna

EXPOSE 8000

# Liveness, which deliberately does not touch the database -- see wiring.py. The
# orchestrator should use /ready for traffic decisions; this one only answers "is
# the process alive", so a slow control plane cannot cause a restart loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Multiple workers are possible only because quota, rate limiting and login
# throttling live in the control plane rather than in per-process dictionaries --
# with those, every worker enforced its own copy of each limit and the effective
# limit was the stated one times the worker count.
#
# The count comes from VANNA_WEB_CONCURRENCY, which the application also reads to
# compute its connection ceiling. It used to be a literal 4 here and a separate
# setting there: two copies of one number, free to disagree, and when they did the
# ceiling was quietly wrong in the unsafe direction -- the app reported a budget
# for two workers while four were running, so the real ceiling was double what the
# log said. Every pool is per process, so this number multiplies all of them.
#
# `sh -c` so the variable expands; `exec` so uvicorn keeps PID 1 and still
# receives signals.
#
# --forwarded-allow-ips is uvicorn's own trust boundary and is deliberately narrow;
# the application applies VANNA_TRUSTED_PROXIES on top when it works out a client
# address, so an X-Forwarded-For from outside the proxy network is ignored.
CMD ["sh", "-c", "exec uvicorn vanna_app.wiring:application --factory \
     --host 0.0.0.0 --port 8000 \
     --workers ${VANNA_WEB_CONCURRENCY:-2} \
     --proxy-headers --forwarded-allow-ips '*' \
     --no-server-header"]
