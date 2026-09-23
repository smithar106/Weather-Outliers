/**
 * Shared presentation primitives.
 *
 * Small and unopinionated on purpose. The interesting components in this project
 * are the ones that render statistics honestly; these just keep spacing, borders
 * and label typography from being reinvented on six pages.
 */

import type { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

export function Container({
  children,
  className = "",
  size = "wide",
}: {
  children: ReactNode;
  className?: string;
  size?: "wide" | "prose";
}) {
  const max = size === "prose" ? "max-w-3xl" : "max-w-7xl";
  return <div className={`mx-auto ${max} px-5 sm:px-8 ${className}`}>{children}</div>;
}

export function Section({
  children,
  className = "",
  id,
}: {
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section id={id} className={`scroll-mt-24 ${className}`}>
      {children}
    </section>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  description,
  actions,
  as: Heading = "h2",
}: {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  as?: "h1" | "h2" | "h3";
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="max-w-2xl">
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <Heading
          className={`font-display tracking-tight text-balance text-paper ${
            Heading === "h1" ? "mt-2 text-3xl sm:text-4xl" : "mt-1.5 text-2xl"
          }`}
        >
          {title}
        </Heading>
        {description && (
          <p className="mt-2.5 text-[0.9375rem] leading-relaxed text-paper-muted">{description}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Card({
  children,
  className = "",
  as: Element = "div",
}: {
  children: ReactNode;
  className?: string;
  as?: "div" | "li" | "article";
}) {
  return <Element className={`surface ${className}`}>{children}</Element>;
}

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

export function Badge({
  children,
  className = "",
  title,
}: {
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[0.6875rem] font-semibold tracking-wide ring-1 ring-inset ${
        className || "bg-ink-800 text-paper-dim ring-ink-700"
      }`}
    >
      {children}
    </span>
  );
}

export function Dot({ color }: { color: string }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block size-1.5 shrink-0 rounded-full"
      style={{ backgroundColor: color }}
    />
  );
}

/**
 * A labelled figure. `hint` is for the caveat that has to travel with the number —
 * the sample size behind a probability, or why a z-score is absent.
 */
export function Stat({
  label,
  value,
  hint,
  tone = "default",
  className = "",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "default" | "muted" | "accent";
  className?: string;
}) {
  const valueTone =
    tone === "accent" ? "text-accent-bright" : tone === "muted" ? "text-paper-dim" : "text-paper";
  return (
    <div className={className}>
      <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
        {label}
      </dt>
      <dd className={`tnum mt-1.5 text-lg ${valueTone}`}>{value}</dd>
      {hint && <p className="mt-1 text-xs leading-snug text-paper-faint">{hint}</p>}
    </div>
  );
}

/**
 * A dominant metric: the number leads, the label explains it, the note trails it.
 *
 * This is the pattern the whole site should use for headline figures — a large
 * value with its explanation beside it, not a value trapped inside a card with a
 * caption that competes for attention.
 */
export function Metric({
  label,
  value,
  note,
  accent,
  className = "",
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  accent?: string;
  className?: string;
}) {
  return (
    <div className={className}>
      <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.12em] text-paper-faint">
        {label}
      </p>
      <p
        className="tnum mt-1.5 text-[2rem] leading-none tracking-tight sm:text-[2.5rem]"
        style={accent ? { color: accent } : undefined}
      >
        {value}
      </p>
      {note && <p className="mt-2 text-sm leading-snug text-paper-muted">{note}</p>}
    </div>
  );
}

/** A compact key/value row, used wherever provenance is listed. */
export function DefRow({
  term,
  children,
  mono = false,
}: {
  term: string;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-b border-ink-800 py-2.5 last:border-0">
      <dt className="text-sm text-paper-muted">{term}</dt>
      {/*
       * `mono` rows carry values the provider chose, not values we wrote:
       * `open_meteo_best_match_analysis`, `era5_seasonal_window`. They contain
       * no space to break on, so without `overflow-wrap: anywhere` they run
       * past the card edge on a narrow phone instead of wrapping.
       */}
      <dd
        className={`min-w-0 text-sm text-paper-dim ${
          mono ? "tnum [overflow-wrap:anywhere]" : ""
        }`}
      >
        {children}
      </dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Notices
// ---------------------------------------------------------------------------

const CALLOUT_TONES = {
  neutral: "border-ink-700 bg-ink-850/70 text-paper-dim",
  info: "border-accent-dim/60 bg-accent/5 text-paper-dim",
  warning: "border-warning/35 bg-warning/[0.06] text-paper-dim",
  danger: "border-negative/35 bg-negative/[0.06] text-paper-dim",
} as const;

export function Callout({
  children,
  title,
  tone = "neutral",
  className = "",
}: {
  children: ReactNode;
  title?: string;
  tone?: keyof typeof CALLOUT_TONES;
  className?: string;
}) {
  return (
    <div className={`rounded-xl border px-4 py-3.5 ${CALLOUT_TONES[tone]} ${className}`}>
      {title && <p className="text-sm font-semibold text-paper">{title}</p>}
      <div className={`text-sm leading-relaxed ${title ? "mt-1.5" : ""}`}>{children}</div>
    </div>
  );
}

/**
 * The page-level failure state.
 *
 * Says what could not be loaded and what the reader can do, and never invents a
 * board. A site whose premise is transparent statistics cannot show placeholder
 * numbers when the backend is unreachable.
 */
export function LoadFailure({
  what,
  detail,
  children,
}: {
  what: string;
  detail?: string;
  children?: ReactNode;
}) {
  return (
    <Container className="py-20">
      <Card className="mx-auto max-w-2xl p-8">
        <p className="eyebrow">Data unavailable</p>
        <h1 className="mt-2 font-display text-2xl tracking-tight text-paper">
          {what} could not be loaded.
        </h1>
        <p className="mt-3 text-[0.9375rem] leading-relaxed text-paper-muted">
          Nothing is shown rather than something approximate. If the daily pipeline has failed, the
          most recent successful analysis is normally served instead, so this usually means the API
          itself is unreachable.
        </p>
        {detail && (
          <p className="tnum mt-4 rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-xs text-paper-faint">
            {detail}
          </p>
        )}
        {children && <div className="mt-6">{children}</div>}
      </Card>
    </Container>
  );
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <Card className="p-8">
      <p className="font-display text-lg text-paper">{title}</p>
      {children && (
        <p className="mt-2 max-w-md text-sm leading-relaxed text-paper-muted">{children}</p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </Card>
  );
}
