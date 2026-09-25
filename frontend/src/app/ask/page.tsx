import { AskClient } from "@/components/AskClient";

// Rendered per request so the canonical/og:url metadata resolves against the
// runtime NEXT_PUBLIC_SITE_URL — the build has no environment variables.
export const dynamic = "force-dynamic";

export const metadata = {
  title: "Ask the data",
  description:
    "Ask questions about the weather outliers in plain language, answered by a read-only query over the application's data.",
  alternates: { canonical: "/ask" },
  openGraph: { url: "/ask" },
};

export default function AskPage() {
  return <AskClient />;
}
