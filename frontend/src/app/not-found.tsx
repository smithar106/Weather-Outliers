import Link from "next/link";

import { Card, Container } from "@/components/ui";

export const metadata = { title: "Not found", robots: { index: false, follow: false } };

export default function NotFound() {
  return (
    <Container className="py-24">
      <Card className="mx-auto max-w-2xl p-8">
        <p className="eyebrow">404</p>
        <h1 className="mt-2 font-display text-2xl tracking-tight text-paper">
          There is nothing published at this address.
        </h1>
        <p className="mt-3 text-[0.9375rem] leading-relaxed text-paper-muted">
          If you were looking for a specific date, only dates with a successfully published run
          exist — a day the pipeline failed on has no board, by design, because a failed run never
          overwrites or invents one.
        </p>
        <div className="mt-6 flex flex-wrap gap-3">
          <Link
            href="/"
            className="rounded-lg border border-ink-700 bg-ink-800 px-4 py-2 text-sm text-paper transition-colors hover:border-accent-dim hover:text-accent-bright"
          >
            Today&apos;s board
          </Link>
          <Link
            href="/archive"
            className="rounded-lg border border-ink-700 px-4 py-2 text-sm text-paper-dim transition-colors hover:border-accent-dim hover:text-accent-bright"
          >
            Browse the archive
          </Link>
        </div>
      </Card>
    </Container>
  );
}
