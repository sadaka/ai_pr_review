"""The `prompts/REGISTRY.md` changelog must stay in sync with the prompt files.

Each `*.md` prompt's current content hash has to appear in the changelog table
against its name. This fails the moment someone edits a prompt without recording
the new hash — the same guard CI runs.
"""
from __future__ import annotations

import re

from prompts import registry


def _registry_rows() -> dict[str, str]:
    text = registry.REGISTRY_DOC.read_text()
    rows: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\|\s*([a-z_]+)\s*\|\s*`([0-9a-f]{12})`\s*\|", line)
        if m:
            rows.setdefault(m.group(1), m.group(2))
    return rows


def test_changelog_matches_prompt_files_on_disk() -> None:
    rows = _registry_rows()
    for prompt in registry.all_prompts():
        assert prompt.name in rows, f"{prompt.name} missing from prompts/REGISTRY.md"
        assert rows[prompt.name] == prompt.version, (
            f"{prompt.name}: prompt file hashes to {prompt.version} but "
            f"REGISTRY.md records {rows[prompt.name]} — add a changelog row"
        )


def test_version_is_stable_short_sha256() -> None:
    p = registry.get("security")
    assert len(p.version) == 12
    assert registry.get("security").version == p.version
