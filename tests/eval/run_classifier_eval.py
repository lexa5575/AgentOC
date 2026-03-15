#!/usr/bin/env python3
"""
Classifier Eval Runner
----------------------

Runs classifier against eval cases and measures accuracy.
Uses production formatters (no manual f-string duplication).

Usage:
    cd "ag infra up"
    python -m tests.eval.run_classifier_eval                  # run all
    python -m tests.eval.run_classifier_eval --tags oos_followup  # filter by tag
    python -m tests.eval.run_classifier_eval --save            # save baseline
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agents.classifier import run_classification
from agents.formatters import (
    compose_classifier_context,
    format_combined_email_text,
)

logger = logging.getLogger(__name__)

EVAL_CASES_PATH = Path(__file__).parent / "classifier_eval_cases.json"
BASELINE_PATH = Path(__file__).parent / "baseline_results.json"


# ---------------------------------------------------------------------------
# Context assembly — uses the same shared composer as production
# ---------------------------------------------------------------------------

def _convert_dates(history: list[dict]) -> list[dict]:
    """Convert date strings in thread history to datetime objects."""
    result = []
    for msg in history:
        entry = dict(msg)
        if isinstance(entry.get("created_at"), str):
            try:
                entry["created_at"] = datetime.fromisoformat(entry["created_at"])
            except (ValueError, TypeError):
                entry["created_at"] = None
        result.append(entry)
    return result


def _build_eval_context(case: dict) -> str:
    """Build classifier context string from eval case data.

    Uses compose_classifier_context() — the same shared composer
    that build_classifier_context() uses in production.
    Guarantees identical whitespace and ordering.
    """
    thread_history = case.get("thread_history")
    if thread_history:
        thread_history = _convert_dates(thread_history)

    return compose_classifier_context(
        conversation_state=case.get("conversation_state"),
        thread_history=thread_history,
        other_thread_states=case.get("other_threads"),
        exclude_thread_id=None,
    )


def _build_eval_email_text(case: dict) -> str:
    """Build email text, handling combined_messages format."""
    combined = case.get("combined_messages")
    if combined:
        # Convert date strings to datetime objects
        candidates = []
        for c in combined:
            entry = dict(c)
            if isinstance(entry.get("created_at"), str):
                try:
                    entry["created_at"] = datetime.fromisoformat(entry["created_at"])
                except (ValueError, TypeError):
                    entry["created_at"] = None
            candidates.append(entry)
        return format_combined_email_text(candidates)
    return case["email_text"]


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _compare_order_items(actual_items, expected_items) -> dict:
    """Compare order_items shape: base_flavor, quantity, region_preference, strict_region."""
    if expected_items is None and actual_items is None:
        return {"match": True, "detail": "both null"}
    if expected_items is None and actual_items is not None:
        return {"match": False, "detail": f"expected null, got {len(actual_items)} items"}
    if expected_items is not None and actual_items is None:
        return {"match": False, "detail": f"expected {len(expected_items)} items, got null"}

    if len(actual_items) != len(expected_items):
        return {
            "match": False,
            "detail": f"count mismatch: expected {len(expected_items)}, got {len(actual_items)}",
        }

    mismatches = []
    for i, (exp, act) in enumerate(zip(expected_items, actual_items)):
        act_dict = act if isinstance(act, dict) else {
            "base_flavor": getattr(act, "base_flavor", ""),
            "quantity": getattr(act, "quantity", 0),
            "region_preference": getattr(act, "region_preference", None),
            "strict_region": getattr(act, "strict_region", False),
        }
        for field in ("base_flavor", "quantity", "region_preference", "strict_region"):
            exp_val = exp.get(field)
            act_val = act_dict.get(field)
            if exp_val != act_val:
                mismatches.append(f"item[{i}].{field}: expected={exp_val}, got={act_val}")

    if mismatches:
        return {"match": False, "detail": "; ".join(mismatches)}
    return {"match": True, "detail": "all items match"}


def _compare(classification, expected: dict) -> dict:
    """Compare classification result against expected values."""
    results = {}

    # situation
    if "situation" in expected:
        actual = classification.situation
        results["situation"] = {
            "expected": expected["situation"],
            "actual": actual,
            "match": actual == expected["situation"],
        }

    # dialog_intent
    if "dialog_intent" in expected:
        actual = classification.dialog_intent
        results["dialog_intent"] = {
            "expected": expected["dialog_intent"],
            "actual": actual,
            "match": actual == expected["dialog_intent"],
        }

    # needs_reply
    if "needs_reply" in expected:
        actual = classification.needs_reply
        results["needs_reply"] = {
            "expected": expected["needs_reply"],
            "actual": actual,
            "match": actual == expected["needs_reply"],
        }

    # client_email
    if "client_email" in expected:
        actual = (classification.client_email or "").lower()
        exp = expected["client_email"].lower()
        results["client_email"] = {
            "expected": exp,
            "actual": actual,
            "match": actual == exp,
        }

    # order_items presence
    actual_items = classification.order_items
    expected_items = expected.get("order_items")
    results["order_items_presence"] = {
        "expected": expected_items is not None,
        "actual": actual_items is not None,
        "match": (expected_items is not None) == (actual_items is not None),
    }

    # order_items shape
    items_cmp = _compare_order_items(actual_items, expected_items)
    results["order_items_shape"] = {
        "match": items_cmp["match"],
        "detail": items_cmp["detail"],
    }

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_eval(
    cases: list[dict],
    *,
    tags_filter: list[str] | None = None,
    verbose: bool = False,
) -> dict:
    """Run eval on cases and return structured results."""
    if tags_filter:
        cases = [c for c in cases if any(t in c.get("tags", []) for t in tags_filter)]

    all_results = []
    field_stats: dict[str, dict] = {}

    for case in cases:
        case_id = case["id"]
        expected = case["expected"]

        try:
            email_text = _build_eval_email_text(case)
            context_str = _build_eval_context(case)
            classification = run_classification(email_text, context_str)
            comparison = _compare(classification, expected)
            error = None
        except Exception as e:
            comparison = {}
            error = str(e)

        case_result = {
            "id": case_id,
            "tags": case.get("tags", []),
            "fields": comparison,
            "error": error,
            "pass": error is None and all(f.get("match", False) for f in comparison.values()),
        }
        all_results.append(case_result)

        # Accumulate per-field stats
        for field, result in comparison.items():
            if field not in field_stats:
                field_stats[field] = {"total": 0, "match": 0, "mismatch": 0}
            field_stats[field]["total"] += 1
            if result.get("match"):
                field_stats[field]["match"] += 1
            else:
                field_stats[field]["mismatch"] += 1

        # Print per-case result
        status = "PASS" if case_result["pass"] else "FAIL"
        if verbose or not case_result["pass"]:
            mismatches = [
                f for f, r in comparison.items() if not r.get("match")
            ]
            detail = f" [{', '.join(mismatches)}]" if mismatches else ""
            if error:
                detail = f" [ERROR: {error}]"
            print(f"  {status} {case_id}{detail}")
        elif verbose:
            print(f"  {status} {case_id}")

    # Tag-level stats
    tag_stats: dict[str, dict] = {}
    for r in all_results:
        for tag in r["tags"]:
            if tag not in tag_stats:
                tag_stats[tag] = {"total": 0, "pass": 0}
            tag_stats[tag]["total"] += 1
            if r["pass"]:
                tag_stats[tag]["pass"] += 1

    total = len(all_results)
    passed = sum(1 for r in all_results if r["pass"])

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": passed / total if total > 0 else 0,
        "field_stats": field_stats,
        "tag_stats": tag_stats,
        "cases": all_results,
    }


def print_report(results: dict) -> None:
    """Print human-readable eval report."""
    print(f"\n{'='*60}")
    print(f"CLASSIFIER EVAL REPORT")
    print(f"{'='*60}")
    print(f"Total: {results['total']}  Pass: {results['passed']}  Fail: {results['failed']}")
    print(f"Accuracy: {results['accuracy']:.1%}")

    print(f"\n--- Per-Field Accuracy ---")
    for field, stats in sorted(results["field_stats"].items()):
        acc = stats["match"] / stats["total"] if stats["total"] > 0 else 0
        print(f"  {field:25s}  {stats['match']}/{stats['total']}  ({acc:.0%})")

    print(f"\n--- Per-Tag Accuracy ---")
    for tag, stats in sorted(results["tag_stats"].items()):
        acc = stats["pass"] / stats["total"] if stats["total"] > 0 else 0
        print(f"  {tag:30s}  {stats['pass']}/{stats['total']}  ({acc:.0%})")

    # Show failures
    failures = [c for c in results["cases"] if not c["pass"]]
    if failures:
        print(f"\n--- Failures ({len(failures)}) ---")
        for f in failures:
            print(f"  {f['id']}:")
            if f.get("error"):
                print(f"    ERROR: {f['error']}")
            for field, result in f["fields"].items():
                if not result.get("match"):
                    exp = result.get("expected", "?")
                    act = result.get("actual", "?")
                    detail = result.get("detail", "")
                    if detail:
                        print(f"    {field}: {detail}")
                    else:
                        print(f"    {field}: expected={exp}, actual={act}")


def main():
    parser = argparse.ArgumentParser(description="Run classifier eval")
    parser.add_argument("--tags", nargs="+", help="Filter by tags")
    parser.add_argument("--save", action="store_true", help="Save results as baseline")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show all cases")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    cases = json.loads(EVAL_CASES_PATH.read_text())
    print(f"Loaded {len(cases)} eval cases from {EVAL_CASES_PATH.name}")

    results = run_eval(cases, tags_filter=args.tags, verbose=args.verbose)
    print_report(results)

    if args.save:
        BASELINE_PATH.write_text(json.dumps(results, indent=2, default=str))
        print(f"\nBaseline saved to {BASELINE_PATH}")

    sys.exit(0 if results["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
