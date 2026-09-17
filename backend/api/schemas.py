"""Pydantic schemas for the public API surface.

M2 scope: just enough of the GitHub `pull_request` webhook payload to route a
review job. We deliberately do NOT model the full GitHub payload (it's huge and
mostly irrelevant here) — `extra="ignore"` lets unknown fields pass through
untouched so a schema-drift on GitHub's side never breaks ingress. Deep diff
content is fetched later, in the orchestrator (M3+), not parsed here.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Repository(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str


class PullRequestWebhookPayload(BaseModel):
    """The subset of GitHub's `pull_request` event we act on."""

    model_config = ConfigDict(extra="ignore")

    action: str
    number: int
    pull_request: dict
    repository: Repository


class ReviewJob(BaseModel):
    """What actually gets enqueued to ARQ — deliberately small and serializable."""

    model_config = ConfigDict(extra="ignore")

    delivery_id: str
    repo_full_name: str
    pr_number: int
    action: str


# ── M13: multi-repo ingestion ──────────────────────────────────────────────
# The subset of GitHub's `installation`/`installation_repositories`/`push`
# events needed to route ingestion jobs — same "model only what we act on,
# ignore the rest" discipline as `PullRequestWebhookPayload` above.


class _InstalledRepository(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str


class InstallationPayload(BaseModel):
    """`installation` event: fired when the App is installed on an account.
    `action == "created"` carries the initial repository list."""

    model_config = ConfigDict(extra="ignore")

    action: str
    repositories: list[_InstalledRepository] = []


class InstallationRepositoriesPayload(BaseModel):
    """`installation_repositories` event: fired when repos are added/removed
    from an existing installation. Only `repositories_added` triggers ingestion
    — a removed repo's `code_chunks` rows are left in place (no destructive
    action taken on an installation-scope change alone)."""

    model_config = ConfigDict(extra="ignore")

    action: str
    repositories_added: list[_InstalledRepository] = []


class _PushCommit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []


class PushWebhookPayload(BaseModel):
    """`push` event: `before`/`after` are commit SHAs; `commits[]` carries the
    per-commit added/removed/modified file lists this aggregates across the
    whole push (ADR-0005 — no local git history to diff against)."""

    model_config = ConfigDict(extra="ignore")

    before: str
    after: str
    repository: Repository
    commits: list[_PushCommit] = []


class IndexRepoJob(BaseModel):
    """Enqueued for a repo newly granted to the App — triggers `full_index`."""

    model_config = ConfigDict(extra="ignore")

    delivery_id: str
    repo_full_name: str


class ReindexRepoJob(BaseModel):
    """Enqueued on `push` — triggers `incremental_index` for exactly the files
    the push reports as changed."""

    model_config = ConfigDict(extra="ignore")

    delivery_id: str
    repo_full_name: str
    before_sha: str
    after_sha: str
    added: list[str] = []
    modified: list[str] = []
    removed: list[str] = []
