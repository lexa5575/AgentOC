"""
Handler Eval Runner
-------------------

End-to-end evaluation of handler responses against golden reference replies.

Flow per case:
1. Build EmailClassification from golden case data
2. Build result dict with client_data from golden case
3. Call route_to_handler(classification, result, email_text)
4. Compare result["draft_reply"] with golden outbound_reply

Usage:
    # Run all stock_question cases from golden dataset
    python -m tests.eval.run_handler_eval --situation stock_question

    # Run with synthetic cases too
    python -m tests.eval.run_handler_eval --situation stock_question --include-synthetic

    # Run a specific case by id
    python -m tests.eval.run_handler_eval --case-id 1073

    # Offline mode (no Gmail API calls — stubs thread history)
    python -m tests.eval.run_handler_eval --situation stock_question --no-gmail
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-7s %(message)s",
)
logger = logging.getLogger(__name__)

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GOLDEN_PATH = PROJECT_ROOT / "golden_dataset.json"
SYNTHETIC_SQ_PATH = PROJECT_ROOT / "tests" / "eval" / "synthetic_stock_question_cases.json"


def _load_golden_cases(situation: str | None = None) -> list[dict]:
    """Load cases from golden_dataset.json, optionally filtered by situation."""
    with GOLDEN_PATH.open() as f:
        data = json.load(f)

    cases = []
    for sit, sit_cases in data.items():
        if situation and sit != situation:
            continue
        for case in sit_cases:
            if case.get("outbound_reply"):  # skip cases without reference reply
                cases.append(case)
    return cases


def _load_synthetic_cases(situation: str | None = None, min_score: int = 7) -> list[dict]:
    """Load synthetic cases, filtered by situation and min score."""
    synthetic_files = {
        "stock_question": SYNTHETIC_SQ_PATH,
    }

    cases = []
    for sit, path in synthetic_files.items():
        if situation and sit != situation:
            continue
        if not path.exists():
            continue
        with path.open() as f:
            raw = json.load(f)
        for case in raw:
            if case.get("score", 0) >= min_score:
                cases.append(case)
    return cases


def _classify_email(case: dict):
    """Run real classifier on email text to get proper classification.

    This is the e2e path — classifier extracts order_items, situation, etc.
    from the actual email text, just like production.
    """
    from agents.classifier import run_classification, compose_classifier_context

    email_text = case.get("inbound_text") or case.get("email_text") or ""
    state = case.get("conversation_state")
    context_str = compose_classifier_context(conversation_state=state)

    classification = run_classification(email_text, context_str, conversation_state=state)

    # Override situation to match golden case (we're testing the HANDLER, not classifier)
    classification.situation = case.get("situation", classification.situation)
    classification.needs_reply = True

    return classification


def _build_result(case: dict) -> dict:
    """Build the result dict that process_classified_email would produce."""
    client_profile = case.get("client_profile") or {}

    return {
        "needs_reply": True,
        "situation": case.get("situation", "stock_question"),
        "client_email": case.get("client_email", "unknown@example.com"),
        "client_name": client_profile.get("name"),
        "client_found": bool(client_profile.get("name")),
        "client_data": client_profile if client_profile.get("name") else None,
        "template_used": False,
        "draft_reply": None,
        "needs_routing": True,
        "stock_issue": None,
        "gmail_thread_id": case.get("gmail_thread_id"),
        "gmail_account": "default",
        "conversation_state": case.get("conversation_state"),
    }


def _extract_email_text(case: dict) -> str:
    """Get the inbound email text from the case."""
    return case.get("inbound_text") or case.get("email_text") or ""


def _compare_replies(actual: str, expected: str, case: dict) -> dict:
    """Compare actual handler reply with expected golden reply.

    Returns structured comparison result.
    """
    result = {
        "actual_reply": actual,
        "expected_reply": expected,
        "checks": {},
    }

    if not actual:
        result["checks"]["has_reply"] = False
        return result
    result["checks"]["has_reply"] = True

    # Check expected_reply_contains (for synthetic cases)
    contains = case.get("expected_reply_contains") or []
    missing = [kw for kw in contains if kw.lower() not in actual.lower()]
    if contains:
        result["checks"]["contains_keywords"] = {
            "expected": contains,
            "missing": missing,
            "pass": len(missing) == 0,
        }

    # Check expected_reply_not_contains (for synthetic cases)
    not_contains = case.get("expected_reply_not_contains") or []
    found_bad = [kw for kw in not_contains if kw.lower() in actual.lower()]
    if not_contains:
        result["checks"]["not_contains"] = {
            "banned": not_contains,
            "found": found_bad,
            "pass": len(found_bad) == 0,
        }

    # Basic quality checks
    result["checks"]["ends_with_thank_you"] = actual.strip().endswith("Thank you!") or actual.strip().endswith("Thanks!")
    result["checks"]["length_reasonable"] = 20 < len(actual) < 2000

    # Semantic comparison with golden reference using LLM-as-judge
    if expected and len(expected) > 10:
        judge_result = _llm_judge(actual, expected, case)
        result["checks"]["llm_judge"] = judge_result

    return result


def _llm_judge(actual: str, expected: str, case: dict) -> dict:
    """Use a cheap LLM to score actual vs expected reply.

    Returns: {"score": 1-10, "issues": [...], "pass": bool}
    """
    import json as _json
    from agno.agent import Agent
    from agno.models.openai import OpenAIResponses

    inbound = case.get("inbound_text") or case.get("email_text") or ""
    # Extract just body for judge context
    body = inbound.split("Body:", 1)[1].strip()[:300] if "Body:" in inbound else inbound[:300]

    judge_prompt = f"""You are a strict QA judge for an ecommerce customer service system.
