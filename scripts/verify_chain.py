"""Recompute the audit hash chain and report the first broken event.

    uv run python scripts/verify_chain.py [--expect-head SEQ:HASH]

Exit code 0 if the chain verifies, 1 if it doesn't.

Read-only: it connects as app_user by default and writes nothing, so running
it doesn't add events to the chain it checks.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit, config, db  # noqa: E402


def parse_head(value: str) -> tuple[int, str]:
    """Parse --expect-head, printed by a previous run as SEQ:HASH."""
    seq, sep, head_hash = value.partition(":")
    if not sep or not seq.isdigit() or len(head_hash) != 64:
        raise argparse.ArgumentTypeError("expected SEQ:HASH, e.g. 12:3f9a... (64 hex chars)")
    return int(seq), head_hash


def print_result(result: audit.VerifyResult) -> None:
    if not result.ok:
        print(f"BROKEN at seq {result.broken_seq}: {result.reason}")
        print(f"(events before it verified: {result.checked})")
        return
    print(f"OK: {result.checked} events verified")
    if result.head_seq is not None:
        print(f"head: {result.head_seq}:{result.head_hash}")
        print("  Save this head outside the database and pass it as --expect-head next time;")
        print("  that is what reveals events deleted from the end of the chain.")
    if result.redacted_blobs:
        print(f"redacted blobs: {len(result.redacted_blobs)} "
              "(text removed, e.g. for GDPR; the events still verify)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the audit_events hash chain.")
    parser.add_argument("--database-url", default=config.APP_DATABASE_URL,
                        help="defaults to APP_DATABASE_URL (read-only use)")
    parser.add_argument("--expect-head", type=parse_head, metavar="SEQ:HASH",
                        help="head printed by an earlier run; fails if it's gone or changed")
    args = parser.parse_args(argv)

    with db.connect(args.database_url) as conn:
        result = audit.verify_database(conn, expect_head=args.expect_head)
    print_result(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
