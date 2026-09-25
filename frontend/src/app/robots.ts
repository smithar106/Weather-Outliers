import type { MetadataRoute } from "next";

/**
 * The canonical base URL, resolved at runtime so the sitemap reference is always
 * the production domain. The build has no environment variables by design, so a
 * fallback is hardcoded for local development, where the exact domain does not
 * matter.
 */
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://weather-outliers-app.up.railway.app";

export const dynamic = "force-dynamic";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: "*", allow: "/" },
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
