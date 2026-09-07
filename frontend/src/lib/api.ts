// The only backend touchpoint for the frontend (ADR-002: frontend -> backend/api,
// never the database directly). Read-only; failures degrade to an empty list so
// the status page always renders.

export type ReviewSummary = {
  id: string;
  repo: string;
  pr_number: number;
  status: string;
  confidence: number | null;
  cost_usd: number;
  created_at: string;
};

const BACKEND_URL = (process.env.BACKEND_URL ?? "http://localhost:8000").replace(/\/$/, "");

export async function getReviews(): Promise<ReviewSummary[]> {
  try {
    const res = await fetch(`${BACKEND_URL}/api/reviews`, { cache: "no-store" });
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data) ? (data as ReviewSummary[]) : [];
  } catch {
    // backend not running / unreachable — the page still renders its shell
    return [];
  }
}
