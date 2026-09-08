"""Evaluation harness (spec §4.1 phase 9): golden dataset + deterministic
scorer + non-blocking LLM-as-judge + regression gate.

Leaf module — nothing under `backend/` imports it. It is exercised by
`tests/test_evaluation.py` and the `python -m evaluation.regression_gate` CLI
only. The blocking metric (recall + false-positive count vs a committed
`baseline.json`) is deterministic; the LLM-judge scores rationale quality as a
separate, non-blocking layer that runs only when live credentials are present.
"""
