"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { setToken } from "../../lib/auth";

// Deliberately minimal (M7/M14 precedent: no design-system, no component
// library) — paste the API_AUTH_TOKEN from backend/.env, stored client-side.
export default function LoginPage() {
  const router = useRouter();
  const [value, setValue] = useState("");

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!value.trim()) return;
    setToken(value.trim());
    router.push("/");
  }

  return (
    <main style={{ maxWidth: 400, margin: "0 auto" }}>
      <h1 style={{ fontSize: 22, marginBottom: 4 }}>Sign in</h1>
      <p style={{ color: "#8b949e", marginTop: 0, fontSize: 13 }}>
        Paste the <code>API_AUTH_TOKEN</code> from <code>backend/.env</code>.
      </p>
      <form onSubmit={onSubmit}>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="token"
          style={{
            width: "100%",
            padding: "8px 10px",
            fontSize: 14,
            background: "#161b22",
            color: "#e6edf3",
            border: "1px solid #30363d",
            borderRadius: 6,
            boxSizing: "border-box",
          }}
        />
        <button
          type="submit"
          style={{
            marginTop: 12,
            padding: "8px 16px",
            fontSize: 14,
            background: "#238636",
            color: "#fff",
            border: "none",
            borderRadius: 6,
            cursor: "pointer",
          }}
        >
          Continue
        </button>
      </form>
    </main>
  );
}