Compare ACTUAL reply vs EXPECTED reply. Be STRICT — score reflects real business impact.

CUSTOMER ASKED: {body}

EXPECTED REPLY (gold standard):
{expected}

ACTUAL REPLY (system output):
{actual}

## Hard fail criteria (automatic score <= 4):
- HALLUCINATION: mentions products that don't exist or aren't in stock
- WRONG INFO: incorrect prices, wrong region, wrong warehouse
- IGNORED QUESTION: customer asked about X but reply doesn't address X
- GENERIC DUMP: lists entire catalog instead of answering the specific question

## Scoring criteria (1-10):
1-4: Hard fail (hallucinations, wrong info, ignored questions)
5-6: Major issues (missing key info, irrelevant alternatives, wrong tone)
7: Acceptable but imperfect (minor format issues, slight verbosity)
8-9: Good (covers all questions, correct products, good tone)
10: Perfect match with expected reply

## Specific checks:
- Are ALL products mentioned in ACTUAL real? (no invented names)
- Does ACTUAL cover every question the customer asked?
- Are alternatives relevant to customer's flavor profile?
- Is pricing mentioned only once per region (not per product)?
- If customer mentioned location/warehouse, does reply address it?

Return ONLY this JSON (no markdown):
{{"score": 7, "hard_fail": false, "issues": ["list of problems"], "verdict": "PASS or FAIL"}}

