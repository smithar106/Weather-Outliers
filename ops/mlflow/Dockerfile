# The MLflow tracking server: the private service that stores this project's
# traces, evaluation runs and their artifacts.
#
#   Build context: this directory (ops/mlflow). Set the Railway service's root
#   directory to `ops/mlflow` and it finds this file and ops/mlflow/railway.json,
#   exactly as the website does with frontend/.
#
# Why a separate image rather than the backend's:
#
#   * The backend installs `mlflow-skinny`, which is a client. It has no server and
#     no web UI — no Flask, no gunicorn — so `mlflow server` and `mlflow ui` cannot
#     run from it at all. The server needs the full distribution.
#   * The backend image must stay small and must not grow a web framework it does
#     not serve. Telemetry is infrastructure here, not part of the application.
#
# Nothing from the application is copied in. This image runs one upstream program.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MLFLOW_ARTIFACTS_DESTINATION=/data/mlartifacts

# Pinned, because an unpinned tracking server can migrate the backend store to a
# schema an older client cannot read, during a deploy nobody was watching.
#
#   mlflow[auth]      the server plus its HTTP basic auth app
#   psycopg2-binary   the PostgreSQL driver SQLAlchemy selects for `postgresql://`.
#                     The binary wheel is deliberate: no build toolchain in the
#                     image. (The application itself uses psycopg 3; these are
#                     separate images and need not agree.)
RUN pip install --no-cache-dir \
        "mlflow[auth]==3.16.1" \
        "psycopg2-binary==2.9.10"

# The unprivileged account the entrypoint drops to. No home directory and no
# shell: this account exists to own a directory and run one process.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mlflow \
    && mkdir -p /data/mlartifacts \
    && chown -R 10001:10001 /data

COPY entrypoint.sh /usr/local/bin/mlflow-entrypoint
RUN chmod 0755 /usr/local/bin/mlflow-entrypoint

# Deliberately no USER: the entrypoint starts as root so it can chown a volume
# that only exists once the platform has mounted it, then drops to uid 10001 with
# setpriv before exec'ing the server. See the comments in entrypoint.sh.

EXPOSE 5000

# No health check here. Railway reads one from railway.json, and a HEALTHCHECK in
# the image would duplicate it in a second place that nobody thinks to update.
ENTRYPOINT ["/usr/local/bin/mlflow-entrypoint"]
