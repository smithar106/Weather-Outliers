#!/usr/bin/env node
/**
 * Copies the evaluation report into the frontend source tree.
 *
 * The evaluation dashboard renders real measurements, so the report has to be a
 * build-time artefact rather than something fetched at runtime: the page must
 * show the numbers that belong to the deployed commit, not whatever a live
 * service happens to return.
 *
 * `evals/reports/latest.json` lives outside `frontend/`, and a platform that
 * builds with the frontend directory as its root cannot see it. So the copy in
 * `src/data/` is committed, and this script keeps it honest:
 *
 *   node scripts/sync-evaluation-report.mjs           # copy if the source exists
 *   node scripts/sync-evaluation-report.mjs --check    # fail if they differ (CI)
 *
 * Running with no source present is not an error. That is the normal state of a
 * frontend-rooted build, which uses the committed copy.
 */

import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = resolve(here, "..", "..", "evals", "reports", "latest.json");
const TARGET = resolve(here, "..", "src", "data", "evaluation-report.json");

const check = process.argv.includes("--check");

function normalise(raw) {
  // Compared as parsed JSON so indentation or trailing-newline differences do
  // not read as a stale report.
  return JSON.stringify(JSON.parse(raw));
}

if (!existsSync(SOURCE)) {
  if (!existsSync(TARGET)) {
    console.error(
      `No evaluation report at ${SOURCE} and no committed copy at ${TARGET}.\n` +
        "Run `backend/.venv/bin/python -m evals.runner` from the repository root."
    );
    process.exit(1);
  }
  console.log(
    "sync-evaluation-report: no source report visible from this build root; " +
      "using the committed copy."
  );
  process.exit(0);
}

const source = readFileSync(SOURCE, "utf8");

if (check) {
  if (!existsSync(TARGET)) {
    console.error(`sync-evaluation-report: ${TARGET} is missing. Run npm run sync-evals.`);
    process.exit(1);
  }
  if (normalise(source) !== normalise(readFileSync(TARGET, "utf8"))) {
    console.error(
      "sync-evaluation-report: the committed report differs from evals/reports/latest.json.\n" +
        "Run `npm run sync-evals` in frontend/ and commit the result."
    );
    process.exit(1);
  }
  console.log("sync-evaluation-report: committed report matches evals/reports/latest.json.");
  process.exit(0);
}

mkdirSync(dirname(TARGET), { recursive: true });
writeFileSync(TARGET, source, "utf8");

const report = JSON.parse(source);
console.log(
  `sync-evaluation-report: copied ${report.status} report from ${report.generated_at} ` +
    `(${report.totals.cases_passed}/${report.totals.cases_total} checks).`
);
