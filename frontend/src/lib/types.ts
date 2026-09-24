/**
 * TypeScript mirror of `backend/app/schemas.py`.
 *
 * Hand-written rather than generated, and deliberately field-for-field: the
 * backend's own rule is that nothing is renamed on the way out, so nothing is
 * renamed here either. A reader can diff this file against the Pydantic models
 * and see immediately whether the two have drifted.
 *
 * Optional-versus-nullable matters. The backend emits explicit `null` for an
 * unavailable number rather than omitting the key, because "we did not measure
 * this" and "this is zero" must never collapse into the same rendering.
 */

// ---------------------------------------------------------------------------
// Enumerations (string unions rather than TS enums, matching the wire format)
// ---------------------------------------------------------------------------

export type MetricId =
  | "temp_max"
  | "temp_min"
  | "temp_mean"
  | "precipitation"
  | "wind_gust";

/** Drives marker colour and card badges. Mirrors `METRIC_CATEGORY`. */
export type CategoryId = "heat" | "cold" | "temperature" | "precipitation" | "wind";

export type Direction = "above" | "below";

/** `final` = reanalysis archive; `provisional` = near-real-time and revisable. */
export type DataTier = "provisional" | "final";

/**
 * What the numbers physically are. `reanalysis` and `model_analysis` are model
 * estimates at a grid cell and must never be rendered as station readings.
 */
export type ObservationType = "reanalysis" | "model_analysis" | "station_observation";

export type DataQuality = "ok" | "incomplete" | "missing";

/** Who wrote an explanation: a language model, or the deterministic templates. */
export type Generator = "llm" | "template";

// ---------------------------------------------------------------------------
// Cities
// ---------------------------------------------------------------------------

export interface City {
  id: string;
  name: string;
  admin: string;
  country: string;
  region: string;
  latitude: number;
  longitude: number;
  timezone: string;
  population: number | null;
  population_source: string | null;
}

export interface CityList {
  registry_version: string;
  count: number;
  selection_criteria_url: string;
  cities: City[];
}

// ---------------------------------------------------------------------------
// Observations and baselines
// ---------------------------------------------------------------------------

export interface Observation {
  local_date: string;
  temp_max_c: number | null;
  temp_min_c: number | null;
  temp_mean_c: number | null;
  precipitation_mm: number | null;
  wind_gust_max_kmh: number | null;
  source_provider: string;
  source_dataset: string;
  observation_type: ObservationType;
  data_tier: DataTier;
  data_quality: DataQuality;
  missing_fields: string[] | null;
  grid_latitude: number | null;
  grid_longitude: number | null;
  grid_elevation_m: number | null;
  utc_offset_seconds: number | null;
  retrieved_at: string;
}

/**
 * Bin counts for the distribution chart. Not a probability density.
 *
 * `bin_edges` has one more entry than `counts`. `degenerate` is true when the
 * reference sample has no spread to bin, in which case a histogram should not be
 * drawn at all.
 */
export interface BaselineHistogram {
  bin_edges: number[];
  counts: number[];
  degenerate: boolean;
}

export interface Baseline {
  metric: MetricId;
  day_of_year: number;
  reference_period: string;
  seasonal_window_days: number;
  n_samples: number;
  n_years: number;
  sufficient: boolean;
  mean: number | null;
  std: number | null;
  median: number | null;
  p25: number | null;
  p75: number | null;
  iqr: number | null;
  min_value: number | null;
  max_value: number | null;
  histogram: BaselineHistogram | null;
  zero_fraction: number | null;
  nonzero_n: number | null;
  source_dataset: string;
  methodology_version: string;
  /** Always true: computed from a reanalysis archive, not official normals. */
  not_official_normals: boolean;
}

// ---------------------------------------------------------------------------
// Explanations
// ---------------------------------------------------------------------------

export interface Evidence {
  label: string;
  value: number | string;
  unit: string | null;
  /** Which agent tool returned the figure. Every number is traceable to one. */
  source_tool: string;
}

