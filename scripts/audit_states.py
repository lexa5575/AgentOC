"""Audit conversation_states — dump all states for analysis.

Answers:
1. How many states exist? How many have pending_oos_resolution?
2. Which fields are actually populated vs empty/null?
3. How much data does the LLM State Updater actually produce?

Run:
    docker exec agentos-api python scripts/audit_states.py
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.models import ConversationState, get_session


def main():
    session = get_session()
    try:
        rows = session.query(ConversationState).order_by(
            ConversationState.updated_at.desc()
        ).all()

        print(f"\n{'='*60}")
        print(f"CONVERSATION STATES AUDIT")
        print(f"{'='*60}")
        print(f"Total states: {len(rows)}")

        if not rows:
            print("No states found.")
            return

        # --- Stats ---
        situations = Counter()
        statuses = Counter()
        has_pending_oos = 0
        has_promises = 0
        has_open_questions = 0
        has_summary = 0
        has_last_exchange = 0
        has_facts_order_id = 0
        has_facts_tracking = 0
        empty_states = 0
        fields_populated = Counter()  # field_name → count of non-empty

        all_states = []

        for row in rows:
            try:
                state = json.loads(row.state_json or "{}")
            except json.JSONDecodeError:
                state = {}

            all_states.append({
                "thread_id": row.gmail_thread_id,
                "client": row.client_email,
                "situation": row.last_situation,
                "msg_count": row.message_count,
                "updated": str(row.updated_at),
                "state": state,
            })

            if not state:
                empty_states += 1
                continue

            situations[row.last_situation] += 1
            statuses[state.get("status", "MISSING")] += 1

            facts = state.get("facts") or {}

            if facts.get("pending_oos_resolution"):
                has_pending_oos += 1
            if state.get("promises"):
                has_promises += 1
            if state.get("open_questions"):
                has_open_questions += 1
            if state.get("summary"):
                has_summary += 1
            if state.get("last_exchange"):
                le = state["last_exchange"]
                if le.get("we_said") or le.get("they_said"):
                    has_last_exchange += 1
            if facts.get("order_id"):
                has_facts_order_id += 1
            if facts.get("tracking_number"):
                has_facts_tracking += 1

            # Count all populated fields
            for key, val in state.items():
                if val and val != {} and val != []:
                    fields_populated[key] += 1
            for key, val in facts.items():
                if val and val != {} and val != []:
                    fields_populated[f"facts.{key}"] += 1

        total = len(rows)
        non_empty = total - empty_states

        print(f"Empty states: {empty_states}")
        print(f"Non-empty states: {non_empty}")

        print(f"\n--- Situations ---")
        for sit, cnt in situations.most_common():
            print(f"  {sit}: {cnt} ({cnt*100//non_empty}%)")

        print(f"\n--- Statuses (LLM-determined) ---")
        for st, cnt in statuses.most_common():
            print(f"  {st}: {cnt} ({cnt*100//non_empty}%)")

        print(f"\n--- Field Population (of {non_empty} non-empty states) ---")
        key_fields = [
            ("status", statuses.total()),
            ("topic", fields_populated.get("topic", 0)),
            ("summary", has_summary),
            ("promises", has_promises),
            ("open_questions", has_open_questions),
            ("last_exchange", has_last_exchange),
            ("facts.order_id", has_facts_order_id),
            ("facts.tracking_number", has_facts_tracking),
            ("facts.pending_oos_resolution", has_pending_oos),
        ]
        for name, cnt in key_fields:
            pct = cnt * 100 // non_empty if non_empty else 0
            marker = " ★ CRITICAL" if name == "facts.pending_oos_resolution" and cnt > 0 else ""
            print(f"  {name}: {cnt}/{non_empty} ({pct}%){marker}")

        print(f"\n--- All facts.* fields ---")
        for key, cnt in sorted(fields_populated.items()):
            if key.startswith("facts."):
                pct = cnt * 100 // non_empty if non_empty else 0
                print(f"  {key}: {cnt}/{non_empty} ({pct}%)")

        # --- Show 3 examples with pending_oos ---
        print(f"\n{'='*60}")
        print("EXAMPLES: States with pending_oos_resolution")
        print(f"{'='*60}")
        shown = 0
        for s in all_states:
            facts = s["state"].get("facts") or {}
            if facts.get("pending_oos_resolution"):
                print(f"\n--- {s['client']} (thread: {s['thread_id'][:20]}...) ---")
                print(f"Situation: {s['situation']}, Messages: {s['msg_count']}")
                print(json.dumps(s["state"], ensure_ascii=False, indent=2))
                shown += 1
                if shown >= 3:
                    break

        if shown == 0:
            print("  (none found)")

        # --- Show 3 latest examples ---
        print(f"\n{'='*60}")
        print("EXAMPLES: 3 most recent states")
        print(f"{'='*60}")
        for s in all_states[:3]:
            print(f"\n--- {s['client']} ({s['situation']}, {s['msg_count']} msgs, {s['updated']}) ---")
            print(json.dumps(s["state"], ensure_ascii=False, indent=2))

        # --- Dump all to JSON file ---
        dump_path = "/tmp/conversation_states_audit.json"
        with open(dump_path, "w") as f:
            json.dump(all_states, f, ensure_ascii=False, indent=2)
        print(f"\n✓ Full dump saved to {dump_path}")
        print(f"  Copy to local: docker cp agentos-api:{dump_path} ./conversation_states_audit.json")

    finally:
        session.close()


if __name__ == "__main__":
    main()
