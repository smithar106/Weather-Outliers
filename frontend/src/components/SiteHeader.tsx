"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

const NAV = [
  { href: "/", label: "Today" },
  { href: "/map", label: "Map" },
  { href: "/archive", label: "Archive" },
  { href: "/ask", label: "Ask the data" },
] as const;

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function SiteHeader() {
  const pathname = usePathname() ?? "/";
  const [open, setOpen] = useState(false);

  return (
    <header className="sticky top-0 z-40 border-b border-ink-700/70 bg-ink-950/85 backdrop-blur-xl">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-6 px-5 sm:px-8">
        <Link
          href="/"
          className="group flex items-center gap-3"
          onClick={() => setOpen(false)}
          aria-label="Weather Outliers home"
        >
          <Glyph />
          <span className="flex flex-col leading-none">
            <span className="font-display text-[1.0625rem] font-semibold tracking-tight text-paper">
              Weather Outliers
            </span>
            <span className="mt-0.5 text-[0.6875rem] tracking-[0.13em] text-paper-faint uppercase">
              Statistical anomaly desk
            </span>
          </span>
        </Link>

        <nav className="hidden items-center gap-1 md:flex" aria-label="Main">
          {NAV.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`rounded-lg px-3 py-2 text-sm transition-colors ${
                  active
                    ? "bg-ink-800 text-paper"
                    : "text-paper-muted hover:bg-ink-850 hover:text-paper"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="rounded-lg border border-ink-700 p-2 text-paper-dim md:hidden"
          aria-expanded={open}
          aria-controls="mobile-nav"
          aria-label={open ? "Close menu" : "Open menu"}
        >
          <svg viewBox="0 0 20 20" className="size-5" fill="none" stroke="currentColor" strokeWidth="1.6">
            {open ? (
              <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
            ) : (
              <path d="M3 6h14M3 10h14M3 14h14" strokeLinecap="round" />
            )}
          </svg>
        </button>
      </div>

      {open && (
        <nav
          id="mobile-nav"
          className="border-t border-ink-700/70 bg-ink-950/95 px-5 pb-4 pt-2 md:hidden"
          aria-label="Main"
        >
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              onClick={() => setOpen(false)}
              aria-current={isActive(pathname, item.href) ? "page" : undefined}
              className={`block rounded-lg px-3 py-2.5 text-sm ${
                isActive(pathname, item.href)
                  ? "bg-ink-800 text-paper"
                  : "text-paper-muted hover:text-paper"
              }`}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      )}
    </header>
  );
}

/**
 * A distribution with a marked tail — the site's one idea, as a mark. The tall
 * bars are the bulk of a seasonal sample; the lit bar on the right is the outlier.
 */
function Glyph() {
  return (
    <svg
      viewBox="0 0 28 28"
      className="size-8 shrink-0"
      aria-hidden="true"
      role="presentation"
    >
      <rect x="0.5" y="0.5" width="27" height="27" rx="8" className="fill-ink-850 stroke-ink-700" />
      <g className="fill-accent-dim">
        <rect x="5" y="17" width="2.4" height="6" rx="0.9" />
        <rect x="8.6" y="12" width="2.4" height="11" rx="0.9" />
        <rect x="12.2" y="9" width="2.4" height="14" rx="0.9" />
        <rect x="15.8" y="14" width="2.4" height="9" rx="0.9" />
      </g>
      <rect x="19.4" y="6" width="2.4" height="17" rx="0.9" className="fill-accent" />
    </svg>
  );
}
