"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { getReviewDetail, NotFoundError, UnauthorizedError, type ReviewDetail } from "../../../lib/api";
import { clearToken, getToken } from "../../../lib/auth";

const cell: React.CSSProperties = {
  padding: "8px 12px",
  borderBottom: "1px solid #30363d",
  textAlign: "left",
};

export default function ReviewDetailPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const [review, setReview] = useState<ReviewDetail | null>(null);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    getReviewDetail(token, params.id)
      .then(setReview)
      .catch((err) => {
        if (err instanceof UnauthorizedError) {
          clearToken();
          router.replace("/login");
        } else if (err instanceof NotFoundError) {
          setNotFound(true);
        }
      });
  }, [router, params.id]);

  if (notFound) {
    return <main style={{ maxWidth: 900, margin: "0 auto" }}>Review not found.</main>;
  }

  return (
    <main style={{ maxWidth: 900, margin: "0 auto" }}>
      <h1 style={{ fontSize: 22, marginBottom: 4 }}>Review detail</h1>
      {review === null ? (
        <p style={{ color: "#8b949e", marginTop: 0, fontSize: 13 }}>Loading…</p>
      ) : (
        <>
          <p style={{ color: "#8b949e", marginTop: 0, fontSize: 13 }}>
            <code>{review.repo}</code> #{review.pr_number} — {review.status} — $
            {review.cost_usd.toFixed(4)} —{" "}
            {review.confidence === null ? "confidence —" : `confidence ${review.confidence.toFixed(2)}`}
            {review.diff_stats && (
              <>
                {" — "}
                {review.diff_stats.files_changed} file
                {review.diff_stats.files_changed === 1 ? "" : "s"} changed,{" "}
                <span style={{ color: "#3fb950" }}>+{review.diff_stats.additions}</span>{" "}
                <span style={{ color: "#f85149" }}>-{review.diff_stats.deletions}</span>
              </>
            )}
          </p>

          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
            <thead>
              <tr>
                <th style={{ ...cell, color: "#8b949e" }}>Severity</th>
                <th style={{ ...cell, color: "#8b949e" }}>Agent</th>
                <th style={{ ...cell, color: "#8b949e" }}>Location</th>
                <th style={{ ...cell, color: "#8b949e" }}>Summary</th>
              </tr>
            </thead>
            <tbody>
              {review.findings.length === 0 ? (
                <tr>
                  <td style={{ ...cell, color: "#8b949e" }} colSpan={4}>
                    No findings.
                  </td>
                </tr>
              ) : (
                review.findings.map((f, i) => (
                  <tr key={i}>
                    <td style={cell}>{f.severity}</td>
                    <td style={cell}>{f.agent_type}</td>
                    <td style={cell}>
                      <code>
                        {f.file_path}
                        {f.line_start !== null ? `:${f.line_start}` : ""}
                      </code>
                    </td>
                    <td style={cell}>{f.summary}</td>
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
