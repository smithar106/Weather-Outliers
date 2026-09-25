import { AgentClient } from "@/components/AgentClient";

// Rendered per request so the canonical/og:url metadata resolves against the
// runtime NEXT_PUBLIC_SITE_URL — the build has no environment variables.
export const dynamic = "force-dynamic";

export const metadata = {
  title: "How's the agent doing?",
  description:
    "Ask questions about the pipeline's recent runs, answered from the MLflow traces each run produced.",
  alternates: { canonical: "/agent" },
  openGraph: { url: "/agent" },
};

export default function AgentPage() {
  return <AgentClient />;
}
