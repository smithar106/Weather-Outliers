"use client";

/**
 * Interactive North America map of the ranked cities.
 *
 * Tiles come from Mapbox. Unlike the rest of this project — which keeps every
 * credential server-side on principle — a Mapbox access token is a *public*
 * token (`pk.…`): it is designed to sit in a browser bundle, identifies the
 * account for billing and map-style access, and cannot be used to administer the
 * account. It is read server-side at request time and passed down as a prop, so
 * it is never committed to source and never inlined at build time.
 *
 * All data arrives as props from a server component. The map never calls the API.
 */

import "mapbox-gl/dist/mapbox-gl.css";

import mapboxgl from "mapbox-gl";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { Badge, Card, Dot } from "@/components/ui";
import {
  CATEGORY_STYLES,
  categoryStyle,
  cityLabel,
  countryName,
  describeRarity,
  directionWord,
  formatDeviation,
  formatMeasurement,
  formatNumber,
  formatPercentile,
  metricShortLabel,
} from "@/lib/format";
import type { CategoryId, RankedEvent } from "@/lib/types";

/** The light basemap, chosen to sit against the site's near-white ground. */
const STYLE_URL = "mapbox://styles/mapbox/light-v11";

/** Fallback for an empty board: a loose North America framing. */
const FALLBACK_BOUNDS: [[number, number], [number, number]] = [
  [-128, 16],
  [-62, 57],
];

/**
 * The bounding box that holds every ranked marker, with breathing room.
 *
 * A fixed continental frame clips the far cities: Honolulu sits at −158° and
 * St. John's at −53°, Anchorage at 61° and Mérida at 19°, so no single hardcoded
 * box holds all of them without drowning the rest in empty ocean. Fitting to the
 * events actually on the board means no marker starts off-screen, and the frame
 * follows the data rather than an assumption about which cities will rank.
 */
function fitBoundsFor(events: RankedEvent[]): [[number, number], [number, number]] {
  if (events.length === 0) return FALLBACK_BOUNDS;
  const lngs = events.map((ranked) => ranked.event.city.longitude);
  const lats = events.map((ranked) => ranked.event.city.latitude);
  const minLng = Math.min(...lngs);
  const maxLng = Math.max(...lngs);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  // A percentage pad so a tight cluster does not sit on the frame edge, floored
  // at four degrees so a single-city or two-city board still gets a usable view.
  const lngPad = Math.max(4, (maxLng - minLng) * 0.12);
  const latPad = Math.max(4, (maxLat - minLat) * 0.12);
  return [
    [minLng - lngPad, minLat - latPad],
    [maxLng + lngPad, maxLat + latPad],
  ];
}

