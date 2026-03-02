"""
Isolated pipeline test — feed real email text through the full pipeline
but with empty conversation history and no ConversationState.

Simulates "first time we see this email" even if we already replied in prod.

Usage (inside container):
    python scripts/test_isolated.py "paste email text here"

Or with a file:
    python scripts/test_isolated.py --file /tmp/test_email.txt
"""

import argparse
import sys
from unittest.mock import patch


def run_isolated(email_text: str) -> str:
    """Run email through pipeline with mocked history/state."""

    # Patch BEFORE importing email_agent (module-level code runs on import)
    with (
        patch("db.memory.get_full_email_history", return_value=[]),
        patch("db.conversation_state.get_state", return_value=None),
        patch("db.conversation_state.save_state"),
        patch("db.memory.save_email"),
        patch("db.memory.save_order_items"),
        patch("utils.telegram.send_telegram"),
        # Also patch in agents.context (it imports directly)
        patch("agents.context.get_full_email_history", return_value=[]),
    ):
        from agents.email_agent import classify_and_process
        result = classify_and_process(email_text)

    return result


def main():
    parser = argparse.ArgumentParser(description="Isolated pipeline test")
    parser.add_argument("email", nargs="?", help="Email text (inline)")
    parser.add_argument("--file", "-f", help="Read email from file")
    args = parser.parse_args()

    if args.file:
        with open(args.file) as f:
            email_text = f.read()
    elif args.email:
        email_text = args.email
    else:
        print("Reading from stdin (Ctrl+D to finish)...")
        email_text = sys.stdin.read()

    if not email_text.strip():
        print("Error: empty email text")
        sys.exit(1)

    print("=" * 60)
    print("INPUT EMAIL")
    print("=" * 60)
    print(email_text[:500])
    print()
    print("=" * 60)
    print("PIPELINE OUTPUT")
    print("=" * 60)

    output = run_isolated(email_text)
    print(output)


if __name__ == "__main__":
    main()
