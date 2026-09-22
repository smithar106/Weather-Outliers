# Evaluation harness

Everything the evaluation dashboard displays is produced by this harness. Nothing
in it is written down by hand: each number in the published report is measured
during the run that produced the report.

```bash
# from the repository root
backend/.venv/bin/python -m evals.runner
```

The run writes `evals/reports/latest.json`, prints a summary table, and exits
non-zero if any suite fails. `evals/reports/latest.json` is committed, because it
is the artefact the frontend renders; every other file in `evals/reports/` is
ignored so ad-hoc runs do not pollute the repository.

## What is measured

| Suite | What it does | What it reports |
| --- | --- | --- |
| `unit_tests` | Runs the backend pytest suite via `--junitxml` and parses the real XML | Test counts per module, failures, errors, wall-clock |
| `grounding` | Replays labelled explanation cases through the production guards | Accuracy, precision and recall on rejecting ungrounded prose, per-category results |
| `reproducibility` | Builds a synthetic world, runs the pipeline twice in one database and once in a fresh one, and compares the boards field by field | Whether the board is bit-for-bit reproducible, idempotency, stage latencies, data completeness |
| `api_contract` | Exercises every public endpoint over the published board | Status codes, response latency, read-only enforcement, whether the served board matches the stored calculations |

## Honesty rules this harness follows

* **No figure is hardcoded.** If a suite cannot measure something it reports
  `null` and says why, rather than filling in a plausible number.
* **The report states its own provenance**: the git commit, the Python version,
  the weather provider, and the LLM provider that were in effect.
* **The synthetic provider is labelled as synthetic.** The reproducibility and
  API suites run against `synthetic_fixture_v1`, not against real weather, and
  the report says so. They measure the *machinery*, not forecast skill.
* **Model quality is not claimed.** With no LLM key configured the grounding
  suite measures the guards and the deterministic templates. Running it with a
  key configured additionally measures how often a real model's output survives
  those guards, and the report records which mode it ran in.

## Labelled cases

`evals/cases/grounding.json` holds the labelled explanation cases. Each case
fixes the tool output an agent was shown, the prose it produced, and the verdict
a correct guard must reach. Cases are grouped by the failure they probe:

* `clean` — faithful prose that must be accepted
* `fabricated_number` — a figure no tool returned
* `record_claim` — record or all-time language, which this project never verifies
* `causal_claim` — a named weather system or an asserted cause
* `source_reference` — an outside organisation or a URL
* `missing_evidence` — the observed value is never stated
* `wrong_city` — prose about a different city than the event

Adding a case is the intended way to extend the eval: append to the JSON, rerun
the harness, and the dashboard picks it up.
