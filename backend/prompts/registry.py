"""Content-hash prompt registry (M12 / phase 18 — prompt versioning).

`registry.get(name)` returns the specialist system prompt paired with a short
sha256 stamp of its exact text. That stamp is written into the agent's
`span.start` row on the `agent_events` spine (see `agents/base_agent.py`), so
every stored review is traceable to the prompt revision that produced it — the
git history *is* the version history; the hash makes it explicit.

`prompts/REGISTRY.md` is a human-readable changelog of those hashes;
`tests/test_prompt_registry.py` keeps it honest.

Leaf module: reads files only, no project imports (`dependency_direction`) —
exactly like `prompts/loader.py`, which it supersedes for the specialists.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent
_VERSION_LEN = 12
REGISTRY_DOC = _PROMPTS_DIR / "REGISTRY.md"


@dataclass(frozen=True)
class VersionedPrompt:
    """A prompt's text plus a stable short hash of that text."""

    name: str
    text: str
    version: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_VERSION_LEN]


def get(name: str) -> VersionedPrompt:
    text = (_PROMPTS_DIR / f"{name}.md").read_text().strip()
    return VersionedPrompt(name=name, text=text, version=_hash(text))


def all_prompts() -> list[VersionedPrompt]:
    """Every `*.md` prompt in this directory (the REGISTRY changelog aside),
    sorted by name."""
    return [
        get(path.stem)
        for path in sorted(_PROMPTS_DIR.glob("*.md"))
        if path.name != REGISTRY_DOC.name
    ]
