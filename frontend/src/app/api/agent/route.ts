import { API_BASE_URL } from "@/lib/api";

/**
 * Server-side proxy for the backend trace-question endpoint, mirroring
 * `app/api/ask/route.ts`: the browser talks only to this route, which attaches
 * `CHAT_API_KEY` from the server environment so the key never reaches the client.
 */
export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json(
      { error: "invalid_request", detail: "The request body must be JSON." },
      { status: 422 }
    );
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  const key = process.env.CHAT_API_KEY;
  if (key) headers["X-API-Key"] = key;

  let upstream: Response;
  try {
    upstream = await fetch(`${API_BASE_URL}/api/agent`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(20_000),
    });
  } catch {
    return Response.json(
      { error: "unavailable", detail: "The analysis service could not be reached. Try again shortly." },
      { status: 503 }
    );
  }

  const data = await upstream.json().catch(() => null);
  return Response.json(
    data ?? { error: "error", detail: "Unexpected response from the analysis service." },
    { status: upstream.status }
  );
}
