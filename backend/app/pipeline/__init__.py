"""The scheduled pipeline: fetch, analyse, rank, explain, publish.

Nothing in the public API imports this package, and nothing here imports the API.
That separation is the point of the architecture: all of the cost — provider
requests, decades of climatology, model tokens — is paid once here, by a job, and
the website only ever reads the rows this package leaves behind.
"""

from app.pipeline.runner import (
    Pipeline,
    PipelineError,
    RunReport,
    backfill,
    coverage,
    finalize,
    resolve_analysis_date,
    seed_cities,
)

__all__ = [
    "Pipeline",
    "PipelineError",
    "RunReport",
    "backfill",
    "coverage",
    "finalize",
    "resolve_analysis_date",
    "seed_cities",
]