Score >= 8 = PASS, < 8 = FAIL. hard_fail=true for scores 1-4."""

    try:
        judge = Agent(
            id="eval-judge",
            model=OpenAIResponses(id="gpt-4o-mini"),
            markdown=False,
        )
        response = judge.run(judge_prompt)
        raw = response.content.strip()
        # Parse JSON
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = _json.loads(raw)
        score = data.get("score", 0)
        return {
            "score": score,
            "hard_fail": data.get("hard_fail", score <= 4),
            "issues": data.get("issues", []),
            "pass": score >= 8,
        }
    except Exception as e:
        logger.warning("LLM judge failed: %s", e)
        return {"score": 0, "issues": [f"judge error: {e}"], "pass": False}


def run_single_case(case: dict) -> dict:
    """Run a single case through the handler and compare."""
    from agents.router import route_to_handler

    case_id = case.get("id", "?")
    email_text = _extract_email_text(case)
    classification = _classify_email(case)
    result = _build_result(case)

    start = time.time()
    try:
        result = route_to_handler(classification, result, email_text)
        elapsed = time.time() - start
    except Exception as e:
        elapsed = time.time() - start
        return {
            "id": case_id,
            "status": "ERROR",
            "error": str(e),
            "elapsed_ms": int(elapsed * 1000),
        }

    actual_reply = result.get("draft_reply") or ""
    expected_reply = case.get("outbound_reply") or ""
    template_used = result.get("template_used", False)

    comparison = _compare_replies(actual_reply, expected_reply, case)

    return {
        "id": case_id,
        "status": "OK",
        "situation": case.get("situation"),
        "template_used": template_used,
        "fallback_triggered": result.get("fallback_triggered", False),
        "elapsed_ms": int(elapsed * 1000),
        "comparison": comparison,
    }


def _install_gmail_stubs():
    """Patch Gmail-dependent functions so eval runs without Gmail API access.

    Stubs:
    - tools.gmail.get_full_thread_messages → returns []
    - db.email_history.get_full_thread_history → returns local DB data only
    """
    from unittest.mock import patch
    import db.email_history as eh

    # Stub the Gmail thread fetch (used by context builder for OOS replies)
    if hasattr(eh, "_fetch_gmail_thread"):
        patch.object(eh, "_fetch_gmail_thread", return_value=[]).start()

    # Stub tools.gmail if it exists
    try:
        import tools.gmail as gmail_mod
        if hasattr(gmail_mod, "get_full_thread_messages"):
            patch.object(gmail_mod, "get_full_thread_messages", return_value=[]).start()
        if hasattr(gmail_mod, "get_thread_messages"):
            patch.object(gmail_mod, "get_thread_messages", return_value=[]).start()
    except ImportError:
        pass


def main():
    parser = argparse.ArgumentParser(description="Handler eval runner")
    parser.add_argument("--situation", type=str, default=None,
                        help="Filter by situation (e.g. stock_question)")
    parser.add_argument("--case-id", type=str, default=None,
                        help="Run a specific case by id")
    parser.add_argument("--include-synthetic", action="store_true",
                        help="Include synthetic cases (score >= 7)")
    parser.add_argument("--output", type=str, default=None,
                        help="Save detailed results to JSON file")
    parser.add_argument("--no-gmail", action="store_true",
                        help="Offline mode: stub Gmail API calls (no external requests)")
    args = parser.parse_args()

    # Offline mode: patch Gmail-dependent functions to avoid external API calls
    if args.no_gmail:
        _install_gmail_stubs()
        logger.info("Offline mode: Gmail API calls stubbed")

    # Load cases
    golden = _load_golden_cases(args.situation)
    synthetic = _load_synthetic_cases(args.situation) if args.include_synthetic else []
    all_cases = golden + synthetic

    if args.case_id:
        all_cases = [c for c in all_cases if str(c.get("id")) == args.case_id]

    if not all_cases:
        print(f"No cases found for situation={args.situation}, case_id={args.case_id}")
        sys.exit(1)

    print(f"Running handler eval: {len(golden)} golden + {len(synthetic)} synthetic = {len(all_cases)} cases\n")

    results = []
    for case in all_cases:
        case_id = case.get("id", "?")
        r = run_single_case(case)
        results.append(r)

        # Print result
        status = r["status"]
        elapsed = r.get("elapsed_ms", 0)

        if status == "ERROR":
            print(f"  ERROR {case_id}: {r['error']}")
            continue

        comp = r["comparison"]
        actual = comp["actual_reply"][:100] if comp.get("actual_reply") else "(empty)"
        template = "TPL" if r.get("template_used") else "LLM"

        # Determine pass/fail from LLM judge
        checks = comp.get("checks", {})
        judge = checks.get("llm_judge", {})
        judge_score = judge.get("score", "?")
        judge_pass = judge.get("pass", True)
        judge_issues = judge.get("issues", [])

        if not checks.get("has_reply", True):
            print(f"  FAIL  {case_id} [{template}] ({elapsed}ms) — no reply generated")
        elif not judge_pass:
            print(f"  FAIL  {case_id} [{template}] ({elapsed}ms) — score={judge_score}/10")
            for issue in judge_issues:
                print(f"         - {issue}")
        else:
            print(f"  PASS  {case_id} [{template}] ({elapsed}ms) — score={judge_score}/10")

        # Always show actual vs expected for review
        print(f"         ACTUAL:   {actual}")
        expected = (case.get("outbound_reply") or "")[:100]
        if expected:
            print(f"         EXPECTED: {expected}")
        print()

    # Summary
    total = len(results)
    errors = sum(1 for r in results if r["status"] == "ERROR")
    completed = total - errors
    template_count = sum(1 for r in results if r.get("template_used"))
    llm_count = completed - template_count

    # Count pass/fail from judge
    passed = sum(1 for r in results
                 if r["status"] == "OK"
                 and r.get("comparison", {}).get("checks", {}).get("llm_judge", {}).get("pass", True))
    failed = completed - passed
    hard_fails = sum(1 for r in results
                     if r["status"] == "OK"
                     and r.get("comparison", {}).get("checks", {}).get("llm_judge", {}).get("hard_fail", False))

    print(f"\n{'='*60}")
    print(f"SUMMARY: {passed} PASS / {failed} FAIL / {errors} ERROR (out of {total})")
    if hard_fails:
        print(f"  HARD FAILS (hallucinations/wrong info): {hard_fails}")
    print(f"  Template replies: {template_count}")
    print(f"  LLM replies: {llm_count}")

    # Fallback rate (only for LLM cases)
    llm_results = [r for r in results if r["status"] == "OK" and not r.get("template_used")]
    fallback_count = sum(1 for r in llm_results if r.get("fallback_triggered"))
    if llm_results:
        print(f"  Fallback: {fallback_count}/{len(llm_results)} LLM cases ({fallback_count/len(llm_results)*100:.0f}%)")

    scores = [r.get("comparison", {}).get("checks", {}).get("llm_judge", {}).get("score", 0)
              for r in results if r["status"] == "OK"]
    avg_score = sum(scores) / len(scores) if scores else 0
    print(f"  Average judge score: {avg_score:.1f}/10 (threshold: >= 8)")

    # Save detailed results
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nDetailed results saved to: {args.output}")


if __name__ == "__main__":
    main()
