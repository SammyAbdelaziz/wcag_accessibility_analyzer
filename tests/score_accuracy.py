"""
Analyzer Accuracy Scorer
Measures precision/recall per fixture against the labeled gold set.

Usage:
    python tests/score_accuracy.py

Output:
    Per-fixture and aggregate precision, recall, F1, and a list of unexpected
    or missing findings to investigate.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from wcag.analyzers.docx_analyzer import DocxAnalyzer
from wcag.analyzers.pptx_analyzer import PptxAnalyzer

GOLD_SET_PATH = Path(__file__).parent / "gold_set.json"
# Override with WCAG_UPLOADS_DIR. Default points at a repo-relative fixtures
# dir that is not shipped (this script is for local accuracy runs against
# a private corpus).
UPLOADS_DIR = Path(os.environ.get(
    "WCAG_UPLOADS_DIR",
    str(Path(__file__).parent / "fixtures" / "uploads"),
))


def analyze(filename: str):
    path = UPLOADS_DIR / filename
    data = path.read_bytes()
    if path.suffix.lower() == ".pptx":
        return PptxAnalyzer(data, path.name).analyze()
    return DocxAnalyzer(data, path.name).analyze()


def remediation_ids(fact_sheet) -> set:
    ids = set()
    for f in fact_sheet.confirmed_findings + fact_sheet.possible_findings:
        if f.remediation_id:
            ids.add(f.remediation_id)
    return ids


def score_fixture(name: str, expected: set, must_not: set):
    fact_sheet = analyze(name)
    actual = remediation_ids(fact_sheet)

    true_positive = expected & actual
    false_negative = expected - actual  # expected, not produced
    false_positive_explicit = must_not & actual  # explicitly forbidden, but produced

    precision_denom = len(true_positive) + len(false_positive_explicit)
    recall_denom = len(expected)

    precision = len(true_positive) / precision_denom if precision_denom else None
    recall = len(true_positive) / recall_denom if recall_denom else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision and recall
        else None
    )

    return {
        "fixture": name,
        "expected": sorted(expected),
        "actual": sorted(actual),
        "true_positive": sorted(true_positive),
        "false_negative": sorted(false_negative),
        "forbidden_but_present": sorted(false_positive_explicit),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def main():
    if not UPLOADS_DIR.exists():
        print(
            f"[SKIP] uploads directory not found: {UPLOADS_DIR}\n"
            f"       Set WCAG_UPLOADS_DIR to a directory containing the gold-set fixtures\n"
            f"       to run accuracy scoring.",
            file=sys.stderr,
        )
        return
    gold = json.loads(GOLD_SET_PATH.read_text(encoding="utf-8"))
    fixtures = gold.get("fixtures", {})

    rows = []
    skipped = 0
    for name, spec in fixtures.items():
        expected = set(spec.get("expected_remediation_ids", []))
        must_not = set(spec.get("must_not_contain_remediation_ids", []))
        if not expected and not must_not:
            skipped += 1
            continue
        rows.append(score_fixture(name, expected, must_not))

    # Aggregate
    total_tp = sum(len(r["true_positive"]) for r in rows)
    total_expected = sum(len(r["expected"]) for r in rows)
    total_forbidden = sum(len(r["forbidden_but_present"]) for r in rows)

    overall_recall = total_tp / total_expected if total_expected else None
    overall_precision = (
        total_tp / (total_tp + total_forbidden) if (total_tp + total_forbidden) else None
    )
    overall_f1 = (
        2 * overall_precision * overall_recall / (overall_precision + overall_recall)
        if overall_precision and overall_recall
        else None
    )

    print("=" * 80)
    print("ANALYZER ACCURACY SCORECARD")
    print("=" * 80)
    print(f"Fixtures scored: {len(rows)}  (skipped {skipped} unlabeled)")
    print(f"Total expected findings: {total_expected}")
    print(f"True positives (matched): {total_tp}")
    print(f"Forbidden findings present (FP): {total_forbidden}")

    if not rows:
        print()
        print("No labeled fixtures yet. Edit tests/gold_set.json to populate")
        print("'expected_remediation_ids' for each fixture, then re-run.")
        return

    if overall_recall is not None:
        print(f"Overall recall:    {overall_recall:.2%}")
    if overall_precision is not None:
        print(f"Overall precision: {overall_precision:.2%}")
    if overall_f1 is not None:
        print(f"Overall F1:        {overall_f1:.2%}")

    print()
    print("Per-fixture summary:")
    for r in rows:
        miss = r["false_negative"]
        fp = r["forbidden_but_present"]
        recall = f"{r['recall']:.0%}" if r["recall"] is not None else "n/a"
        precision = f"{r['precision']:.0%}" if r["precision"] is not None else "n/a"
        flag = "OK" if not miss and not fp else "REVIEW"
        print(
            f"[{flag}] {r['fixture']}  recall={recall}  precision={precision}"
        )
        if miss:
            print(f"        missing: {miss}")
        if fp:
            print(f"        forbidden_present: {fp}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
