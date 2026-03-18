"""Collect golden dataset for LLM handler evaluation.

For each LLM handler situation, pulls inbound email + outbound reply pairs
from email_history. Groups by gmail_thread_id to get (customer_email → our_reply).

Run:
    docker exec agentos-api python scripts/collect_golden_dataset.py

Output:
    /tmp/golden_dataset.json — structured dataset for eval
    Console — summary + examples
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.models import EmailHistory, Client, get_session


# Situations where LLM handler is used (these need eval data)
LLM_SITUATIONS = [
    "tracking",
    "payment_question",
    "discount_request",
    "shipping_timeline",
    "stock_question",
    "other",           # general handler
    "oos_followup",    # LLM fallback path
]

# Also collect template situations for comparison
TEMPLATE_SITUATIONS = [
    "new_order",
    "payment_received",
    "price_question",
]

ALL_SITUATIONS = LLM_SITUATIONS + TEMPLATE_SITUATIONS
MAX_PER_SITUATION = 20


def main():
    session = get_session()
    try:
        # Step 1: Get all inbound emails grouped by situation
        inbound = (
            session.query(EmailHistory)
            .filter(EmailHistory.direction == "inbound")
            .filter(EmailHistory.situation.in_(ALL_SITUATIONS))
            .order_by(EmailHistory.created_at.desc())
            .all()
        )

        # Step 2: For each inbound, find the next outbound in same thread
        dataset = defaultdict(list)

        for email in inbound:
            situation = email.situation
            if len(dataset[situation]) >= MAX_PER_SITUATION:
                continue

            # Find outbound reply in same thread
            reply = None
            if email.gmail_thread_id:
                reply = (
                    session.query(EmailHistory)
                    .filter(
                        EmailHistory.gmail_thread_id == email.gmail_thread_id,
                        EmailHistory.direction == "outbound",
                        EmailHistory.created_at > email.created_at,
                    )
                    .order_by(EmailHistory.created_at.asc())
                    .first()
                )

            # Get client profile
            client = None
            if email.client_email:
                client_row = (
                    session.query(Client)
                    .filter(Client.email == email.client_email.lower().strip())
                    .first()
                )
                if client_row:
                    client = client_row.to_dict()

            dataset[situation].append({
                "id": email.id,
                "situation": situation,
                "client_email": email.client_email,
                "client_profile": client,
                "subject": email.subject or "",
                "inbound_text": email.body or "",
                "outbound_reply": reply.body if reply else None,
                "reply_situation": reply.situation if reply else None,
                "gmail_thread_id": email.gmail_thread_id,
                "created_at": str(email.created_at),
            })

        # Step 3: Print summary
        print(f"\n{'='*60}")
        print("GOLDEN DATASET COLLECTION")
        print(f"{'='*60}")

        total = 0
        for situation in ALL_SITUATIONS:
            examples = dataset.get(situation, [])
            with_reply = sum(1 for e in examples if e["outbound_reply"])
            total += len(examples)
            marker = " ★ LLM" if situation in LLM_SITUATIONS else ""
            print(f"  {situation:20s}: {len(examples):3d} examples ({with_reply} with reply){marker}")

        print(f"\n  Total: {total} examples")

        # Step 4: Show examples for each LLM situation
        for situation in LLM_SITUATIONS:
            examples = dataset.get(situation, [])
            if not examples:
                continue

            print(f"\n{'='*60}")
            print(f"EXAMPLES: {situation} ({len(examples)} total)")
            print(f"{'='*60}")

            for ex in examples[:3]:
                print(f"\n--- {ex['client_email']} ({ex['created_at'][:10]}) ---")

                # Show inbound (truncated)
                inbound_preview = ex["inbound_text"][:300]
                print(f"CUSTOMER: {inbound_preview}")

                # Show reply (truncated)
                if ex["outbound_reply"]:
                    reply_preview = ex["outbound_reply"][:300]
                    print(f"OUR REPLY: {reply_preview}")
                else:
                    print("OUR REPLY: (none)")

                # Show client info
                if ex["client_profile"]:
                    c = ex["client_profile"]
                    print(f"CLIENT: {c.get('name', '?')}, {c.get('payment_type', '?')}, "
                          f"discount={c.get('discount_percent', 0)}%")

        # Step 5: Save to JSON
        dump_path = "/tmp/golden_dataset.json"
        with open(dump_path, "w") as f:
            json.dump(dict(dataset), f, ensure_ascii=False, indent=2)

        print(f"\n✓ Full dataset saved to {dump_path}")
        print(f"  Copy to local: docker cp agentos-api:{dump_path} ./golden_dataset.json")

    finally:
        session.close()


if __name__ == "__main__":
    main()
