#!/usr/bin/env python3
"""
Build golden eval subsets from:
1) golden_dataset.json (source cases)
2) manual_validation_master_decisions.json (manual audit labels)

Outputs machine-readable subsets for testing prompt/handler changes:
- accept (safe for quality scoring)
- bug (known bad behavior to fix)
- pending (valid intent, but no final workflow output yet)
- exclude (do not use in eval score)
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


DECISION_TO_BUCKET = {
    "KEEP_MANUAL_CONFIRMED": "accept_manual",
    "KEEP_BUG_CASE": "bug",
    "PENDING_WORKFLOW": "pending",
    "DROP_EXCLUDE": "exclude",
}


def _flatten_golden(golden: dict) -> list[dict]:
    rows: list[dict] = []
    for situation, cases in golden.items():
        for case in cases:
            row = dict(case)
            row["situation"] = situation
            rows.append(row)
    return rows


def _load_json(path: Path) -> dict | list:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_sets(golden_path: Path, decisions_path: Path, out_dir: Path) -> dict:
    golden = _load_json(golden_path)
    decisions = _load_json(decisions_path)

    if not isinstance(golden, dict):
        raise ValueError(f"Expected dict in {golden_path}, got {type(golden).__name__}")
    if not isinstance(decisions, list):
        raise ValueError(f"Expected list in {decisions_path}, got {type(decisions).__name__}")

    flat = _flatten_golden(golden)
    by_id = {row["id"]: row for row in flat}

    manual_decision_by_id = {}
    manual_note_by_id = {}
    for row in decisions:
        cid = row.get("id")
        decision = row.get("decision")
        if cid is None or not decision:
            continue
        manual_decision_by_id[cid] = decision
        manual_note_by_id[cid] = row.get("note") or ""

    all_cases: list[dict] = []
    accept_cases: list[dict] = []
    accept_manual_cases: list[dict] = []
    accept_auto_cases: list[dict] = []
    bug_cases: list[dict] = []
    pending_cases: list[dict] = []
    exclude_cases: list[dict] = []

    for cid in sorted(by_id):
        case = dict(by_id[cid])
        decision = manual_decision_by_id.get(cid, "AUTO_ACCEPT")
        bucket = DECISION_TO_BUCKET.get(decision, "accept_auto")
        note = manual_note_by_id.get(cid, "")

        case["validation"] = {
            "decision": decision,
            "bucket": bucket,
            "note": note,
            "source": "manual" if cid in manual_decision_by_id else "auto_unreviewed",
        }
        all_cases.append(case)

        if bucket in {"accept_manual", "accept_auto"}:
            accept_cases.append(case)
            if bucket == "accept_manual":
                accept_manual_cases.append(case)
            else:
                accept_auto_cases.append(case)
        elif bucket == "bug":
            bug_cases.append(case)
        elif bucket == "pending":
            pending_cases.append(case)
        elif bucket == "exclude":
            exclude_cases.append(case)

    def _count_by_situation(rows: list[dict]) -> dict[str, int]:
        c = Counter(r["situation"] for r in rows)
        return dict(sorted(c.items()))

    summary = {
        "inputs": {
            "golden_path": str(golden_path),
            "decisions_path": str(decisions_path),
        },
        "counts": {
            "all": len(all_cases),
            "accept": len(accept_cases),
            "accept_manual": len(accept_manual_cases),
            "accept_auto": len(accept_auto_cases),
            "bug": len(bug_cases),
            "pending": len(pending_cases),
            "exclude": len(exclude_cases),
        },
        "counts_by_situation": {
            "accept": _count_by_situation(accept_cases),
            "bug": _count_by_situation(bug_cases),
            "pending": _count_by_situation(pending_cases),
            "exclude": _count_by_situation(exclude_cases),
        },
        "decision_breakdown_manual": dict(
            sorted(Counter(manual_decision_by_id.values()).items())
        ),
    }

    _write_json(out_dir / "all_cases_with_validation.json", all_cases)
    _write_json(out_dir / "accept_cases.json", accept_cases)
    _write_json(out_dir / "accept_manual_cases.json", accept_manual_cases)
    _write_json(out_dir / "accept_auto_cases.json", accept_auto_cases)
    _write_json(out_dir / "bug_cases.json", bug_cases)
    _write_json(out_dir / "pending_cases.json", pending_cases)
    _write_json(out_dir / "exclude_cases.json", exclude_cases)
    _write_json(out_dir / "summary.json", summary)

    md_lines = [
        "# Golden Eval Sets Summary",
        "",
        "## Counts",
        f"- all: {summary['counts']['all']}",
        f"- accept: {summary['counts']['accept']}",
        f"- accept_manual: {summary['counts']['accept_manual']}",
        f"- accept_auto: {summary['counts']['accept_auto']}",
        f"- bug: {summary['counts']['bug']}",
        f"- pending: {summary['counts']['pending']}",
        f"- exclude: {summary['counts']['exclude']}",
        "",
        "## Accept By Situation",
    ]
    for situation, cnt in summary["counts_by_situation"]["accept"].items():
        md_lines.append(f"- {situation}: {cnt}")
    md_lines.extend(["", "## Bug By Situation"])
    for situation, cnt in summary["counts_by_situation"]["bug"].items():
        md_lines.append(f"- {situation}: {cnt}")
    md_lines.extend(["", "## Pending By Situation"])
    for situation, cnt in summary["counts_by_situation"]["pending"].items():
        md_lines.append(f"- {situation}: {cnt}")
    md_lines.extend(["", "## Exclude By Situation"])
    for situation, cnt in summary["counts_by_situation"]["exclude"].items():
        md_lines.append(f"- {situation}: {cnt}")
    md_lines.extend(["", "## Manual Decision Breakdown"])
    for decision, cnt in summary["decision_breakdown_manual"].items():
        md_lines.append(f"- {decision}: {cnt}")
    md_lines.append("")

    (out_dir / "summary.md").write_text("\n".join(md_lines), encoding="utf-8")
    return summary


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Build golden eval subsets")
    parser.add_argument(
        "--golden",
        type=Path,
        default=repo_root / "golden_dataset.json",
        help="Path to golden_dataset.json",
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=repo_root / "docs" / "manual_validation_master_decisions.json",
        help="Path to manual_validation_master_decisions.json",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=repo_root / "docs" / "eval_sets",
        help="Output directory for generated subsets",
    )
    args = parser.parse_args()

    summary = build_sets(args.golden, args.decisions, args.out_dir)
    print("Golden eval sets generated.")
    print(json.dumps(summary["counts"], ensure_ascii=False, indent=2))
    print(f"Output dir: {args.out_dir}")


if __name__ == "__main__":
    main()