export interface Explanation {
  headline: string;
  statistical_explanation: string;
  historical_context: string;
  caveats: string;
  evidence: Evidence[];
  confidence: string;
  generator: Generator;
  llm_provider: string | null;
  model: string | null;
  tool_call_count: number;
  fallback_reason: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

export interface EventCalculation {
  deviation: number | null;
  robust_deviation: number | null;
  z_score: number | null;
  /**
   * False whenever the distribution does not justify reading the z-score as a
   * probability. When false, the UI must not present it as comparable across
   * metrics.
   */
  z_valid: boolean;
  percentile: number | null;
  tail_probability: number | null;
  /** True when the tail probability is capped by sample size, not measured. */
  tail_probability_is_bounded: boolean;
  beyond_baseline_sample: boolean;
  return_period_years: number | null;
  surprisal: number;
  margin_bonus: number;
  anomaly_score: number;
}

export interface EventBaseline {
  mean: number | null;
  median: number | null;
  std: number | null;
  p25: number | null;
  p75: number | null;
  min: number | null;
  max: number | null;
  n: number;
  sufficient: boolean;
}

export interface AnomalyEvent {
  id: string;
  city: City;
  local_date: string;
  metric: MetricId;
  metric_label: string;
  category: CategoryId;
  direction: Direction;
  observed_value: number;
  unit: string;
  baseline: EventBaseline;
  calculation: EventCalculation;
  data_tier: DataTier;
  data_quality: DataQuality;
  source_dataset: string;
  observation_type: ObservationType;
  methodology_version: string;
  explanation: Explanation | null;
  /** Always true / always false respectively. No record archive is consulted. */
  is_statistical_outlier: boolean;
  is_verified_official_record: boolean;
}

export interface AnomalyEventDetail extends AnomalyEvent {
  evidence: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Rankings
// ---------------------------------------------------------------------------

export interface RankedEvent {
  rank: number;
  score: number;
  event: AnomalyEvent;
}

export interface RunProvenance {
  run_id: string;
  analysis_date: string;
  data_tier: DataTier | null;
  published_at: string | null;
  started_at: string;
  finished_at: string | null;
  cities_total: number;
  cities_with_data: number;
  completeness: number | null;
  events_total: number;
  events_published: number;
  methodology_version: string;
  registry_version: string | null;
  llm_calls: number;
  llm_estimated_usd: number;
  llm_budget_exhausted: boolean;
}

export interface DataSource {
  name: string;
  url: string;
  dataset: string;
  licence: string;
  attribution: string;
  observation_type: string;
  note: string | null;
}

export interface Rankings {
  analysis_date: string;
  published_at: string | null;
  methodology_version: string;
  /** Fixed label. Never "records". */
  result_type: "statistical_outliers";
  ranking_basis: string;
  one_event_per_city: boolean;
  count: number;
  events: RankedEvent[];
  run: RunProvenance;
  data_sources: DataSource[];
  /** False when an earlier board was served because the request had none. */
  is_latest_available: boolean;
  requested_date: string | null;
}

export interface ArchiveEntry {
  analysis_date: string;
  published_at: string | null;
  methodology_version: string;
  event_count: number;
  top_city: string | null;
  top_metric: MetricId | null;
  top_score: number | null;
}

export interface Archive {
  count: number;
  total: number;
  limit: number;
  offset: number;
  entries: ArchiveEntry[];
}

// ---------------------------------------------------------------------------
// City detail and history
// ---------------------------------------------------------------------------

export interface CityDetail {
  city: City;
  analysis_date: string | null;
  latest_observation: Observation | null;
  baselines: Baseline[];
  events: AnomalyEvent[];
  limitations: string[];
}

export interface CityHistoryPoint {
  local_date: string;
  temp_max_c: number | null;
  temp_min_c: number | null;
  temp_mean_c: number | null;
  precipitation_mm: number | null;
  wind_gust_max_kmh: number | null;
  data_tier: DataTier;
  data_quality: DataQuality;
}

export interface CityHistory {
  city: City;
  start_date: string;
  end_date: string;
  count: number;
  points: CityHistoryPoint[];
  source_datasets: string[];
}

// ---------------------------------------------------------------------------
// Methodology and health
// ---------------------------------------------------------------------------

export interface MethodologyMetric {
  metric: MetricId;
  label: string;
  unit: string;
  category: CategoryId;
  /** `both` or `upper` — precipitation and wind gusts are upper-tail only. */
  tails: "both" | "upper";
  /** False where the seasonal distribution does not justify publishing one. */
  z_score_published: boolean;
  /** `mean` or `median`, depending on the metric's skew. */
  deviation_reference: string;
  distribution_model: string;
}

/** The `ai` block of the methodology payload, as the backend assembles it. */
export interface MethodologyAi {
  explanations_enabled: boolean;
  llm_enabled: boolean;
  generator_default: Generator;
  provider: string;
  model: string | null;
  /** Always true: explanations are written once by the pipeline, never per visit. */
  precomputed: boolean;
  precompute_note: string;
  tools: string[];
  bounds: {
    max_tool_calls_per_event: number;
    max_iterations_per_event: number;
    max_retries_per_event: number;
    timeout_seconds_per_event: number;
    events_investigated: number;
    monthly_max_llm_calls: number;
    monthly_usd_budget: number;
  };
  grounding: string;
  fallback: string;
}

export interface Methodology {
  methodology_version: string;
  registry_version: string | null;
  reference_period: string;
  seasonal_window_days: number;
  seasonal_window_day_count: number;
  calendar: string;
  leap_day_handling: string;
  min_samples: number;
  min_years: number;
  min_wet_days: number;
  ranking_top_n: number;
  one_event_per_city: boolean;
  ranking_basis: string;
  tiebreak_chain: string[];
  score_formula: string;
  metrics: MethodologyMetric[];
  data_sources: DataSource[];
  limitations: string[];
  ai: MethodologyAi;
}

export interface Health {
  status: "ok" | "degraded";
  environment: string;
  methodology_version: string;
  database: string;
  cities: number | null;
  latest_published_date: string | null;
  latest_published_at: string | null;
  hours_since_publish: number | null;
  llm_enabled: boolean;
}

// ---------------------------------------------------------------------------
// Evaluation report (`evals/reports/latest.json`)
// ---------------------------------------------------------------------------

export type SuiteStatus = "passed" | "failed" | "error" | "skipped";

export interface EvalMetric {
  label: string;
  value: number | string | null;
  unit: string | null;
  detail: string | null;
}

export interface EvalCase {
  id: string;
  title: string;
  passed: boolean;
  category: string;
  expected: string;
  observed: string;
  detail: string | null;
}

export interface EvalSuite {
  id: string;
  title: string;
  description: string;
  status: SuiteStatus;
  duration_ms: number;
  cases_total: number;
  cases_passed: number;
  cases: EvalCase[];
  metrics: EvalMetric[];
  notes: string[];
  error: string | null;
}

export interface EvalReport {
  generated_at: string;
  git_commit: string | null;
  git_dirty: boolean;
  methodology_version: string;
  status: SuiteStatus;
  environment: Record<string, string | number | boolean | null>;
  totals: {
    suites_total: number;
    suites_passed: number;
    cases_total: number;
    cases_passed: number;
    duration_ms: number;
  };
  suites: EvalSuite[];
  limitations: string[];
}

// ---------------------------------------------------------------------------
// Monitoring (pipeline runs, evaluations, MLflow traces)
// ---------------------------------------------------------------------------

export interface MonitorRun {
  run_id: string;
  kind: string;
  analysis_date: string | null;
  status: string;
  data_tier: string | null;
  published: boolean;
  started_at: string | null;
  duration_ms: number | null;
  cities_total: number;
  cities_with_data: number;
  completeness: number | null;
  events_total: number;
  events_published: number;
  llm_calls: number;
  llm_estimated_usd: number;
  error: string | null;
  error_type: string | null;
}

export interface MonitorRuns {
  count: number;
  runs: MonitorRun[];
}

export interface MonitorEval {
  id: number;
  generated_at: string | null;
  status: string;
  git_commit: string | null;
  git_dirty: boolean;
  suites_total: number;
  suites_passed: number;
  cases_total: number;
  cases_passed: number;
  duration_ms: number;
}

export interface MonitorEvals {
  count: number;
  reports: MonitorEval[];
}

export interface MonitorSpan {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  span_type: string | null;
  status: string;
  latency_ms: number | null;
  attributes: Record<string, unknown>;
}

export interface MonitorTrace {
  trace_id: string;
  timestamp_ms: number | null;
  status: string;
  duration_ms: number | null;
  root_span: string | null;
  span_count: number;
  model: string | null;
  methodology_version: string | null;
  prompt_version: string | null;
  run_id: string | null;
}

export interface MonitorTraceDetail {
  trace_id: string;
  timestamp_ms: number | null;
  status: string;
  duration_ms: number | null;
  spans: MonitorSpan[];
}

export interface MonitorTraces {
  /** False when the tracking store is not configured or unreachable. */
  available: boolean;
  note: string | null;
  count: number;
  traces: MonitorTrace[];
}

// ---------------------------------------------------------------------------
// Agent chat (trace questions)
// ---------------------------------------------------------------------------

export interface AgentChatResponse {
  question: string;
  answer: string;
  /** How many traces the answer was grounded in. */
  trace_count: number;
  /** False when the tracking store is not configured or unreachable. */
  available: boolean;
  note: string | null;
}

export interface AgentStatus {
  available: boolean;
  level: "ok" | "degraded" | "unknown";
  label: string;
  detail: string | null;
  runs: number;
}
