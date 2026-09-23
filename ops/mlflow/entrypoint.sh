#!/bin/sh
# Start the MLflow tracking server with a PostgreSQL backend store and a
# persistent artifact store, or refuse to start and say why.
#
# Everything this script does falls into one of three jobs:
#
#   1. Resolve the backend store URI and fix its scheme. Railway hands out
#      `postgres://`, which SQLAlchemy 2 no longer accepts, and the application's
#      own URL names psycopg 3 (`postgresql+psycopg://`), a driver this image does
#      not ship. Both are normalised to `postgresql://`.
#   2. Refuse to run an unauthenticated server unless that is stated explicitly.
#      The open-source MLflow server has no authentication of its own, so the only
#      thing standing between an accidental public domain and an open telemetry
#      store is this check.
#   3. Make the artifact directory writable and drop root. Railway mounts a volume
#      owned by root, so the chown has to happen after the mount exists, which
#      means at start-up rather than at build time.
#
# Configuration, all from the environment:
#
#   MLFLOW_BACKEND_STORE_URI   PostgreSQL URI. Falls back to DATABASE_URL.
#   MLFLOW_ARTIFACTS_DESTINATION  Directory for artifacts. Default /data/mlartifacts.
#   MLFLOW_AUTH_USERNAME / MLFLOW_AUTH_PASSWORD   Enable HTTP basic auth.
#   MLFLOW_AUTH_DATABASE_URI   Where basic auth keeps its user table. Default: the
#                              backend store.
#   MLFLOW_ALLOW_ANONYMOUS     "true" to run with no authentication at all. Only
#                              defensible for a service with no public domain.
#   PORT                       Injected by the platform. Default 5000.
#   MLFLOW_RUN_AS_UID          Unprivileged uid to drop to. Default 10001.

set -eu

log() { printf '%s mlflow-entrypoint: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------- backend store

store="${MLFLOW_BACKEND_STORE_URI:-${DATABASE_URL:-}}"
[ -n "$store" ] || die "neither MLFLOW_BACKEND_STORE_URI nor DATABASE_URL is set.
  Attach the PostgreSQL service and set MLFLOW_BACKEND_STORE_URI to a reference
  such as \${{Postgres.DATABASE_URL}}, pointed at a database reserved for MLflow.
  Do not share the application's database: MLflow runs its own migrations in it."

case "$store" in
    postgres://*) store="postgresql://${store#postgres://}" ;;
esac
case "$store" in
    # postgresql+psycopg://, postgresql+asyncpg://, ... — this image ships psycopg2,
    # which is what SQLAlchemy selects for the bare scheme.
    postgresql+*://*) store="postgresql://${store#postgresql+*://}" ;;
esac

case "$store" in
    postgresql://*) : ;;
    sqlite:*) log "WARNING: a SQLite backend store is for local testing only." ;;
    *) die "unsupported backend store scheme in MLFLOW_BACKEND_STORE_URI." ;;
esac

# ------------------------------------------------------------- artifact storage

artifacts="${MLFLOW_ARTIFACTS_DESTINATION:-/data/mlartifacts}"
run_uid="${MLFLOW_RUN_AS_UID:-10001}"

mkdir -p "$artifacts" 2>/dev/null || die "cannot create $artifacts.
  Attach a Railway volume mounted at its parent directory (default /data), or set
  MLFLOW_ARTIFACTS_DESTINATION to a path that is writable and persistent. Without
  a volume every artifact is lost on the next deploy."

# ------------------------------------------------------------- authentication

auth_app=""
if [ -n "${MLFLOW_AUTH_USERNAME:-}" ] && [ -n "${MLFLOW_AUTH_PASSWORD:-}" ]; then
    config="${TMPDIR:-/tmp}/mlflow-auth.ini"
    # 0600 before the file exists, not after: the password is in it.
    umask 077
    printf '[mlflow]\n' > "$config"
    printf 'default_permission = READ\n' >> "$config"
    printf 'database_uri = %s\n' "${MLFLOW_AUTH_DATABASE_URI:-$store}" >> "$config"
    printf 'admin_username = %s\n' "$MLFLOW_AUTH_USERNAME" >> "$config"
    printf 'admin_password = %s\n' "$MLFLOW_AUTH_PASSWORD" >> "$config"
    printf 'authorization_function = mlflow.server.auth:authenticate_request_basic_auth\n' \
        >> "$config"
    umask 022
    chown "$run_uid" "$config" 2>/dev/null || true
    export MLFLOW_AUTH_CONFIG_PATH="$config"
    auth_app="basic-auth"
    log "HTTP basic auth enabled. Clients need MLFLOW_TRACKING_USERNAME and MLFLOW_TRACKING_PASSWORD."
