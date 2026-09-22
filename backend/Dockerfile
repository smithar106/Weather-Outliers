# Backend image: serves the read-only public API, and is also the image the
# scheduled worker runs `python -m app.pipeline` inside. One image for both is
# deliberate — the pipeline and the API must agree about the methodology version
# and the schema, and separate images are how they drift apart.
#
# Build context is the repository root, not backend/, so that `evals/` can be
# copied in: the API serves the evaluation report and the worker can run the
# harness. Build with:
#
#   docker build -f backend/Dockerfile -t weather-outliers-backend .

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# psycopg[binary] ships its own libpq, so no build toolchain and no libpq-dev are
# needed here. Kept in mind rather than discovered: adding a dependency that
# needs compiling means adding a builder stage, not apt-get to this one.

# Dependencies first, so a code change does not reinstall them.
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/app/__init__.py ./app/__init__.py
RUN pip install --no-cache-dir .

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
# The city registry is data the application reads at seed time, not a fixture.
COPY data ./data
# The evaluation report the API serves, plus the harness the worker can rerun.
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
