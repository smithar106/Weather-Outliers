/**
 * Server-side API client.
 *
 * Every fetch in this file runs on the server. That is the whole architecture in
 * one sentence: the browser talks only to Next.js, Next.js talks to FastAPI, and
 * FastAPI reads rows the scheduled pipeline already wrote. A visitor therefore
 * cannot trigger a weather-provider request or a model call, and no provider
 * credential ever needs to reach the client bundle.
 *
 * `API_BASE_URL` should point at the backend's private address in production
 * (`http://<service>.railway.internal:8000` on Railway — the host follows the
 * service's name), because nothing here needs the public internet.
 * `NEXT_PUBLIC_API_BASE_URL` is separate and is used only to build links a human
 * can click; it is never fetched from.
 */

import type {
  AgentStatus,
  Archive,
  AnomalyEventDetail,
  CityDetail,
  CityHistory,
  CityList,
  Health,
  Methodology,
  MonitorEvals,
  MonitorRuns,
  MonitorTraceDetail,
  MonitorTraces,
  Rankings,
} from "@/lib/types";

/**
 * Accept a bare host as well as a full origin.
 *
 * This value is typed into a deployment dashboard by hand, and a missing scheme
 * is the likely mistake: `new URL("example.up.railway.app/api/...")` throws
 * `Invalid URL`, which is indistinguishable from the backend being down and says
 * nothing about the cause. So a schemeless value gets one inferred rather than
 * failing every page — `http` for loopback and for Railway's private network,
 * which is not TLS-terminated, and `https` for anything else, because a public
 * ingress host always is.
 */
function normalizeBaseUrl(raw: string): string {
  // Strip trailing characters that cannot appear in a URL at all. Copying a
  // domain out of a hosting dashboard tends to bring the link glyph with it —
  // `weather-outliers-app.up.railway.app↗` was a real value here, and it
  // fails as an unreachable API with no hint that the host has a stray arrow on
  // the end. Leading/trailing whitespace, NBSP and zero-width characters come
  // from the same place. The cost of this is that a genuine IDN host ending in a
  // non-ASCII character would be truncated; nothing here has one, and an ASCII
  // host is what every deployment target issues.
  const value = raw
    .replace(/[^\x21-\x7e]+$/u, "")
    .trim()
    .replace(/\/+$/, "");
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) return value;
  const plaintext = /^(localhost|127\.0\.0\.1|\[::1\]|[^/]+\.railway\.internal)(:\d+)?$/i;
  return `${plaintext.test(value) ? "http" : "https"}://${value}`;
}

export const API_BASE_URL = normalizeBaseUrl(
  process.env.API_BASE_URL ??
    process.env.NEXT_PUBLIC_API_BASE_URL ??
    "http://127.0.0.1:8000",
);

/** Public, browser-facing base URL. Display only — see the note above. */
export const PUBLIC_API_BASE_URL = normalizeBaseUrl(
  process.env.NEXT_PUBLIC_API_BASE_URL ?? API_BASE_URL,
);

/**
 * How long a page may serve a cached copy before revalidating.
 *
 * The pipeline publishes once a day, so a short window would cost requests
 * without ever finding new data. Ten minutes is short enough that a manual
 * backfill or a late run appears promptly and long enough that a traffic spike
 * does not reach the database at all.
 */
export const REVALIDATE_SECONDS = 600;

/** Meta endpoints describe the deployment, so they are not cached as long. */
export const REVALIDATE_SECONDS_META = 60;

const REQUEST_TIMEOUT_MS = 10_000;

export class ApiError extends Error {
  readonly status: number;
  readonly path: string;

  constructor(path: string, status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
  }
}

/** Distinguishes "the backend said no such thing" from "the backend is down". */
export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

interface GetOptions {
  /** Query parameters. Undefined and null values are dropped. */
  params?: Record<string, string | number | boolean | undefined | null>;
  revalidate?: number;
  /** Cache tag, so a future webhook could revalidate a single resource. */
  tags?: string[];
}