elif [ "${MLFLOW_ALLOW_ANONYMOUS:-false}" = "true" ]; then
    log "WARNING: running with NO authentication, because MLFLOW_ALLOW_ANONYMOUS=true."
    log "WARNING: anyone who can reach this port can read and write every trace."
    log "WARNING: only defensible with no public domain on this service."
else
    die "no authentication configured.
  This server would accept any request that reached it, and MLflow has no
  authentication of its own. Choose one:
    * set MLFLOW_AUTH_USERNAME and MLFLOW_AUTH_PASSWORD (recommended), or
    * set MLFLOW_ALLOW_ANONYMOUS=true, and only with no public domain attached,
      so the service is reachable on the private network alone."
fi

# --------------------------------------------------------------- allowed hosts

# MLflow 3 ships a DNS-rebinding guard that checks the Host header, and its default
# allows localhost and private IPs only. On Railway a client connects by *name* —
# http://mlflow.railway.internal:5000 — so with the default the health check on
# /health passes (it is exempt) while every real API call returns 403. A service
# that looks healthy and rejects all of its clients is worth one line of config.
#
# Measured on MLflow 3.16.1: Host: mlflow.railway.internal:5000 against a default
# server gives 200 on /health and 403 on /api/2.0/mlflow/experiments/search.
allowed_hosts="${MLFLOW_ALLOWED_HOSTS:-*.railway.internal,*.railway.internal:*,localhost,localhost:*,127.0.0.1,127.0.0.1:*}"

# ------------------------------------------------- Flask CSRF signing key

# MLflow 3 refuses to start its auth app without this, and it must be the same on
# every restart or every replica, or sessions fail validation. Rather than make the
# operator invent and store a fourth secret, it is derived from one they already
# have — the auth password, or failing that the backend store URI. Deriving it
# means a password rotation rotates the key too, which invalidates open sessions
# and nothing else.
if [ -z "${MLFLOW_FLASK_SERVER_SECRET_KEY:-}" ]; then
    _seed_label="csrf-from-store-uri"
    _seed="$store"
    if [ -n "${MLFLOW_AUTH_PASSWORD:-}" ]; then
        _seed_label="csrf-from-auth-password"
        _seed="$MLFLOW_AUTH_PASSWORD"
    fi
    # Through the environment, not argv: argv is visible in the process list.
    MLFLOW_FLASK_SERVER_SECRET_KEY="$(
        _SEED="$_seed" _LABEL="$_seed_label" python -c 'import hashlib, os
seed = (os.environ["_LABEL"] + "/" + os.environ["_SEED"]).encode()
print(hashlib.sha256(seed).hexdigest())'
    )"
    export MLFLOW_FLASK_SERVER_SECRET_KEY
    log "derived the Flask signing key ($_seed_label). Set MLFLOW_FLASK_SERVER_SECRET_KEY to pin it yourself."
    unset _seed _seed_label
fi

# ------------------------------------------------------------------------- run

port="${PORT:-5000}"
log "starting: port=$port artifacts=$artifacts auth=${auth_app:-none}"

set -- mlflow server \
    --backend-store-uri "$store" \
    --serve-artifacts \
    --artifacts-destination "$artifacts" \
    --host 0.0.0.0 \
    --port "$port" \
    --allowed-hosts "$allowed_hosts"
[ -z "$auth_app" ] || set -- "$@" --app-name "$auth_app"
[ -z "${MLFLOW_CORS_ALLOWED_ORIGINS:-}" ] \
    || set -- "$@" --cors-allowed-origins "$MLFLOW_CORS_ALLOWED_ORIGINS"

if [ "$(id -u)" = "0" ]; then
    chown -R "$run_uid" "$artifacts" 2>/dev/null \
        || log "WARNING: could not chown $artifacts; the server may not be able to write to it."
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid="$run_uid" --regid="$run_uid" --clear-groups "$@"
    fi
    log "WARNING: setpriv is unavailable, so the server runs as root."
fi

exec "$@"
