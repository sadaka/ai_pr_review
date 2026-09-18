// The only backend touchpoint for the frontend (ADR-002: frontend -> backend/api,
// never the database directly).
//
// M14: the API requires the M14 bearer token, which lives in the browser's
// localStorage (see lib/auth.ts) — so these fetches run client-side, not in a
// server component like M7's original version did.

export type ReviewSummary = {
  id: string;
  repo: string;
  pr_number: number;
  status: string;
  confidence: number | null;
  cost_usd: number;
  created_at: string;
};

export type FindingSummary = {
  agent_type: string;
  severity: string;
  category: string;
  summary: string;
  file_path: string;
  line_start: number | null;
  confidence: number;
  rationale: string;
};

export type DiffStats = {
  files_changed: number;
  additions: number;
  deletions: number;
};

export type ReviewDetail = ReviewSummary & {
  findings: FindingSummary[];
  diff_stats: DiffStats | null;
};

export type RepoSummary = {
  repo: string;
  status: "pending" | "done" | "failed";
  last_indexed_commit: string | null;
  indexed_at: string;
  chunk_count: number;
  review_count: number;
  error: string | null;
};

export type PendingHitlItem = {
  id: string;
  review_id: string;
  repo: string;
  pr_number: number;
  reason: string;
  created_at: string;
};

const BACKEND_URL = (
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000"
).replace(/\/$/, "");

export class UnauthorizedError extends Error {}
export class NotFoundError extends Error {}

async function authedFetch(path: string, token: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(`${BACKEND_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: { ...(init?.headers ?? {}), Authorization: `Bearer ${token}` },
  });
  if (res.status === 401) throw new UnauthorizedError();
  return res;
}

export async function getReviews(token: string): Promise<ReviewSummary[]> {
  const res = await authedFetch("/api/reviews", token);
  if (!res.ok) return [];
  const data = await res.json();
  return Array.isArray(data) ? (data as ReviewSummary[]) : [];
}

export async function getReviewDetail(token: string, id: string): Promise<ReviewDetail> {
  const res = await authedFetch(`/api/reviews/${id}`, token);
  if (res.status === 404) throw new NotFoundError();
  if (!res.ok) throw new Error(`GET /api/reviews/${id} failed: ${res.status}`);
  return (await res.json()) as ReviewDetail;
}

export async function getRepos(token: string): Promise<RepoSummary[]> {
  const res = await authedFetch("/api/repos", token);
  if (!res.ok) return [];
  const data = await res.json();
  return Array.isArray(data) ? (data as RepoSummary[]) : [];
}

export async function getHitlPending(token: string): Promise<PendingHitlItem[]> {
  const res = await authedFetch("/api/hitl", token);
  if (!res.ok) return [];
  const data = await res.json();
  return Array.isArray(data) ? (data as PendingHitlItem[]) : [];
}

export async function resolveHitl(
  token: string,
  reviewId: string,
  action: "approve" | "reject"
): Promise<{ ok: true } | { ok: false; alreadyResolved: true }> {
  const res = await authedFetch(`/api/hitl/${reviewId}/${action}`, token, { method: "POST" });
  if (res.status === 409) return { ok: false, alreadyResolved: true };
  if (!res.ok) throw new Error(`POST /api/hitl/${reviewId}/${action} failed: ${res.status}`);
  return { ok: true };
}
