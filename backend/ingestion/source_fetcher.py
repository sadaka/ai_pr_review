"""Fetches a repo's source tree onto local disk (ADR-0005: tarball via GitHub's
REST API, not `git clone`) so `chunker` can walk it.

Reuses `integrations.github_client.GitHubAppClient`'s installation-token auth
and breaker/retry transport — no second credential mechanism, no `git` binary.
"""
from __future__ import annotations

import io
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SupportsTarballFetch(Protocol):
    async def get_repo_head_sha(self, *, repo_full_name: str, ref: str = "HEAD") -> str: ...
    async def download_tarball(self, *, repo_full_name: str, ref: str) -> bytes: ...


@dataclass(frozen=True)
class FetchedSource:
    root: Path
    commit_sha: str


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract guarding against path traversal (`../` members) — the tarball
    is fetched from GitHub, but a compromised/misconfigured upstream should
    never be able to write outside `dest`."""
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        if not _is_within(dest.resolve(), member_path):
            raise ValueError(f"tarball member escapes extraction root: {member.name}")
    tar.extractall(dest, filter="data")  # members already validated above; "data" is the safe default


async def fetch_tarball(github: SupportsTarballFetch, *, repo_full_name: str, ref: str = "HEAD") -> FetchedSource:
    """Download and extract `repo_full_name` at `ref`. Returns the extracted
    root directory (GitHub tarballs nest everything under one
    `{owner}-{repo}-{short_sha}/` directory — this returns that inner dir, not
    the temp dir itself) plus the resolved commit SHA."""
    commit_sha = await github.get_repo_head_sha(repo_full_name=repo_full_name, ref=ref)
    tarball_bytes = await github.download_tarball(repo_full_name=repo_full_name, ref=commit_sha)

    tmp_dir = Path(tempfile.mkdtemp(prefix="ingestion-"))
    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
        _safe_extract(tar, tmp_dir)

    entries = [p for p in tmp_dir.iterdir() if p.is_dir()]
    if len(entries) != 1:
        raise ValueError(f"expected exactly one top-level dir in tarball, found {len(entries)}")
    return FetchedSource(root=entries[0], commit_sha=commit_sha)
