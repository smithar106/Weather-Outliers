/**
 * Capture the screenshots used in the README and docs.
 *
 * Playwright is deliberately *not* a dependency of this repository. It is a
 * 100 MB+ browser download that nothing in the application, the test suite or the
 * deployment needs — it exists only so that a human refreshing the README does not
 * have to crop five browser windows by hand. So it is invoked ad hoc, from
 * `frontend/`, which is also why the script lives here rather than at the repo root:
 * Node resolves `import "playwright"` by walking up from this file, so it has to be
 * inside the workspace that has the package installed.
 *
 *   cd frontend
 *   npm install --no-save playwright
 *   npx playwright install chromium     # first run only
 *   node scripts/capture-screenshots.mjs
 *
 * Requires a running backend and frontend. See docs/local-development.md.
 *
 *   BASE_URL   frontend origin            (default http://127.0.0.1:3050)
 *   OUT_DIR    where the PNGs are written (default ../docs/screenshots)
 *   CITY_ID    which city detail page     (default the top-ranked city of the
 *              latest board, resolved from the API so the shot is never of a
 *              hard-coded city that has since dropped off the board)
 */

import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import { chromium } from "playwright";

const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:3050";
const API_BASE_URL = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";
const OUT_DIR = process.env.OUT_DIR ?? path.resolve(import.meta.dirname, "../../docs/screenshots");

/** Wide enough for the desktop layout, tall enough that the fold is not the whole story. */
const VIEWPORT = { width: 1440, height: 1000 };
/**
 * 1x by default. A 2x capture of a 1440x1000 page is a ~2MB PNG, and six of them
 * is 13MB of binary committed to a repository whose entire point is readable
 * source. At 1x the README images are still sharp at their rendered width.
 * Override with SCALE=2 for a one-off high-resolution capture.
 */
const SCALE = Number(process.env.SCALE ?? 1);

/**
 * What is actually on the board being photographed.
 *
 * Returned so the manifest can record it. A screenshot of fixture data looks
 * exactly like a screenshot of real weather, and an image in a README is a claim
 * about the application — so the provenance of the numbers in the frame gets
 * written down beside the file, not left to whoever committed it to remember.
 */
async function resolveBoard() {
  const response = await fetch(`${API_BASE_URL}/api/rankings/latest`);
  if (!response.ok) {
    throw new Error(`cannot read the published board: ${API_BASE_URL} answered ${response.status}`);
  }
  const rankings = await response.json();
  const events = rankings.events ?? [];
  if (events.length === 0) {
    throw new Error("the latest board has no events, so there is nothing to photograph");
  }

  const datasets = [
    ...new Set(events.map((entry) => entry.event?.source_dataset).filter(Boolean)),
  ].sort();
  const tiers = [...new Set(events.map((entry) => entry.event?.data_tier).filter(Boolean))].sort();
  const cities = new Set(events.map((entry) => entry.event?.city?.id).filter(Boolean));

  return {
    cityId: process.env.CITY_ID ?? events[0].event?.city?.id,
    provenance: {
      analysis_date: rankings.analysis_date ?? null,
      published_at: rankings.published_at ?? null,
      methodology_version: rankings.methodology_version ?? null,
      // The value to read first. `synthetic_fixture_v1` means these images are of
      // deterministic test data and must not be presented as real weather.
      source_datasets: datasets,
      data_tiers: tiers,
      events_published: events.length,
      distinct_cities: cities.size,
    },
  };
}

async function main() {
  const { cityId, provenance } = await resolveBoard();
  await mkdir(OUT_DIR, { recursive: true });

  const shots = [
    { name: "home", path: "/", fullPage: true },
    { name: "city", path: `/city/${cityId}`, fullPage: true },
    { name: "methodology", path: "/methodology", fullPage: true },
    { name: "evaluation", path: "/evaluation", fullPage: true },
    { name: "archive", path: "/archive", fullPage: true },
  ];

  // Defaults to the Chrome already on the machine, so refreshing a screenshot does
  // not require a separate browser download on top of the package. Set
  // BROWSER_CHANNEL= (empty) to use Playwright's own bundled Chromium instead,
  // which is the more reproducible choice if the images ever need to match exactly.
  const channel = process.env.BROWSER_CHANNEL ?? "chrome";
  const browser = await chromium.launch(channel ? { channel } : {});
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: SCALE,
    colorScheme: "light",
    // Freezes the animated hero gradient and any transition mid-flight, so two
    // runs of this script produce comparable images.
    reducedMotion: "reduce",
  });

  const problems = [];
  const captured = [];

  for (const shot of shots) {
    const page = await context.newPage();
    const consoleErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => consoleErrors.push(`pageerror: ${error.message}`));

    const url = `${BASE_URL}${shot.path}`;
    try {
      // `load`, not `networkidle`: every page here is server-rendered HTML that is
      // complete at `load`.
      const response = await page.goto(url, { waitUntil: "load", timeout: 30_000 });
      const status = response?.status();
      if (status !== 200) throw new Error(`${url} answered ${status}`);
      if (shot.waitFor) await shot.waitFor(page);

      const file = path.join(OUT_DIR, `${shot.name}.png`);
      await page.screenshot({ path: file, fullPage: shot.fullPage });
      captured.push({ name: shot.name, url, file, consoleErrors });
      console.log(`✓ ${shot.name.padEnd(12)} ${url}`);
    } catch (error) {
      problems.push(`${shot.name}: ${error.message}`);
      console.error(`✗ ${shot.name.padEnd(12)} ${error.message}`);
    } finally {
      // Console errors are reported even on success. A page can look perfect and
      // still be throwing in the browser, and a screenshot would hide that.
      if (consoleErrors.length > 0) {
        console.error(`  console errors on ${shot.name}:`);
        for (const line of consoleErrors) console.error(`    ${line}`);
      }
      await page.close();
    }
  }

  await browser.close();

  // A manifest so the README's images can be traced back to what produced them.
  await writeFile(
    path.join(OUT_DIR, "manifest.json"),
    `${JSON.stringify(
      {
        captured_at: new Date().toISOString(),
        base_url: BASE_URL,
        viewport: VIEWPORT,
        device_scale_factor: SCALE,
        // The data behind the images, not just the images.
        data: provenance,
        shots: captured.map(({ name, url, file, consoleErrors }) => ({
          name,
          url,
          // Repo-relative: an absolute path records whose laptop took the picture,
          // which is both noise in a diff and a small privacy leak.
          file: path.relative(path.resolve(import.meta.dirname, "../.."), file),
          console_errors: consoleErrors,
        })),
        failed: problems,
      },
      null,
      2,
    )}\n`,
  );

  if (problems.length > 0) {
    console.error(`\n${problems.length} shot(s) failed.`);
    process.exit(1);
  }
  console.log(`\n${captured.length} screenshots written to ${OUT_DIR}`);
}

await main();
