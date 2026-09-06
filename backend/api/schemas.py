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
