import { ImageResponse } from "next/og";

/**
 * The social-preview image, generated from JSX at request time rather than
 * committed as a binary. It shares the site's near-white ground and the
 * distribution-with-a-marked-tail mark, so a shared link is recognisably this
 * project rather than a blank `summary_large_image` card.
 */

export const alt =
  "Weather Outliers — yesterday's most unusual North American weather";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          backgroundColor: "#f6f8fa",
          padding: "64px 72px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center" }}>
          <Glyph />
          <div style={{ marginLeft: 16, fontSize: 30, fontWeight: 600, color: "#0f1c2e" }}>
            Weather Outliers
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column" }}>
          <div style={{ fontSize: 64, lineHeight: 1.08, fontWeight: 600, color: "#0f1c2e" }}>
            Yesterday was anything but normal.
          </div>
          <div style={{ marginTop: 20, fontSize: 28, color: "#57657d" }}>
            The ten most statistically unusual weather events across 50 North American cities,
            ranked against a 30-year seasonal baseline.
          </div>
        </div>
      </div>
    ),
    { width: 1200, height: 630 }
  );
}

function Glyph() {
  return (
    <div style={{ display: "flex", alignItems: "flex-end", height: 40 }}>
      <div style={{ width: 8, height: 26, backgroundColor: "#93c5fd", borderRadius: 3 }} />
      <div style={{ width: 8, height: 34, backgroundColor: "#93c5fd", borderRadius: 3, marginLeft: 4 }} />
      <div style={{ width: 8, height: 22, backgroundColor: "#93c5fd", borderRadius: 3, marginLeft: 4 }} />
      <div style={{ width: 8, height: 40, backgroundColor: "#2563eb", borderRadius: 3, marginLeft: 4 }} />
    </div>
  );
}
