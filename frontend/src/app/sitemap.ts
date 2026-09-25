import type { MetadataRoute } from "next";

import { getArchive, getCities } from "@/lib/api";

/**
 * The canonical base URL, resolved at runtime. Matches `robots.ts`.
 */
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://weather-outliers-app.up.railway.app";

export const dynamic = "force-dynamic";

/**
 * The fixed, content pages. The ops-facing routes (`/monitor`, `/agent`) are
 * deliberately excluded — they describe the pipeline, not the weather.
 */
const STATIC_PATHS = ["/", "/map", "/archive", "/methodology", "/evaluation", "/ask"];

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const entries: MetadataRoute.Sitemap = STATIC_PATHS.map((path) => ({
    url: `${SITE_URL}${path}`,
  }));

  try {
    const [cities, archive] = await Promise.all([
      getCities(),
      getArchive({ limit: 1000 }),
    ]);

    for (const city of cities.cities) {
      entries.push({ url: `${SITE_URL}/city/${encodeURIComponent(city.id)}` });
    }
    for (const entry of archive.entries) {
      entries.push({
        url: `${SITE_URL}/archive/${entry.analysis_date}`,
        lastModified: entry.published_at ? new Date(entry.published_at) : undefined,
      });
    }
  } catch {
    // The fixed routes still make a valid sitemap; dynamic entries are dropped
    // rather than letting a backend blip take the sitemap down.
  }

  return entries;
}
