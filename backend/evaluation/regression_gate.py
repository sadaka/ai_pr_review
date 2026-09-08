"""Regression gate: compare a suite score against a committed baseline and
decide whether the change may land. `evaluate()` is a pure function; the
`__main__` CLI runs the live golden suite (when credentials are present) and
exits non-zero on regression — the "gate blocks" half of spec §4.1 phase 9.

Wiring this into actual CI (GitHub Actions, canary path) is phase 18 and out
of scope here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .scorer import SuiteScore

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


class GateThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_recall_drop: float = 0.05
    max_fp_increase: int = 0


class Baseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mean_recall: float
    total_false_positives: int
    recorded_at: str
    cases: list[str]


class GateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    reasons: list[str]
    current_mean_recall: float
    current_false_positives: int
    baseline_mean_recall: float
    baseline_false_positives: int


def evaluate(
    suite: SuiteScore,
    baseline: Baseline,
    thresholds: GateThresholds = GateThresholds(),
) -> GateReport:
    reasons: list[str] = []

    recall_drop = baseline.mean_recall - suite.mean_recall
    if recall_drop > thresholds.max_recall_drop:
        reasons.append(
            f"mean recall dropped {recall_drop:.3f} "
            f"({baseline.mean_recall:.3f} → {suite.mean_recall:.3f}), "
            f"over the {thresholds.max_recall_drop:.3f} tolerance"
        )

    fp_increase = suite.total_false_positives - baseline.total_false_positives
    if fp_increase > thresholds.max_fp_increase:
        reasons.append(
            f"false positives rose by {fp_increase} "
            f"({baseline.total_false_positives} → {suite.total_false_positives})"
        )

    return GateReport(
        passed=not reasons,
        reasons=reasons,
        current_mean_recall=suite.mean_recall,
        current_false_positives=suite.total_false_positives,
        baseline_mean_recall=baseline.mean_recall,
        baseline_false_positives=baseline.total_false_positives,
    )


def load_baseline(path: Path = BASELINE_PATH) -> Baseline:
    return Baseline.model_validate_json(path.read_text())


def baseline_from_suite(suite: SuiteScore, case_ids: list[str]) -> Baseline:
    return Baseline(
        mean_recall=suite.mean_recall,
        total_false_positives=suite.total_false_positives,
        recorded_at=datetime.now(timezone.utc).isoformat(),
        cases=case_ids,
    )


def write_baseline(suite: SuiteScore, case_ids: list[str], path: Path = BASELINE_PATH) -> None:
    path.write_text(baseline_from_suite(suite, case_ids).model_dump_json(indent=2) + "\n")


async def _run_live_suite() -> tuple[SuiteScore, list[str]]:
    # Imported lazily: pulls in asyncpg / openai and only makes sense with creds.
    from agents.llm_client import create_llm_client
    from memory.context_retriever import create_pool

    from .golden_dataset import load_golden_cases
    from .runner import run_suite

    cases = load_golden_cases()
    pool = await create_pool(os.environ["TIGER_DATABASE_URL"])
    try:
        llm = create_llm_client(os.environ["OPENAI_API_KEY"])
        suite = await run_suite(cases, pool=pool, llm=llm)
    finally:
        await pool.close()
    return suite, [c.id for c in cases]


def _has_live_creds() -> bool:
    return bool(os.environ.get("TIGER_DATABASE_URL")) and os.environ.get(
        "OPENAI_API_KEY", ""
    ).startswith("sk-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the golden suite and gate on regression.")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="run the suite and overwrite baseline.json instead of gating",
    )
    args = parser.parse_args(argv)

    # Mirror tests/conftest.py: pick up backend/.env so a local run has creds
    # without the caller exporting them. Best-effort — absent dotenv is fine.
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass

    if not _has_live_creds():
        print(
            "regression_gate: no TIGER_DATABASE_URL / OPENAI_API_KEY — skipping the live suite. "
            "The deterministic scorer/gate logic is covered by tests/test_evaluation.py.",
            file=sys.stderr,
        )
        return 0

    suite, case_ids = asyncio.run(_run_live_suite())

    if args.update_baseline:
        write_baseline(suite, case_ids)
        print(f"baseline.json updated: mean_recall={suite.mean_recall:.3f} "
              f"false_positives={suite.total_false_positives}")
        return 0

    report = evaluate(suite, load_baseline())
    print(json.dumps(report.model_dump(), indent=2))
    if not report.passed:
        print("REGRESSION GATE: FAIL", file=sys.stderr)
        return 1
    print("REGRESSION GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
