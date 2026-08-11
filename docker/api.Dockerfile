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

RUN pip install --upgrade pip setuptools wheel \
 && pip install "." \
      "uvicorn[standard]>=0.27" \
      "fastapi>=0.110" \
      "psycopg2-binary" \
      "anthropic" \
      "openai"

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
RUN mkdir -p /data && chown -R vanna:vanna /data /app

USER vanna

EXPOSE 8000

# Hits /health, which deliberately does not touch the database -- see app.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
