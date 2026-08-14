# Vanna API — FastAPI + uvicorn
#
# Two stages so build tooling (compilers, headers needed by psycopg2) never
# reaches the runtime image. The result is smaller and has a much smaller
# attack surface: a compiler in a production container is a gift to anyone who
# gets code execution.

# ---------------------------------------------------------------- builder ---
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build-only dependencies. psycopg2 compiles against libpq headers; the runtime
# stage needs only the shared library.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only what the build needs first, so editing source does not invalidate
# the dependency layer -- the slowest step should be the least often repeated.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Database drivers.
#
# The connection form offers every engine in vanna.core.datasource, so the image
# has to be able to build a runner for the ones an operator can pick -- offering
# MySQL and then failing on "PyMySQL is required" is a worse experience than not
# offering it. These four are pure-Python or ship wheels, so they cost image size
# and nothing else:
#
#   pymysql            MySQL / MariaDB
#   oracledb           Oracle, thin mode -- no Instant Client needed
#   duckdb             DuckDB
#   clickhouse-connect ClickHouse
#
# Not installed here: pyodbc (SQL Server) needs unixODBC plus Microsoft's driver
# from a third-party apt repository, and BigQuery/Snowflake pull large SDKs. All
# three remain available via `pip install 'vanna[mssql]'` etc. in a derived image;
# the registry reports them and the runner raises a clear install hint.
RUN pip install --upgrade pip setuptools wheel \
 && pip install "." \
      "uvicorn[standard]>=0.27" \
      "fastapi>=0.110" \
      "psycopg2-binary" \
      "pymysql" \
      "oracledb" \
      "duckdb" \
      "clickhouse-connect" \
      "anthropic" \
      "openai" \
 `# Vector retrieval. fastembed runs a small ONNX model in-process, so` \
 `# embeddings need no API key and no network at query time -- which is what` \
 `# lets VANNA_INDEX_BACKEND=qdrant work in an air-gapped deployment. The` \
 `# model weights are fetched on first use into the embed-cache volume.` \
      "qdrant-client" \
      "fastembed"

# ---------------------------------------------------------------- runtime ---
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    VANNA_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. Container escapes are much less useful from an
# unprivileged account, and nothing here needs root.
RUN useradd --create-home --uid 10001 vanna

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# app.py is the assembly; tenancy.py owns the control-plane database, accounts.py
# the credentials, and the *_routes modules the HTTP surface over them. All are
# imported as top-level modules, so they must sit together in the working
# directory. Listed explicitly rather than `COPY docker/ /app/` so that adding a
# file to docker/ is a deliberate act -- but note the failure mode that costs:
# a new module not added here fails at import, on boot, with ModuleNotFoundError.
COPY docker/app.py docker/tenancy.py docker/accounts.py docker/billing.py \
     docker/portal_routes.py docker/auth_routes.py /app/

# Writable state: the demo SQLite database, the schema catalog, the generation
# log, and the markdown knowledge directory. Mounted as a volume in compose so
# it survives a rebuild.
# /data/embed-cache is created here, not just mounted, and that matters: Docker
# initialises an *empty* named volume from the image path it covers, ownership
# included. Without this the volume arrives owned by root, the container runs as
# uid 10001, and fastembed cannot write the model it just downloaded -- which
# surfaces as "Could not load model ... from any source" and a silent fall back
# to keyword-only retrieval.
RUN mkdir -p /data/embed-cache && chown -R vanna:vanna /data /app

USER vanna

EXPOSE 8000

# Hits /health, which deliberately does not touch the database -- see app.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
