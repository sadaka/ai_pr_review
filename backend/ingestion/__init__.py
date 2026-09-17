"""Multi-repo codebase ingestion & indexing (M13) — the write side of
`code_chunks`. `memory.context_retriever.ContextRetriever` is the sole *reader*
of `code_chunks` (`single_data_spine` invariant); this package is the sole
*writer*. Reads inward from `integrations` (tarball fetch reuses the GitHub
App client) — never the other way around (`dependency_direction`, ADR-0002).
"""
from __future__ import annotations
