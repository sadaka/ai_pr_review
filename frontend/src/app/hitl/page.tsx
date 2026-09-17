"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { getHitlPending, resolveHitl, UnauthorizedError, type PendingHitlItem } from "../../lib/api";
import { clearToken, getToken } from "../../lib/auth";

const cell: React.CSSProperties = {
  padding: "8px 12px",
  borderBottom: "1px solid #30363d",
  textAlign: "left",
};

const button: React.CSSProperties = {
  padding: "4px 10px",
  fontSize: 13,
  border: "1px solid #30363d",
  borderRadius: 6,
  cursor: "pointer",
  marginRight: 8,
};

export default function HitlPage() {
  const router = useRouter();
  const [items, setItems] = useState<PendingHitlItem[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    (token: string) => {
      getHitlPending(token)
        .then(setItems)
        .catch((err) => {
          if (err instanceof UnauthorizedError) {
            clearToken();
            router.replace("/login");
          } else {
            setItems([]);
          }
        });
    },
    [router]
  );

  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    load(token);
  }, [router, load]);

  async function onResolve(reviewId: string, action: "approve" | "reject") {
    const token = getToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    setBusyId(reviewId);
    setError(null);
    try {
      const result = await resolveHitl(token, reviewId, action);
      if (!result.ok) {
        setError(`${reviewId} was already resolved.`);
      }
      load(token);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        clearToken();
        router.replace("/login");
      } else {
        setError(`Failed to ${action} ${reviewId}.`);
      }
    } finally {
      setBusyId(null);
    }
  }

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

      <h1 style={{ fontSize: 22, marginBottom: 4 }}>HITL inbox</h1>
      {items === null ? (
        <p style={{ color: "#8b949e", marginTop: 0, fontSize: 13 }}>Loading…</p>
      ) : (
        <>
          <p style={{ color: "#8b949e", marginTop: 0, fontSize: 13 }}>
            Reviews awaiting human approval — {items.length} pending
          </p>
          {error && <p style={{ color: "#f85149", fontSize: 13 }}>{error}</p>}

          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
            <thead>
              <tr>
                <th style={{ ...cell, color: "#8b949e" }}>Review</th>
                <th style={{ ...cell, color: "#8b949e" }}>Reason</th>
                <th style={{ ...cell, color: "#8b949e" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 ? (
                <tr>
                  <td style={{ ...cell, color: "#8b949e" }} colSpan={3}>
                    Nothing pending.
                  </td>
                </tr>
              ) : (
                items.map((item) => (
                  <tr key={item.id}>
                    <td style={cell}>
                      <code>{item.repo}</code> #{item.pr_number}
                    </td>
                    <td style={cell}>{item.reason}</td>
                    <td style={cell}>
                      <button
                        style={{ ...button, color: "#3fb950" }}
                        disabled={busyId === item.review_id}
                        onClick={() => onResolve(item.review_id, "approve")}
                      >
                        Approve
                      </button>
                      <button
                        style={{ ...button, color: "#f85149" }}
                        disabled={busyId === item.review_id}
                        onClick={() => onResolve(item.review_id, "reject")}
                      >
                        Reject
                      </button>
                    </td>
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
