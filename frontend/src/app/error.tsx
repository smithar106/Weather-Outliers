"use client";

/**
 * The last-resort boundary.
 *
 * Pages handle their own API failures and render a `LoadFailure` explaining what
 * could not be loaded, so reaching this component means something unexpected went
 * wrong in rendering. It shows the digest rather than the message: Next.js strips
 * server error messages in production precisely so they cannot leak internals, and
 * the digest is what correlates the page a visitor saw with a line in the logs.
 */

import Link from "next/link";
import { useEffect } from "react";

import { Card, Container } from "@/components/ui";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("unhandled rendering error", error);
  }, [error]);

  return (
    <Container className="py-24">
      <Card className="mx-auto max-w-2xl p-8">
        <p className="eyebrow">Something broke</p>
        <h1 className="mt-2 font-display text-2xl tracking-tight text-paper">
          This page could not be rendered.
        </h1>
        <p className="mt-3 text-[0.9375rem] leading-relaxed text-paper-muted">
          No partial or approximate board is shown in place of the real one. The published data is
          unaffected — this is a fault in rendering it.
        </p>
        {error.digest && (
          <p className="tnum mt-4 rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-xs text-paper-faint">
            digest {error.digest}
          </p>
        )}
        <div className="mt-6 flex flex-wrap gap-3">
          <button
            type="button"
            onClick={reset}
            className="rounded-lg border border-ink-700 bg-ink-800 px-4 py-2 text-sm text-paper transition-colors hover:border-accent-dim hover:text-accent-bright"
          >
            Try again
          </button>
          <Link
            href="/"
            className="rounded-lg border border-ink-700 px-4 py-2 text-sm text-paper-dim transition-colors hover:border-accent-dim hover:text-accent-bright"
          >
            Today&apos;s board
          </Link>
        </div>
      </Card>
    </Container>
  );
}
