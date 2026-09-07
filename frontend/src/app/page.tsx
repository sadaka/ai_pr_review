import { getReviews, type ReviewSummary } from "../lib/api";

// Re-fetch on every request — this is a live status view, not a static page.
export const dynamic = "force-dynamic";

const cell: React.CSSProperties = {
  padding: "8px 12px",
  borderBottom: "1px solid #30363d",
  textAlign: "left",
};

function confidence(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

function cost(value: number): string {
  return `$${value.toFixed(4)}`;
}

export default async function Page() {
  const reviews: ReviewSummary[] = await getReviews();

  return (
    <main style={{ maxWidth: 900, margin: "0 auto" }}>
      <h1 style={{ fontSize: 22, marginBottom: 4 }}>Reviews</h1>
      <p style={{ color: "#8b949e", marginTop: 0, fontSize: 13 }}>
        Recent automated PR reviews — {reviews.length} shown
      </p>

      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
        <thead>
          <tr>
            <th style={{ ...cell, color: "#8b949e" }}>Review</th>
            <th style={{ ...cell, color: "#8b949e" }}>Status</th>
            <th style={{ ...cell, color: "#8b949e" }}>Cost</th>
            <th style={{ ...cell, color: "#8b949e" }}>Confidence</th>
          </tr>
        </thead>
        <tbody>
          {reviews.length === 0 ? (
            <tr>
              <td style={{ ...cell, color: "#8b949e" }} colSpan={4}>
                No reviews yet.
              </td>
            </tr>
          ) : (
            reviews.map((r) => (
              <tr key={r.id}>
                <td style={cell}>
                  <code>{r.repo}</code> #{r.pr_number}
                </td>
                <td style={cell}>{r.status}</td>
                <td style={cell}>{cost(r.cost_usd)}</td>
                <td style={cell}>{confidence(r.confidence)}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </main>
  );
}
