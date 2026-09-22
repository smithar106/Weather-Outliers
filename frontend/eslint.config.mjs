/**
 * Flat config, composed directly from `eslint-config-next`.
 *
 * As of Next 16 the shared configs are published as flat-config arrays, so they are
 * spread in as-is. The older `FlatCompat`-plus-`extends` recipe that most guides
 * still show does not work against them — the compatibility layer tries to validate
 * them as eslintrc objects and dies on a circular plugin reference.
 */

import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypeScript from "eslint-config-next/typescript";

const config = [
  {
    ignores: [".next/**", "node_modules/**", "next-env.d.ts", "src/data/**"],
  },
  ...nextCoreWebVitals,
  ...nextTypeScript,
];

export default config;
