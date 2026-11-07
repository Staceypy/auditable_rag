"""Deliberately tamper with one audit event and show the verifier catches it.

    uv run python scripts/tamper_demo.py --yes [--seq N]

This permanently breaks the chain in the database it points at (see the
README for how to reset it). If the chain is empty, it first adds three demo
events. Then it:
  1. verifies the intact chain,
  2. shows app_user is refused an UPDATE (missing privilege),
  3. shows even the superuser is refused (append-only trigger),
  4. switches the triggers off as superuser and edits one payload,
  5. verifies again, which now reports the edited seq.

Exit code 0 if the tampering was detected, 1 otherwise, 2 without --yes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg import errors  # noqa: E402

from app import audit, config, db  # noqa: E402
from scripts import verify_chain  # noqa: E402

TAMPER_SQL = "UPDATE audit_events SET payload = payload || %s::jsonb WHERE seq = %s"
TAMPER_PATCH = {"tampered_by_demo": True}


def seed_demo_events(conn) -> None:
    for n in range(1, 4):
        question = f"Demo question {n}: what is the notice period for share transfers?"
        audit.append_event(
            conn,
            event_type="ai.answer_generated",
            tenant_id="demo-tenant",
            matter_id="tamper-demo",
            actor_id="demo-user",
            actor_role="lawyer",
            payload={"query_hash": audit.sha256_text(question), "note": "seeded by tamper_demo"},
            blobs=[question],
        )


def try_update(conn, label: str, seq: int) -> None:
    """Attempt a normal UPDATE and print why it was refused."""
    try:
        conn.execute(TAMPER_SQL, (json.dumps(TAMPER_PATCH), seq))
    except (errors.InsufficientPrivilege, errors.RaiseException) as exc:
        print(f"  {label}: refused ({type(exc).__name__}: {exc.diag.message_primary})")
    else:
        raise RuntimeError(f"{label} was able to UPDATE audit_events; schema is not protected")
    finally:
        conn.rollback()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tamper with one event to demo detection.")
    parser.add_argument("--app-url", default=config.APP_DATABASE_URL)
    parser.add_argument("--superuser-url", default=config.SUPERUSER_DATABASE_URL)
    parser.add_argument("--seq", type=int, help="event to tamper with (default: the middle one)")
    parser.add_argument("--yes", action="store_true",
                        help="confirm: this permanently breaks the chain in this database")
    args = parser.parse_args(argv)

    if not args.yes:
        print("This edits an audit event as superuser and permanently breaks the chain")
        print("in the target database. Re-run with --yes to proceed.")
        return 2

    with db.connect(args.app_url) as app_conn, db.connect(args.superuser_url) as super_conn:
        if not audit.load_chain(app_conn):
            print("Chain is empty; adding 3 demo events.")
            seed_demo_events(app_conn)

        print("\n1. Verify the intact chain")
        if verify_chain.main(["--database-url", args.app_url]) != 0:
            print("The chain is already broken; reset the database before running the demo.")
            return 1

        seqs = [e["seq"] for e in audit.load_chain(app_conn)]
        seq = args.seq if args.seq is not None else seqs[len(seqs) // 2]
        if seq not in seqs:
            print(f"No event with seq {seq}.")
            return 1

        print(f"\n2-3. Try a normal UPDATE of seq {seq}")
        try_update(app_conn, "app_user", seq)
        try_update(super_conn, "superuser", seq)

        print(f"\n4. Superuser switches triggers off and edits seq {seq}")
        with super_conn.transaction():
            # Superuser-only setting; ordinary triggers don't fire in replica mode.
            super_conn.execute("SET LOCAL session_replication_role = replica")
            super_conn.execute(TAMPER_SQL, (json.dumps(TAMPER_PATCH), seq))
        print(f"  merged {json.dumps(TAMPER_PATCH)} into the payload")

    print("\n5. Verify again")
    detected = verify_chain.main(["--database-url", args.app_url]) == 1
    print("\nTampering was detected." if detected else "\nTampering was NOT detected!")
    return 0 if detected else 1


if __name__ == "__main__":
    sys.exit(main())
