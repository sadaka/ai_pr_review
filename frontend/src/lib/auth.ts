// Single-user bearer token (M14). Stored in localStorage — an accepted
// tradeoff for a single-user personal tool per backend/security/threat_model.md's
// own posture, not a general-purpose session mechanism.

const STORAGE_KEY = "ai-pr-review-token";

export function getToken(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, token);
  } catch {
    // localStorage unavailable (private mode, etc.) — token simply won't persist
  }
}

export function clearToken(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
