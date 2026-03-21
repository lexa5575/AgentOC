#!/usr/bin/env python3
"""
Generate synthetic classifier eval cases with quality score (1-10).

Policy:
- score >= 7: auto-ready for regression eval
- score < 7: requires manual review

Outputs:
- tests/eval/generated/synthetic_cases_all.json
- tests/eval/generated/synthetic_cases_ready.json
- tests/eval/generated/synthetic_cases_review_required.json
- tests/eval/generated/classifier_eval_cases_extended.json
- docs/synthetic_review_queue.md
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import re
from collections import Counter
from pathlib import Path


def _read_json(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected list JSON in {path}")
    return data


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _extract_parts(case: dict) -> tuple[str, str, str]:
    text = case.get("email_text") or ""
    from_email = (
        (case.get("expected") or {}).get("client_email")
        or case.get("client_email")
        or "synthetic.user@example.com"
    )
    subject = "Re: Message"
    body = text

    m_from = re.search(r"^From:\s*(.+)$", text, flags=re.MULTILINE)
    if m_from:
        from_line = m_from.group(1).strip()
        email_match = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", from_line)
        if email_match:
            from_email = email_match.group(1)

    m_subject = re.search(r"^Subject:\s*(.+)$", text, flags=re.MULTILINE)
    if m_subject:
        subject = m_subject.group(1).strip()

    if "Body:" in text:
        body = text.split("Body:", 1)[1].strip()
    return from_email, subject, body


def _compose_email(from_email: str, subject: str, body: str) -> str:
    return f"From: {from_email}\nSubject: {subject}\nBody: {body.strip()}"


def _apply_typos(body: str, rng: random.Random) -> str:
    swaps = [
        ("please", "pls"),
        ("thanks", "thx"),
        ("order", "ordr"),
        ("payment", "paymnet"),
        ("tracking", "traking"),
        ("received", "recieved"),
        ("hello", "helo"),
    ]
    out = body
    rng.shuffle(swaps)
    for src, dst in swaps[: rng.randint(2, 4)]:
        out = re.sub(rf"\b{re.escape(src)}\b", dst, out, flags=re.IGNORECASE)
    return out


def _high_typo_noise(case: dict, rng: random.Random) -> tuple[str, int, str, list[str]]:
    from_email, subject, body = _extract_parts(case)
    body2 = _apply_typos(body, rng)
    body2 += "\n\nSent from my iPhone"
    return (
        _compose_email(from_email, subject, body2),
        8,
        "typo_noise",
        ["minor typos", "mobile-style signature"],
    )


def _high_quoted_tail(case: dict, rng: random.Random) -> tuple[str, int, str, list[str]]:
    from_email, subject, body = _extract_parts(case)
    quoted = (
        "\n\nOn Tue, Mar 17, 2026 at 9:12 AM Support <order@shipmecarton.com> wrote:"
        "\n> Please confirm details"
        "\n> Thank you!"
    )
    if rng.random() < 0.5:
        quoted += "\n> Tracking will update later"
    return (
        _compose_email(from_email, subject, body + quoted),
        9,
        "quoted_tail",
        ["quoted thread noise"],
    )


def _low_mixed_language(case: dict, rng: random.Random) -> tuple[str, int, str, list[str]]:
    from_email, subject, body = _extract_parts(case)
    suffixes = [
        "grazie",
        "merci",
        "spasibo",
        "molim",
    ]
    body2 = body + f"\n\n{rng.choice(suffixes)}. Can u confirm?"
    return (
        _compose_email(from_email, subject, body2),
        6,
        "mixed_language",
        ["mixed language", "ambiguous short question"],
    )


def _low_intent_collision(case: dict, rng: random.Random) -> tuple[str, int, str, list[str]]:
    from_email, subject, body = _extract_parts(case)
    situation = (case.get("expected") or {}).get("situation") or "other"
    collisions = {
        "new_order": "Also where is my previous tracking number?",
        "tracking": "Also I want 2 boxes of Yellow if available.",
        "payment_question": "Also do you have Green EU in stock?",
        "stock_question": "Also I already paid yesterday, can you check?",
        "oos_followup": "Also can I get discount if I take 5 boxes?",
    }
    extra = collisions.get(situation, "Also can you confirm total and shipping date?")
    body2 = f"{body}\n\n{extra}"
    return (
        _compose_email(from_email, subject, body2),
        5,
        "intent_collision",
        ["mixed intent in one message", "routing ambiguity likely"],
    )


def _low_minimal_body(case: dict, rng: random.Random) -> tuple[str, int, str, list[str]]:
    from_email, subject, body = _extract_parts(case)
    minimal_pool = ["ok", "yes", "pls check", "?", "paid"]
    body2 = rng.choice(minimal_pool)
    if rng.random() < 0.5:
        body2 += "\n\n" + body[:80]
    return (
        _compose_email(from_email, subject, body2),
        4,
        "minimal_body",
        ["too short", "insufficient intent signal"],
    )


def _build_case(
    base_case: dict,
    new_id: str,
    new_text: str,
    score: int,
    profile: str,
    reasons: list[str],
) -> dict:
    c = copy.deepcopy(base_case)
    c["id"] = new_id
    c["email_text"] = new_text
    tags = list(c.get("tags") or [])
    tags.extend(["synthetic", f"profile:{profile}", f"score:{score}"])
    c["tags"] = tags
    c["synthetic_meta"] = {
        "score": score,
        "profile": profile,
        "reasons": reasons,
        "requires_manual_review": score < 7,
    }
    return c


def _build_edge_templates() -> list[dict]:
    # Deliberately difficult, all require manual review by default.
    edge = []
    edge.append(
        {
            "id": "edge_new_order_vs_tracking_mix",
            "tags": ["synthetic", "edge_case", "mixed_intent"],
            "email_text": (
                "From: edge.user1@example.com\n"
                "Subject: Re: Order\n"
                "Body: I want 2 Green EU, and where is my last package tracking?"
            ),
            "conversation_state": None,
            "thread_history": None,
            "other_threads": None,
            "combined_messages": None,
            "expected": {
                "situation": "new_order",
                "dialog_intent": None,
                "needs_reply": True,
                "client_email": "edge.user1@example.com",
                "order_items": [
                    {"base_flavor": "Green", "quantity": 2, "region_preference": ["EU"], "strict_region": False}
                ],
            },
            "synthetic_meta": {
                "score": 5,
                "profile": "edge_template",
                "reasons": ["mixed intent (order + tracking)"],
                "requires_manual_review": True,
            },
        }
    )
    edge.append(
        {
            "id": "edge_payment_screenshot_style",
            "tags": ["synthetic", "edge_case", "payment_question"],
            "email_text": (
                "From: edge.user2@example.com\n"
                "Subject: Re: Payment\n"
                "Body: [attachment: screenshot.png]"
            ),
            "conversation_state": None,
            "thread_history": None,
            "other_threads": None,
            "combined_messages": None,
            "expected": {
                "situation": "other",
                "dialog_intent": None,
                "needs_reply": False,
                "client_email": "edge.user2@example.com",
                "order_items": None,
            },
            "synthetic_meta": {
                "score": 4,
                "profile": "edge_template",
                "reasons": ["attachment-only style message"],
                "requires_manual_review": True,
            },
        }
    )
    edge.append(
        {
            "id": "edge_oos_soft_agreement",
            "tags": ["synthetic", "edge_case", "oos_followup"],
            "email_text": (
                "From: edge.user3@example.com\n"
                "Subject: Re: Stock\n"
                "Body: hmm ok maybe that replacement can work if same price"
            ),
            "conversation_state": {
                "status": "awaiting_response",
                "topic": "out_of_stock",
                "facts": {
                    "pending_oos_resolution": {
                        "original": [{"base_flavor": "Turquoise", "quantity": 2}],
                        "alternatives": [{"base_flavor": "Silver", "quantity": 2}],
                    }
                },
            },
            "thread_history": None,
            "other_threads": None,
            "combined_messages": None,
            "expected": {
                "situation": "oos_followup",
                "dialog_intent": "agrees_to_alternative",
                "needs_reply": True,
                "client_email": "edge.user3@example.com",
                "order_items": [{"base_flavor": "Silver", "quantity": 2, "region_preference": None, "strict_region": False}],
            },
            "synthetic_meta": {
                "score": 6,
                "profile": "edge_template",
                "reasons": ["soft/conditional agreement wording"],
                "requires_manual_review": True,
            },
        }
    )
    return edge


def generate(
    base_cases: list[dict],
    seed: int,
    high_per_base: int,
    low_per_base: int,
    max_review_cases: int,
) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)

    high_profiles = [_high_typo_noise, _high_quoted_tail]
    low_profiles = [_low_mixed_language, _low_intent_collision, _low_minimal_body]

    synthetic_ready: list[dict] = []
    synthetic_review: list[dict] = []
    review_candidates: list[dict] = []

    for idx, base in enumerate(base_cases):
        if not base.get("email_text"):
            continue
        base_id = str(base.get("id", f"case_{idx}"))

        for i in range(high_per_base):
            profile_fn = high_profiles[(idx + i) % len(high_profiles)]
            new_text, score, profile, reasons = profile_fn(base, rng)
            cid = f"syn_{base_id}__{profile}__h{i+1}"
            synthetic_ready.append(_build_case(base, cid, new_text, score, profile, reasons))

        for i in range(low_per_base):
            profile_fn = low_profiles[(idx + i) % len(low_profiles)]
            new_text, score, profile, reasons = profile_fn(base, rng)
            cid = f"syn_{base_id}__{profile}__l{i+1}"
            review_candidates.append(_build_case(base, cid, new_text, score, profile, reasons))

    rng.shuffle(review_candidates)
    synthetic_review = review_candidates[:max_review_cases]
    synthetic_review.extend(_build_edge_templates())
    return synthetic_ready, synthetic_review


def _write_review_md(path: Path, review_cases: list[dict]) -> None:
    lines = [
        "# Synthetic Cases Requiring Manual Review",
        "",
        "Rule: all synthetic cases with `score < 7` must be reviewed before scoring.",
        "",
        "| ID | Score | Proposed Situation | Client Email | Reasons |",
        "|---|---|---|---|---|",
    ]
    for case in review_cases:
        meta = case.get("synthetic_meta") or {}
        score = meta.get("score", "?")
        reasons = ", ".join(meta.get("reasons") or [])
        exp = case.get("expected") or {}
        lines.append(
            "| {id} | {score} | {sit} | {email} | {reasons} |".format(
                id=case.get("id"),
                score=score,
                sit=exp.get("situation", "-"),
                email=exp.get("client_email", "-"),
                reasons=reasons or "-",
            )
        )

    lines.append("")
    lines.append("## Preview")
    for case in review_cases[:20]:
        meta = case.get("synthetic_meta") or {}
        lines.append(f"- `{case.get('id')}` score={meta.get('score')} :: {(case.get('email_text') or '')[:140]}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Generate synthetic classifier eval cases with scoring")
    parser.add_argument(
        "--base",
        type=Path,
        default=repo_root / "tests" / "eval" / "classifier_eval_cases.json",
        help="Base eval cases JSON",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=repo_root / "tests" / "eval" / "generated",
        help="Output directory for generated files",
    )
    parser.add_argument(
        "--review-md",
        type=Path,
        default=repo_root / "docs" / "synthetic_review_queue.md",
        help="Markdown file with low-score review queue",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--high-per-base", type=int, default=1)
    parser.add_argument("--low-per-base", type=int, default=1)
    parser.add_argument("--max-review-cases", type=int, default=30)
    args = parser.parse_args()

    base_cases = _read_json(args.base)
    ready, review = generate(
        base_cases=base_cases,
        seed=args.seed,
        high_per_base=args.high_per_base,
        low_per_base=args.low_per_base,
        max_review_cases=args.max_review_cases,
    )

    all_syn = ready + review
    extended = base_cases + ready

    args.out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(args.out_dir / "synthetic_cases_ready.json", ready)
    _write_json(args.out_dir / "synthetic_cases_review_required.json", review)
    _write_json(args.out_dir / "synthetic_cases_all.json", all_syn)
    _write_json(args.out_dir / "classifier_eval_cases_extended.json", extended)
    _write_review_md(args.review_md, review)

    score_dist = Counter((c.get("synthetic_meta") or {}).get("score", 0) for c in all_syn)
    print("Synthetic generation complete.")
    print(f"Base cases: {len(base_cases)}")
    print(f"Synthetic ready (score >=7): {len(ready)}")
    print(f"Synthetic review (score <7): {len(review)}")
    print(f"Extended eval cases (base + ready): {len(extended)}")
    print(f"Review queue: {args.review_md}")
    print(f"Score distribution: {dict(sorted(score_dist.items()))}")


if __name__ == "__main__":
    main()
