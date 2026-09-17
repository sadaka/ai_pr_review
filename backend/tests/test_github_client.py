"""Unit tests for the M13 additions to `GitHubAppClient`: resolving `ref="HEAD"`
to a real commit SHA via the repo's default branch (not GitHub's literal
`commits/HEAD`, which the REST API does not document supporting), and
following the tarball endpoint's redirect to codeload.

Uses `httpx.MockTransport` — no real GitHub App auth or network needed.
"""
from __future__ import annotations

import time

import httpx
import pytest

from integrations.github_client import GitHubAppClient


def _rsa_private_key_pem() -> str:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    return _rsa_private_key_pem()


def _client(private_key_pem: str, handler) -> GitHubAppClient:
    http = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    client = GitHubAppClient(app_id="12345", private_key_pem=private_key_pem, http_client=http)
    # Pre-seed the installation id + token caches so tests only exercise the
    # calls under test, not the whole auth chain.
    client._installation_ids["acme/widgets"] = 999
    client._installation_tokens[999] = ("installation-token", time.monotonic() + 3600)
    return client


async def test_get_repo_head_sha_resolves_head_via_default_branch(private_key_pem):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/repos/acme/widgets":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path == "/repos/acme/widgets/commits/main":
            return httpx.Response(200, json={"sha": "abc123def"})
        raise AssertionError(f"unexpected request: {request.url.path}")

    client = _client(private_key_pem, handler)

    sha = await client.get_repo_head_sha(repo_full_name="acme/widgets")

    assert sha == "abc123def"
    assert calls == ["/repos/acme/widgets", "/repos/acme/widgets/commits/main"]


async def test_get_repo_head_sha_passes_through_a_concrete_ref(private_key_pem):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/repos/acme/widgets/commits/deadbeef":
            return httpx.Response(200, json={"sha": "deadbeef"})
        raise AssertionError(f"unexpected request: {request.url.path} (should not resolve default branch)")

    client = _client(private_key_pem, handler)

    sha = await client.get_repo_head_sha(repo_full_name="acme/widgets", ref="deadbeef")

    assert sha == "deadbeef"
    assert calls == ["/repos/acme/widgets/commits/deadbeef"]


async def test_download_tarball_follows_codeload_redirect(private_key_pem):
    tarball_bytes = b"fake-gzip-tarball-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/widgets/tarball/main":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/acme/widgets/tar.gz/main"})
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=tarball_bytes)
        raise AssertionError(f"unexpected request: {request.url}")

    client = _client(private_key_pem, handler)

    content = await client.download_tarball(repo_full_name="acme/widgets", ref="main")

    assert content == tarball_bytes
