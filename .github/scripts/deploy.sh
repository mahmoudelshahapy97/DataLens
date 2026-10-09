#!/usr/bin/env bash
# Runs ON THE DEPLOY HOST, from $DEPLOY_PATH. Uploaded and invoked by
# .github/workflows/deploy.yml; not meant to be run from a laptop.
#
#   deploy.sh <bundle.tgz> <backend-image> <frontend-image> <dockerhub-user>
#
# The Docker Hub token arrives on stdin rather than as an argument, so it never
# appears in the host's process list.
#
# What the host must already have:
#   - $DEPLOY_PATH/.env      every secret the stack needs (see .env.example). CI never
#                            sees these; this script refuses to run without the file.
#   - multidb_network        the sandbox network docker-compose.yml joins as external.
#   - Docker Compose v2.17+  for repeated --env-file.
#
# The bundle carries the compose files and backend/projects, the one bind mount the
# stack reads from the host. Images come from Docker Hub, pinned by commit SHA, so
# nothing is built here.

set -euo pipefail

BUNDLE=$1
BACKEND_IMAGE=$2
FRONTEND_IMAGE=$3
HUB_USER=$4

# nginx publishes 3000 and proxies /ready to the API, so this checks the whole path
# a user takes, and /ready (unlike /health) fails when the control plane is down.
READY_URL=${READY_URL:-http://127.0.0.1:3000/ready}

STATE=.deploy
mkdir -p "$STATE"

dc() {
  docker compose --env-file .env --env-file release.env \
    -f docker-compose.yml -f docker-compose.prod.yml "$@"
}

log() { echo "==> $*"; }

# ------------------------------------------------------------ preconditions ---

if [ ! -f .env ]; then
  echo "ERROR: $(pwd)/.env is missing. Create it from .env.example before the first deploy." >&2
  exit 1
fi

if ! docker network inspect multidb_network >/dev/null 2>&1; then
  echo "ERROR: docker network multidb_network does not exist. Start the databases stack first." >&2
  exit 1
fi

# --------------------------------------------------------------------- pull ---
#
# Pull before touching anything, so a registry outage or a bad tag fails the deploy
# with the running stack untouched. Logged out again straight after: the token has
# no reason to sit in ~/.docker/config.json between deploys.

log "Pulling $BACKEND_IMAGE and $FRONTEND_IMAGE"
docker login --username "$HUB_USER" --password-stdin >/dev/null
trap 'docker logout >/dev/null 2>&1 || true' EXIT
docker pull --quiet "$BACKEND_IMAGE"
docker pull --quiet "$FRONTEND_IMAGE"
docker logout >/dev/null 2>&1 || true

# ----------------------------------------------------------------- snapshot ---
#
# The previous release is the files it ran with plus the image names in
# release.env. Keeping both makes a rollback exact rather than "the old images with
# the new compose file".

had_previous=0
if [ -f release.env ]; then
  had_previous=1
  cp release.env "$STATE/release.env.previous"
  tar -czf "$STATE/previous.tgz" docker-compose.yml docker-compose.prod.yml backend/projects 2>/dev/null || true
fi

# ------------------------------------------------------------------ install ---

log "Installing $(basename "$BUNDLE")"
rm -rf backend/projects
tar -xzf "$BUNDLE"
cat > release.env <<EOF
VANNA_BACKEND_IMAGE=$BACKEND_IMAGE
VANNA_FRONTEND_IMAGE=$FRONTEND_IMAGE
EOF

ready() {
  for _ in $(seq 1 30); do
    curl -fsS -m 5 -o /dev/null "$READY_URL" && return 0
    sleep 5
  done
  return 1
}

start() {
  # --wait blocks on the healthchecks in docker-compose.yml; the frontend already
  # waits for a healthy backend through depends_on.
  dc up -d --no-build --remove-orphans --wait --wait-timeout 300 && ready
}

rollback() {
  echo "ERROR: $1" >&2
  dc ps || true
  dc logs --tail 100 backend frontend || true
  if [ "$had_previous" = 0 ]; then
    echo "ERROR: first deploy on this host; nothing to roll back to. Containers left up for inspection." >&2
    exit 1
  fi
  log "Rolling back to $(grep VANNA_BACKEND_IMAGE "$STATE/release.env.previous" | cut -d= -f2)"
  rm -rf backend/projects
  tar -xzf "$STATE/previous.tgz"
  cp "$STATE/release.env.previous" release.env
  start || echo "ERROR: the rollback is not healthy either." >&2
  exit 1
}

# -------------------------------------------------------------------- start ---

log "Starting the stack"
start || rollback "the new release did not become ready at $READY_URL"

# -------------------------------------------------------------------- prune ---
#
# Keep the current and previous release of each image for a manual rollback, and
# nothing older: every release is a few hundred MB.

keep=$(cat release.env "$STATE/release.env.previous" 2>/dev/null | cut -d= -f2 | sort -u)
for image in "$BACKEND_IMAGE" "$FRONTEND_IMAGE"; do
  repo=${image%:*}
  docker image ls "$repo" --format '{{.Repository}}:{{.Tag}}' | while read -r ref; do
    grep -qxF "$ref" <<<"$keep" || docker image rm "$ref" >/dev/null 2>&1 || true
  done
done
docker image prune -f >/dev/null
rm -f "$BUNDLE"

log "Deployed $BACKEND_IMAGE"
