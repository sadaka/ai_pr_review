"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { getRepos, getReviews, UnauthorizedError, type RepoSummary, type ReviewSummary } from "../lib/api";
import { clearToken, getToken } from "../lib/auth";

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

export default function Page() {
  const router = useRouter();
  const [repos, setRepos] = useState<RepoSummary[] | null>(null);
  const [reviews, setReviews] = useState<ReviewSummary[] | null>(null);

  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    function onAuthFailure(err: unknown) {
      if (err instanceof UnauthorizedError) {
        clearToken();
        router.replace("/login");
        return true;
      }
      return false;
    }
    getRepos(token)
      .then(setRepos)
      .catch((err) => {
        if (!onAuthFailure(err)) setRepos([]);
      });
    getReviews(token)
      .then(setReviews)
      .catch((err) => {
        if (!onAuthFailure(err)) setReviews([]);
      });
  }, [router]);

  return (
    <main style={{ maxWidth: 900, margin: "0 auto" }}>
      <nav style={{ marginBottom: 16, fontSize: 13 }}>
        <Link href="/" style={{ color: "#58a6ff", marginRight: 16 }}>
          Dashboard
        </Link>
        <Link href="/hitl" style={{ color: "#58a6ff" }}>
          HITL inbox
        </Link>
      </nav>

      <h1 style={{ fontSize: 22, marginBottom: 4 }}>Indexed repos</h1>
      {repos === null ? (
        <p style={{ color: "#8b949e", marginTop: 0, fontSize: 13 }}>Loading…</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14, marginBottom: 32 }}>
          <thead>
            <tr>
              <th style={{ ...cell, color: "#8b949e" }}>Repo</th>
              <th style={{ ...cell, color: "#8b949e" }}>Last indexed commit</th>
              <th style={{ ...cell, color: "#8b949e" }}>Indexed at</th>
              <th style={{ ...cell, color: "#8b949e" }}>Chunks</th>
              <th style={{ ...cell, color: "#8b949e" }}>Reviews</th>
            </tr>
          </thead>
          <tbody>
            {repos.length === 0 ? (
              <tr>
                <td style={{ ...cell, color: "#8b949e" }} colSpan={5}>
                  No repos indexed yet.
                </td>
              </tr>
            ) : (
              repos.map((r) => (
                <tr key={r.repo}>
                  <td style={cell}>
                    <code>{r.repo}</code>
                  </td>
                  <td style={cell}>
                    <code>{r.last_indexed_commit.slice(0, 12)}</code>
                  </td>
                  <td style={cell}>{new Date(r.indexed_at).toLocaleString()}</td>
                  <td style={cell}>{r.chunk_count}</td>
                  <td style={cell}>{r.review_count}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      )}

      <h1 style={{ fontSize: 22, marginBottom: 4 }}>Reviews</h1>
      {reviews === null ? (
        <p style={{ color: "#8b949e", marginTop: 0, fontSize: 13 }}>Loading…</p>
      ) : (
        <>
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
                      <Link href={`/reviews/${r.id}`} style={{ color: "#e6edf3" }}>
                        <code>{r.repo}</code> #{r.pr_number}
                      </Link>
                    </td>
                    <td style={cell}>{r.status}</td>
                    <td style={cell}>{cost(r.cost_usd)}</td>
                    <td style={cell}>{confidence(r.confidence)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </>
      )}
    </main>
  );
}
