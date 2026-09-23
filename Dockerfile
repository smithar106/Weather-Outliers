# Backend image: serves the read-only public API, and is also the image the
# scheduled worker runs `python -m app.pipeline` inside. One image for both is
# deliberate — the pipeline and the API must agree about the methodology version
# and the schema, and separate images are how they drift apart.
#
#   docker build -t weather-outliers-backend .
#
# Why this file is at the repository root rather than in backend/, which is where
# it belongs on the face of it:
#
#   1. It needs a root build context regardless, because the city registry lives
#      in `data/` outside backend/ and both the API and the pipeline read it.
#   2. PaaS builders detect a Dockerfile at the root of the build context and
#      otherwise fall back to language autodetection, which cannot make sense of a
#      repository whose root holds a Python service and a Node service side by
#      side. Three Railway deploys failed on exactly that before this moved here.
#      See docs/deployment.md.
#
# The website has its own self-contained image in frontend/, built with frontend/
# as its context. `evals/` is copied here so the evaluation delivery worker
# (python -m evals.deliver) can run in the same image the workers already use;
# nothing in `app/` imports it, so the API never executes harness code.

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# psycopg[binary] ships its own libpq, so no build toolchain and no libpq-dev are
# needed here. Kept in mind rather than discovered: adding a dependency that
# needs compiling means adding a builder stage, not apt-get to this one.

# Dependencies first, so a code change does not reinstall them. The code is copied
# *after* this layer precisely so that editing it does not reinstall every dependency,
# which means setuptools has no package directory to find when it runs — so `app` is
# stubbed for the length of the install.
#
# The stub must then be removed from **site-packages**, not just from the build
# context, and that is the whole point of the last two lines. `app` is an implicit
# namespace package here — there is no backend/app/__init__.py — and Python resolves
# a regular package ahead of a namespace one *regardless of sys.path order*: a
# namespace portion is recorded and the scan continues, so a one-line
# site-packages/app/__init__.py silently wins over /app/app. The container then builds
# perfectly and dies on `Error loading ASGI app. Could not import module "app.main"`.
# That shipped once. The find_spec assertion is here so it cannot ship twice.
#
# The `[tracing]` extra installs `mlflow-skinny`, the tracking client. It is here
# so the scheduled workers can send traces to the private MLflow service; it is
# inert until MLFLOW_TRACING_ENABLED is true, and `app.observability.tracing` is
# written so that its absence is not an error. Skinny, not `mlflow`: the full
# distribution would add pandas, numpy, scipy and Flask to an image that serves an
# API. The tracking *server* is a separate image — see ops/mlflow/.
COPY backend/pyproject.toml ./pyproject.toml
RUN mkdir -p app \
    && touch app/__init__.py \
    && pip install --no-cache-dir ".[tracing]" \
    && rm -rf app "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/app" \
    && python -c "import importlib.util, sys; sys.exit(None if importlib.util.find_spec('app') is None else 'a stub app package survived the dependency layer and would shadow /app/app at runtime')"

COPY backend/app ./app
# Fail the build, not the deploy, if `app.main` is not importable. find_spec imports
# the parent package only, so this executes no application code and needs no
# database, no settings and no network.
RUN python -c "import importlib.util, sys; sys.exit(None if importlib.util.find_spec('app.main') else 'app.main is not importable from WORKDIR; uvicorn would fail the same way at runtime')"
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
# The city registry is data the application reads at seed time, not a fixture.
COPY data ./data
# The evaluation harness, for the delivery worker. It imports `app` through the
# cwd (the app package is /app/app) and needs data/cities.json above. Not
# imported by `app` itself, so the API never touches it.
COPY evals ./evals

# Run as a non-root user. The container writes nothing outside /tmp.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# PORT is injected by most platforms, including Railway; 8000 is the local default.
# One worker: the pipeline is a separate process and the API is I/O-bound against
# PostgreSQL, so process count is a deployment knob rather than a Dockerfile one.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