function buildUrl(path: string, params: GetOptions["params"]): string {
  const url = new URL(`${API_BASE_URL}${path}`);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null) {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

async function get<T>(path: string, options: GetOptions = {}): Promise<T> {
  const url = buildUrl(path, options.params);
  const revalidate = options.revalidate ?? REVALIDATE_SECONDS;

  let response: Response;
  try {
    response = await fetch(url, {
      // An unreachable backend must surface as a handled error on the page, not
      // as a request that hangs until the platform kills it.
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      headers: { Accept: "application/json" },
      next: { revalidate, tags: options.tags },
    });
  } catch (cause) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    throw new ApiError(path, 0, `could not reach the API at ${API_BASE_URL}: ${reason}`);
  }

  if (!response.ok) {
    // The backend returns a structured ErrorOut; fall back to the status text if
    // the failure happened before the handler ran.
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string; error?: string };
      detail = body.detail ?? body.error ?? detail;
    } catch {
      /* non-JSON error body; the status text is the best available message */
    }
    throw new ApiError(path, response.status, detail);
  }

  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export function getHealth(): Promise<Health> {
  return get<Health>("/health", { revalidate: REVALIDATE_SECONDS_META });
}

/**
 * The most recent published board.
 *
 * If today's pipeline run failed, this is yesterday's board rather than an error
 * page — a failed run never unpublishes a good one. `analysis_date` on the result
 * is what the page must display, and it is always the date of the data actually
 * being shown.
 */
export function getLatestRankings(): Promise<Rankings> {
  return get<Rankings>("/api/rankings/latest", { tags: ["rankings"] });
}

export function getRankingsForDate(analysisDate: string): Promise<Rankings> {
  return get<Rankings>(`/api/rankings/${analysisDate}`, {
    tags: ["rankings", `rankings:${analysisDate}`],
  });
}

export function getArchive(options: { limit?: number; offset?: number } = {}): Promise<Archive> {
  return get<Archive>("/api/rankings", {
    params: { limit: options.limit, offset: options.offset },
    tags: ["archive"],
  });
}

export function getCities(options: { country?: string } = {}): Promise<CityList> {
  return get<CityList>("/api/cities", { params: { country: options.country }, tags: ["cities"] });
}

export function getCity(cityId: string, analysisDate?: string): Promise<CityDetail> {
  return get<CityDetail>(`/api/cities/${encodeURIComponent(cityId)}`, {
    params: { analysis_date: analysisDate },
    tags: ["cities", `city:${cityId}`],
  });
}

export function getCityHistory(
  cityId: string,
  options: { days?: number; end?: string } = {}
): Promise<CityHistory> {
  return get<CityHistory>(`/api/cities/${encodeURIComponent(cityId)}/history`, {
    params: { days: options.days, end: options.end },
    tags: [`city:${cityId}`],
  });
}

export function getEvent(eventId: string): Promise<AnomalyEventDetail> {
  return get<AnomalyEventDetail>(`/api/events/${encodeURIComponent(eventId)}`, {
    tags: [`event:${eventId}`],
  });
}

export function getMethodology(): Promise<Methodology> {
  return get<Methodology>("/api/methodology", {
    revalidate: REVALIDATE_SECONDS_META,
    tags: ["methodology"],
  });
}

// ---------------------------------------------------------------------------
// Monitoring
// ---------------------------------------------------------------------------

export function getMonitorRuns(limit = 20): Promise<MonitorRuns> {
  return get<MonitorRuns>("/api/monitor/runs", {
    params: { limit },
    revalidate: REVALIDATE_SECONDS_META,
    tags: ["monitor", "monitor:runs"],
  });
}

export function getMonitorEvals(limit = 20): Promise<MonitorEvals> {
  return get<MonitorEvals>("/api/monitor/evals", {
    params: { limit },
    revalidate: REVALIDATE_SECONDS_META,
    tags: ["monitor", "monitor:evals"],
  });
}

export function getMonitorTraces(limit = 50): Promise<MonitorTraces> {
  return get<MonitorTraces>("/api/monitor/traces", {
    params: { limit },
    revalidate: REVALIDATE_SECONDS_META,
    tags: ["monitor", "monitor:traces"],
  });
}

export function getAgentStatus(): Promise<AgentStatus> {
  return get<AgentStatus>("/api/monitor/agent-status", {
    revalidate: REVALIDATE_SECONDS_META,
    tags: ["monitor", "monitor:agent-status"],
  });
}

export function getMonitorTrace(traceId: string): Promise<MonitorTraceDetail> {
  return get<MonitorTraceDetail>(`/api/monitor/traces/${encodeURIComponent(traceId)}`, {
    revalidate: REVALIDATE_SECONDS_META,
    tags: [`monitor:trace:${traceId}`],
  });
}
