"""GitHub App client — the one place the review bot talks *out* to GitHub.

Auth chain (standard GitHub App flow):
  App JWT (RS256, signed with the app private key)               — identifies the app
    → installation id           GET /repos/{owner}/{repo}/installation
    → installation access token  POST /app/installations/{id}/access_tokens  (~1h TTL)
    → the actual call            POST /repos/{owner}/{repo}/pulls/{n}/reviews

`outbound_call_safety` (context-graph invariant): every request goes through an
explicit timeout, retry-with-backoff on transient failures, and a circuit
breaker that trips after repeated failures so a GitHub outage fails fast
instead of hanging the aggregator. Same self-contained pattern as
`job_queue/queue.py` — deliberately not shared, to avoid a sideways import.

Idempotency of *posting* is not enforced here — it lives one layer up: the
aggregator only calls `post_review` once per review, and `TruthStore` keys a
review on the unique `github_delivery_id`, so a redelivered webhook resolves
to the same review row and skips re-posting.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import jwt

GITHUB_API = "https://api.github.com"
_REQUEST_TIMEOUT_SECONDS = 10.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.2
_BREAKER_FAILURE_THRESHOLD = 4
_BREAKER_RESET_AFTER_SECONDS = 30.0

_JWT_TTL_SECONDS = 9 * 60  # GitHub caps app JWTs at 10 min; stay just under
_JWT_BACKDATE_SECONDS = 60  # tolerate small clock skew between us and GitHub
_INSTALLATION_TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60  # refresh at ~55 min of the ~60 min TTL


class GitHubError(RuntimeError):
    """A GitHub call failed after retries, or returned a non-retryable error status."""


class CircuitOpenError(GitHubError):
    """Raised instead of calling GitHub while the breaker is open."""


class _CircuitBreaker:
    """Opens after N consecutive failures; half-opens (one probe) after a cooldown."""

    def __init__(self, threshold: int = _BREAKER_FAILURE_THRESHOLD, reset_after: float = _BREAKER_RESET_AFTER_SECONDS) -> None:
        self._threshold = threshold
        self._reset_after = reset_after
        self._failures = 0
        self._opened_at: float | None = None

    def before_call(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at >= self._reset_after:
            return  # half-open: allow one probe through
        raise CircuitOpenError("github circuit breaker is open")

    def on_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()


def _is_retryable_status(status: int) -> bool:
    return status == 429 or status >= 500


class GitHubAppClient:
    """One instance per process. `repo_full_name` is always "owner/name"."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._http = http_client or httpx.AsyncClient(base_url=GITHUB_API, timeout=_REQUEST_TIMEOUT_SECONDS)
        self._owns_http = http_client is None
        self._breaker = _CircuitBreaker()

        self._jwt: tuple[str, float] | None = None  # (token, expires_at monotonic)
        self._installation_ids: dict[str, int] = {}  # repo_full_name -> installation id
        self._installation_tokens: dict[int, tuple[str, float]] = {}  # id -> (token, expires_at monotonic)

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "GitHubAppClient":
        key_path = Path(env["GITHUB_PRIVATE_KEY_PATH"])
        if not key_path.is_absolute():
            # paths in .env are written relative to backend/
            key_path = Path(__file__).resolve().parent.parent / key_path
        return cls(app_id=env["GITHUB_APP_ID"], private_key_pem=key_path.read_text())

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ── auth ────────────────────────────────────────────────────────────────

    def _app_jwt(self) -> str:
        now = time.time()
        if self._jwt is not None and self._jwt[1] - time.monotonic() > 30:
            return self._jwt[0]
        payload = {
            "iat": int(now) - _JWT_BACKDATE_SECONDS,
            "exp": int(now) + _JWT_TTL_SECONDS,
            "iss": self._app_id,
        }
        token = jwt.encode(payload, self._private_key_pem, algorithm="RS256")
        self._jwt = (token, time.monotonic() + _JWT_TTL_SECONDS)
        return token

    async def _installation_id(self, repo_full_name: str) -> int:
        cached = self._installation_ids.get(repo_full_name)
        if cached is not None:
            return cached
        owner, name = repo_full_name.split("/", 1)
        data = await self._request(
            "GET", f"/repos/{owner}/{name}/installation", auth=f"Bearer {self._app_jwt()}"
        )
        installation_id = int(data["id"])
        self._installation_ids[repo_full_name] = installation_id
        return installation_id

    async def _installation_token(self, repo_full_name: str) -> str:
        installation_id = await self._installation_id(repo_full_name)
        cached = self._installation_tokens.get(installation_id)
        if cached is not None and cached[1] - time.monotonic() > _INSTALLATION_TOKEN_REFRESH_MARGIN_SECONDS:
            return cached[0]
        data = await self._request(
            "POST", f"/app/installations/{installation_id}/access_tokens", auth=f"Bearer {self._app_jwt()}"
        )
        token = data["token"]
        # GitHub returns an ISO expiry ~1h out; we don't parse it — just assume
        # 60 min from now and refresh early via the margin above.
        self._installation_tokens[installation_id] = (token, time.monotonic() + 60 * 60)
        return token

    # ── public operations ──────────────────────────────────────────────────

    async def get_pull_request_diff(self, *, repo_full_name: str, pr_number: int) -> str:
        """The unified diff for a PR — the `application/vnd.github.v3.diff`
        media type returns the raw patch as text, not JSON. This is what the
        ARQ worker feeds to the specialist agents."""
        owner, name = repo_full_name.split("/", 1)
        token = await self._installation_token(repo_full_name)
        resp = await self._send(
            "GET",
            f"/repos/{owner}/{name}/pulls/{pr_number}",
            auth=f"token {token}",
            accept="application/vnd.github.v3.diff",
        )
        return resp.text

    async def post_review(self, *, repo_full_name: str, pr_number: int, body: str, event: str) -> int:
        """Create a PR review. `event` is one of APPROVE / COMMENT / REQUEST_CHANGES.
        Returns GitHub's numeric review id."""
        owner, name = repo_full_name.split("/", 1)
        token = await self._installation_token(repo_full_name)
        data = await self._request(
            "POST",
            f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
            auth=f"token {token}",
            json={"body": body, "event": event},
        )
        return int(data["id"])

    # ── transport ───────────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, *, auth: str, json: dict | None = None) -> dict:
        resp = await self._send(method, path, auth=auth, json=json)
        return resp.json() if resp.content else {}

    async def _send(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        json: dict | None = None,
        accept: str = "application/vnd.github+json",
    ) -> httpx.Response:
        """The transport: breaker + retry-with-backoff around one request,
        returning the successful `Response` (a definitive 4xx raises)."""
        headers = {
            "Authorization": auth,
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async def _once() -> httpx.Response:
            return await self._http.request(method, path, headers=headers, json=json)

        self._breaker.before_call()
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                resp = await _once()
            except httpx.HTTPError as exc:  # connect/read/timeout — transient
                last_exc = exc
            else:
                if resp.status_code < 400:
                    self._breaker.on_success()
                    return resp
                if not _is_retryable_status(resp.status_code):
                    self._breaker.on_success()  # a 4xx is a definitive answer, not a service failure
                    raise GitHubError(f"GitHub {method} {path} -> {resp.status_code}: {resp.text[:300]}")
                last_exc = GitHubError(f"GitHub {method} {path} -> {resp.status_code} (retryable)")
            if attempt < _RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_BASE_DELAY_SECONDS * (2**attempt))

        self._breaker.on_failure()
        raise GitHubError(f"GitHub {method} {path} failed after {_RETRY_ATTEMPTS} attempts") from last_exc
