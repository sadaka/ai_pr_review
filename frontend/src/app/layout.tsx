import type { ReactNode } from "react";

export const metadata = {
  title: "ai-pr-review — status",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          padding: "2rem",
          fontFamily:
            "ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif",
          background: "#0d1117",
          color: "#e6edf3",
        }}
      >
        {children}
      </body>
    </html>
  );
}