export function AnomalyMap({
  events,
  accessToken,
}: {
  events: RankedEvent[];
  accessToken: string | null;
}) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<mapboxgl.Map | null>(null);
  const markersRef = useRef<mapboxgl.Marker[]>([]);
  const [selectedRank, setSelectedRank] = useState<number | null>(
    events.length > 0 ? events[0].rank : null
  );
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState<string | null>(
    accessToken ? null : "no Mapbox access token is configured"
  );

  const byRank = useMemo(
    () => new Map(events.map((ranked) => [ranked.rank, ranked])),
    [events]
  );
  const selected = selectedRank === null ? null : byRank.get(selectedRank) ?? null;
  const initialBounds = useMemo(() => fitBoundsFor(events), [events]);

  // -- initialise once -----------------------------------------------------
  useEffect(() => {
    if (!container.current || mapRef.current || !accessToken) return;

    mapboxgl.accessToken = accessToken;

    let map: mapboxgl.Map;
    try {
      map = new mapboxgl.Map({
        container: container.current,
        style: STYLE_URL,
        bounds: initialBounds,
        fitBoundsOptions: { padding: 48, maxZoom: 6 },
        attributionControl: false,
        // The board is fifty cities on one continent; tilt and rotation add
        // nothing and make the markers harder to compare.
        pitchWithRotate: false,
        dragRotate: false,
        touchZoomRotate: true,
      });
    } catch (error) {
      // Surfaced on a microtask rather than inline. Setting state synchronously in
      // an effect body triggers a cascading render, and the construction failure
      // itself comes from an external system — a browser without usable WebGL —
      // so a callback is the right place to report it.
      const message =
        error instanceof Error ? error.message : "the map could not be initialised";
      queueMicrotask(() => setFailed((current) => current ?? message));
      return;
    }

    mapRef.current = map;
    map.addControl(
      new mapboxgl.AttributionControl({
        compact: true,
      }),
      "bottom-right"
    );
    map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right");
    map.on("load", () => setReady(true));
    map.on("error", (event) => {
      // A tile or glyph failure should degrade to the list beside the map rather
      // than leaving a blank rectangle with no explanation.
      const message = event.error?.message ?? "the tile provider could not be reached";
      setFailed((current) => current ?? message);
    });

    return () => {
      markersRef.current.forEach((marker) => marker.remove());
      markersRef.current = [];
      map.remove();
      mapRef.current = null;
    };
  }, [accessToken, initialBounds]);

  // -- markers -------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    markersRef.current.forEach((marker) => marker.remove());
    markersRef.current = events.map((ranked) => {
      const style = categoryStyle(ranked.event.category);
      const element = document.createElement("button");
      element.type = "button";
      element.setAttribute(
        "aria-label",
        `Rank ${ranked.rank}: ${cityLabel(ranked.event.city)}, ${style.label}`
      );
      element.style.cssText = [
        "display:grid",
        "place-items:center",
        "width:26px",
        "height:26px",
        "border-radius:9999px",
        `background:${style.color}`,
        "border:2px solid #ffffff",
        "box-shadow:0 0 0 1px rgba(15,23,42,0.14), 0 2px 6px rgba(15,23,42,0.28)",
        "color:#ffffff",
        "font:600 11px/1 ui-monospace, monospace",
        "cursor:pointer",
        "transition:transform 140ms ease",
      ].join(";");
      element.textContent = String(ranked.rank);
      element.addEventListener("click", () => {
        setSelectedRank(ranked.rank);
        map.easeTo({
          center: [ranked.event.city.longitude, ranked.event.city.latitude],
          zoom: Math.max(map.getZoom(), 4.2),
          duration: 600,
        });
      });
      element.addEventListener("mouseenter", () => {
        element.style.transform = "scale(1.18)";
      });
      element.addEventListener("mouseleave", () => {
        element.style.transform = "scale(1)";
      });

      return new mapboxgl.Marker({ element, anchor: "center" })
        .setLngLat([ranked.event.city.longitude, ranked.event.city.latitude])
        .addTo(map);
    });
  }, [events, ready]);

  const presentCategories = useMemo(() => {
    const seen = new Set<CategoryId>();
    events.forEach((ranked) => seen.add(ranked.event.category));
    return (Object.keys(CATEGORY_STYLES) as CategoryId[]).filter((key) => seen.has(key));
  }, [events]);

  return (
    <div className="grid gap-5 lg:grid-cols-[1.6fr_1fr]">
      <Card className="overflow-hidden">
        <div className="relative">
          <div
            ref={container}
            className="h-[26rem] w-full bg-ink-900 sm:h-[34rem]"
            aria-label="Map of ranked cities"
          />
          {/*
            The two test ids below are the handshake with scripts/capture-screenshots.mjs:
            the placeholder going away is how that script knows the basemap actually
            drew, and the failure panel appearing is how it knows to abort rather than
            publish a screenshot of an empty rectangle.
          */}
          {!ready && !failed && (
            <p
              data-testid="map-loading"
              className="pointer-events-none absolute inset-0 grid place-items-center text-sm text-paper-muted"
            >
              Loading map tiles…
            </p>
          )}
          {failed && (
            <div
              data-testid="map-failure"
              className="absolute inset-0 grid place-items-center bg-ink-900/95 p-6"
            >
              <div className="max-w-sm text-center">
                <p className="font-display text-lg text-paper">The map could not load.</p>
                <p className="mt-2 text-sm leading-relaxed text-paper-muted">
                  {failed === "no Mapbox access token is configured" ? (
                    <>Set <code className="font-mono text-xs">MAPBOX_TOKEN</code> to a public
                    Mapbox access token to render the basemap. The ranked events are listed
                    beside this panel and are unaffected.</>
                  ) : (
                    <>Tiles come from Mapbox, an external service. The ranked events are listed
                    beside this panel and are unaffected.</>
                  )}
                </p>
                {failed !== "no Mapbox access token is configured" && (
                  <p className="tnum mt-3 text-xs text-paper-faint">{failed}</p>
                )}
              </div>
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-ink-700 px-4 py-3">
          <span className="eyebrow">Category</span>
          {presentCategories.map((key) => (
            <span key={key} className="flex items-center gap-1.5 text-xs text-paper-dim">
              <Dot color={CATEGORY_STYLES[key].color} />
              {CATEGORY_STYLES[key].label}
            </span>
          ))}
          <span className="ml-auto text-xs text-paper-faint">
            Markers are numbered by rank. Positions are city coordinates, not the grid cell the
            data was sampled from.
          </span>
        </div>
      </Card>

      <div className="flex flex-col gap-4">
        {selected ? <SelectedPanel ranked={selected} /> : null}
        <Card className="overflow-hidden">
          <p className="eyebrow px-4 pt-4">Ranked cities</p>
          <ul className="mt-2 max-h-[22rem] divide-y divide-ink-800 overflow-y-auto">
            {events.map((ranked) => {
              const style = categoryStyle(ranked.event.category);
              const active = ranked.rank === selectedRank;
              return (
                <li key={ranked.event.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedRank(ranked.rank);
                      mapRef.current?.easeTo({
                        center: [ranked.event.city.longitude, ranked.event.city.latitude],
                        zoom: 4.6,
                        duration: 600,
                      });
                    }}
                    className={`flex w-full items-center gap-3 px-4 py-3 text-left transition-colors ${
                      active ? "bg-ink-800" : "hover:bg-ink-850"
                    }`}
                    aria-current={active ? "true" : undefined}
                  >
                    <span className="tnum w-6 shrink-0 text-sm" style={{ color: style.color }}>
                      {String(ranked.rank).padStart(2, "0")}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm text-paper">
                        {cityLabel(ranked.event.city)}
                      </span>
                      <span className="block truncate text-xs text-paper-faint">
                        {metricShortLabel(ranked.event.metric)} ·{" "}
                        {formatMeasurement(ranked.event.observed_value, ranked.event.unit)}
                      </span>
                    </span>
                    <span className="tnum shrink-0 text-xs text-paper-muted">
                      {formatNumber(ranked.event.calculation.anomaly_score, 2)}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </Card>
      </div>
    </div>
  );
}

function SelectedPanel({ ranked }: { ranked: RankedEvent }) {
  const { event } = ranked;
  const style = categoryStyle(event.category);

  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="eyebrow">Rank {ranked.rank}</p>
          <h3 className="mt-1 font-display text-xl leading-tight tracking-tight text-paper">
            {cityLabel(event.city)}
          </h3>
          <p className="mt-1 text-sm text-paper-muted">
            {countryName(event.city.country)} · {event.city.timezone}
          </p>
        </div>
        <Badge className={style.badge}>
          <Dot color={style.color} />
          {style.label}
        </Badge>
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4">
        <div>
          <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
            {metricShortLabel(event.metric)}
          </dt>
          <dd className="tnum mt-1 text-xl" style={{ color: style.color }}>
            {formatMeasurement(event.observed_value, event.unit)}
          </dd>
        </div>
        <div>
          <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
            vs baseline
          </dt>
          <dd className="tnum mt-1 text-xl text-paper-dim">
            {formatDeviation(event.calculation.deviation, event.unit)}
          </dd>
          <p className="mt-1 text-xs text-paper-faint">
            {directionWord(event.direction, event.category)}
          </p>
        </div>
        <div>
          <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
            Percentile
          </dt>
          <dd className="tnum mt-1 text-base text-paper-dim">
            {formatPercentile(event.calculation.percentile)}
          </dd>
        </div>
        <div>
          <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
            Score
          </dt>
          <dd className="tnum mt-1 text-base text-paper-dim">
            {formatNumber(event.calculation.anomaly_score, 2)}
          </dd>
        </div>
      </dl>

      <p className="mt-4 text-xs leading-relaxed text-paper-muted">
        {describeRarity(event.calculation)}
      </p>

      {event.explanation && (
        <p className="mt-3 border-l-2 border-ink-700 pl-3 text-sm leading-relaxed text-paper-dim">
          {event.explanation.headline}
        </p>
      )}

      <Link
        href={`/city/${event.city.id}`}
        className="link-underline mt-4 inline-block text-sm text-accent-bright"
      >
        Full breakdown for {event.city.name} →
      </Link>
    </Card>
  );
}
