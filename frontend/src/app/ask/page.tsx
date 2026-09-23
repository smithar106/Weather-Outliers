import { AskClient } from "@/components/AskClient";

export const metadata = {
  title: "Ask the data",
  description:
    "Ask questions about the weather outliers in plain language, answered by a read-only query over the application's data.",
};

export default function AskPage() {
  return <AskClient />;
}
