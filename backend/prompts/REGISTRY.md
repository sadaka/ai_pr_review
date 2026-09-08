# Prompt registry — changelog

Each specialist runs on a versioned system prompt. The **version** is
`sha256(prompt_text)[:12]` (see `prompts/registry.py`). It is stamped onto the
`span.start` row in the `agent_events` spine, so any stored review can be traced
back to the exact prompt that produced it.

When you edit a `*.md` prompt in this directory, add a row here with the new
hash and a one-line note. `tests/test_prompt_registry.py` fails until this table
matches the files on disk.

| prompt   | version        | updated    | note                    |
|----------|----------------|------------|-------------------------|
| docs     | `d314c0482795` | 2026-09-08 | initial registry entry  |
| quality  | `a1c302568c6a` | 2026-09-08 | initial registry entry  |
| security | `3ab3aac94287` | 2026-09-08 | initial registry entry  |
| tests    | `3626a45d1388` | 2026-09-08 | initial registry entry  |
