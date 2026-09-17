"""Walks an extracted repo tree and splits files into chunks for embedding.

Chunking strategy: split by top-level symbol boundary where the language makes
that cheap to detect (currently `.py` — top-level `def`/`class`), else fall
back to a fixed-line window with overlap so no file is skipped just because we
don't have a language-aware splitter for it yet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VENDORED_DIR_NAMES = {
    ".git", ".venv", "venv", "node_modules", "vendor", "dist", "build",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".next", "target",
}
_LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "uv.lock", "Pipfile.lock", "go.sum", "Cargo.lock", "composer.lock",
}
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz",
    ".tar", ".whl", ".so", ".dylib", ".dll", ".exe", ".woff", ".woff2",
    ".ttf", ".eot", ".mp3", ".mp4", ".sqlite", ".db", ".bin", ".pyc",
}
_MAX_FILE_BYTES = 500_000  # oversized files (generated code, data dumps) are skipped

_LINE_WINDOW = 200
_LINE_OVERLAP = 20

_PY_TOP_LEVEL_SYMBOL = re.compile(r"^(?:async\s+def|def|class)\s+(\w+)")


@dataclass(frozen=True)
class Chunk:
    path: str
    symbol: str | None
    chunk_index: int
    content: str


def _is_vendored(rel_path: Path) -> bool:
    return any(part in _VENDORED_DIR_NAMES for part in rel_path.parts)


def _should_skip(rel_path: Path, size_bytes: int) -> bool:
    if _is_vendored(rel_path):
        return True
    if rel_path.name in _LOCKFILE_NAMES:
        return True
    if rel_path.suffix.lower() in _BINARY_SUFFIXES:
        return True
    if size_bytes > _MAX_FILE_BYTES:
        return True
    return False


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binary or unreadable — skip rather than guess an encoding


def _split_python(text: str) -> list[tuple[str | None, str]]:
    """Split on top-level `def`/`class` lines. Anything before the first match
    (imports, module docstring) becomes its own leading chunk."""
    lines = text.splitlines(keepends=True)
    boundaries: list[int] = []
    symbols: list[str | None] = []
    for i, line in enumerate(lines):
        match = _PY_TOP_LEVEL_SYMBOL.match(line)
        if match:
            boundaries.append(i)
            symbols.append(match.group(1))

    if not boundaries:
        return [(None, text)] if text.strip() else []

    pieces: list[tuple[str | None, str]] = []
    if boundaries[0] > 0:
        head = "".join(lines[: boundaries[0]])
        if head.strip():
            pieces.append((None, head))
    for idx, start in enumerate(boundaries):
        end = boundaries[idx + 1] if idx + 1 < len(boundaries) else len(lines)
        pieces.append((symbols[idx], "".join(lines[start:end])))
    return pieces


def _split_by_line_window(text: str) -> list[tuple[str | None, str]]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return []
    pieces: list[tuple[str | None, str]] = []
    step = _LINE_WINDOW - _LINE_OVERLAP
    for start in range(0, len(lines), step):
        window = lines[start : start + _LINE_WINDOW]
        if window:
            pieces.append((None, "".join(window)))
        if start + _LINE_WINDOW >= len(lines):
            break
    return pieces


def chunk_file(rel_path: Path, text: str) -> list[Chunk]:
    pieces = _split_python(text) if rel_path.suffix == ".py" else _split_by_line_window(text)
    return [
        Chunk(path=str(rel_path), symbol=symbol, chunk_index=i, content=content)
        for i, (symbol, content) in enumerate(pieces)
        if content.strip()
    ]


def chunk_repo(root: Path, *, only_paths: set[str] | None = None) -> list[Chunk]:
    """Walk `root` and chunk every eligible file. When `only_paths` is given
    (relative POSIX paths), only those files are chunked — the incremental
    re-index path uses this so it re-embeds exactly the changed files."""
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(root)
        if only_paths is not None and rel_path.as_posix() not in only_paths:
            continue
        try:
            size_bytes = path.stat().st_size
        except OSError:
            continue
        if _should_skip(rel_path, size_bytes):
            continue
        text = _read_text(path)
        if text is None:
            continue
        chunks.extend(chunk_file(rel_path, text))
    return chunks
