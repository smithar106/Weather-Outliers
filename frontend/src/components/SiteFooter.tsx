import Link from "next/link";

import { PUBLIC_API_BASE_URL } from "@/lib/api";

const REPO_URL = "https://github.com/smithar106/Weather-Outliers";

/**
 * The footer carries the two attributions the project is obliged to show —
 * Open-Meteo for the weather data and OpenFreeMap/OpenStreetMap for the tiles —
 * plus the standing disclaimer that these are statistical outliers and not
 * records. That sentence appears on every page by construction.
 */
export function SiteFooter() {
  return (
    <footer className="mt-24 border-t border-ink-700/70 bg-ink-950">
      <div className="mx-auto max-w-7xl px-5 py-12 sm:px-8">
        <div className="grid gap-10 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <p className="font-display text-base font-semibold text-paper">Weather Outliers</p>
            <p className="mt-2 max-w-xs text-sm leading-relaxed text-paper-muted">
              A daily statistical read on North American weather. Open source, and every number on
              the site is traceable to the calculation that produced it.
            </p>
          </div>

          <nav aria-label="Pages">
            <p className="eyebrow">Pages</p>
            <ul className="mt-3 space-y-2 text-sm">
              {[
                { href: "/", label: "Today's outliers" },
                { href: "/archive", label: "Archive" },
                { href: "/ask", label: "Ask the data" },
                { href: "/methodology", label: "Methodology" },
                { href: "/evaluation", label: "Evaluation" },
                { href: "/monitor", label: "Monitor" },
              ].map((item) => (
                <li key={item.href}>
                  <Link href={item.href} className="text-paper-dim transition-colors hover:text-paper">
                    {item.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>

          <div>
            <p className="eyebrow">Data &amp; tiles</p>
            <ul className="mt-3 space-y-2 text-sm text-paper-dim">
              <li>
                Weather data by{" "}
                <a
                  href="https://open-meteo.com/"
                  className="link-underline"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  Open-Meteo.com
                </a>
                , based on ERA5 reanalysis from Copernicus C3S / ECMWF.
              </li>
              <li>
                Map tiles by{" "}
                <a
                  href="https://openfreemap.org/"
                  className="link-underline"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  OpenFreeMap
                </a>
                , data{" "}
                <a
                  href="https://www.openstreetmap.org/copyright"
                  className="link-underline"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  © OpenStreetMap
                </a>{" "}
                contributors.
              </li>
            </ul>
          </div>

          <div>
            <p className="eyebrow">Project</p>
            <ul className="mt-3 space-y-2 text-sm text-paper-dim">
              <li>
                <a
                  href={REPO_URL}
                  className="link-underline"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  Source on GitHub
                </a>
              </li>
              <li>
                <a
                  href={`${PUBLIC_API_BASE_URL}/docs`}
                  className="link-underline"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  Public API documentation
                </a>
              </li>
              <li>
                <a
                  href={`${REPO_URL}/blob/main/LICENSE`}
                  className="link-underline"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  MIT licence
                </a>
              </li>
            </ul>
          </div>
        </div>

        <div className="mt-10 border-t border-ink-800 pt-6">
          <p className="max-w-4xl text-xs leading-relaxed text-paper-faint">
            <strong className="font-semibold text-paper-muted">
              These are statistical outliers, not weather records.
            </strong>{" "}
            Events are ranked by how unusual they are against a 1991–2020 seasonal distribution for
            each city, computed from gridded reanalysis. No authoritative records archive is
            consulted, so nothing here is verified as a city, state, national, or all-time record.
            Values are model estimates for the grid cell nearest each city, not readings from a
            weather station inside it.
          </p>
        </div>
      </div>
    </footer>
  );
}
