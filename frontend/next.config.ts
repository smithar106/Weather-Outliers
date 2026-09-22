import type { NextConfig } from "next";

/**
 * `standalone` output is what makes the production image small: Next traces the
 * modules actually reached and copies only those, so the runtime stage does not
 * need `node_modules` at all.
 */
const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  // Note: Next 16 removed the build-time ESLint integration and the `eslint`
  // config key with it, so there is nothing to opt out of here. Linting is its own
  // CI step (`npm run lint`), which is where it belongs — a formatting rule should
  // fail a pull request, not take the public site down on a redeploy.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};

export default nextConfig;
