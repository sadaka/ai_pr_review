"""Loads the per-agent system prompt files in this directory as plain text."""
from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text().strip()
