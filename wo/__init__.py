"""``wo`` — a read-only observability CLI for Weather Outliers.

The CLI analyses the MLflow traces and the application's own PostgreSQL database
to answer questions about the pipeline and the LLM explanation path: what ran,
what failed, what it cost, and how two versions compare.

Run it from the repository root with the backend virtualenv, the same way
``evals`` is run:

    backend/.venv/bin/python -m wo summary
    backend/.venv/bin/python -m wo traces
    backend/.venv/bin/python -m wo trace <trace_id>

The CLI never writes to MLflow or to the application database. See ``wo/README.md``.
"""

__version__ = "0.1.0"
