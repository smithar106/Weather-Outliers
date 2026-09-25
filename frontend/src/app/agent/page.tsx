import { AgentClient } from "@/components/AgentClient";

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
