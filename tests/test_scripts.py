"""verify_chain.py and tamper_demo.py, run against the test database."""
import subprocess
import sys
from pathlib import Path

import pytest

from app import audit
from scripts import tamper_demo, verify_chain
from tests.conftest import TEST_APP_URL, TEST_SUPERUSER_URL
from tests.test_chain import append, tamper

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ["--database-url", TEST_APP_URL]
DEMO = ["--app-url", TEST_APP_URL, "--superuser-url", TEST_SUPERUSER_URL]


@pytest.fixture
def chain(app_conn):
    return [append(app_conn, n) for n in range(3)]


# --- verify_chain.py ----------------------------------------------------------


def test_verify_ok(chain, capsys):
    assert verify_chain.main(VERIFY) == 0
    out = capsys.readouterr().out
    assert "OK: 3 events verified" in out
    assert f"head: {chain[-1]['seq']}:{chain[-1]['hash']}" in out


def test_verify_reports_first_broken_seq(chain, super_conn, capsys):
    tamper(super_conn, "UPDATE audit_events SET payload = '{}' WHERE seq = %s",
           (chain[1]["seq"],))
    assert verify_chain.main(VERIFY) == 1
    assert f"BROKEN at seq {chain[1]['seq']}" in capsys.readouterr().out


def test_verify_expect_head_catches_tail_deletion(chain, super_conn, capsys):
    head = f"{chain[-1]['seq']}:{chain[-1]['hash']}"
    tamper(super_conn, "DELETE FROM audit_events WHERE seq = %s", (chain[-1]["seq"],))
    assert verify_chain.main(VERIFY) == 0
    assert verify_chain.main([*VERIFY, "--expect-head", head]) == 1
    assert "deleted from the end" in capsys.readouterr().out


def test_verify_rejects_malformed_head():
    with pytest.raises(SystemExit) as exc:
        verify_chain.main([*VERIFY, "--expect-head", "not-a-head"])
    assert exc.value.code == 2


def test_verify_reports_redacted_blobs(chain, super_conn, capsys):
    super_conn.execute("DELETE FROM audit_blobs WHERE hash = %s",
                       (chain[0]["payload"]["query_hash"],))
    super_conn.commit()
    assert verify_chain.main(VERIFY) == 0
    assert "redacted blobs: 1" in capsys.readouterr().out


@pytest.mark.parametrize("broken, code", [(False, 0), (True, 1)])
def test_verify_cli_exit_code(chain, super_conn, broken, code):
    """The real command-line process exits 0 or 1, for use in cron or CI."""
    if broken:
        tamper(super_conn, "UPDATE audit_events SET actor_id = 'x' WHERE seq = %s",
               (chain[0]["seq"],))
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_chain.py"), *VERIFY],
                          capture_output=True, text=True, cwd="/")
    assert proc.returncode == code, proc.stdout + proc.stderr


# --- tamper_demo.py -------------------------------------------------------------


def test_demo_needs_confirmation(chain, app_conn):
    assert tamper_demo.main(DEMO) == 2
    assert audit.verify_database(app_conn).ok


def test_demo_seeds_empty_chain_and_detects_tampering(app_conn, capsys):
    assert tamper_demo.main([*DEMO, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "adding 3 demo events" in out
    assert "app_user: refused (InsufficientPrivilege" in out
    assert "superuser: refused (RaiseException" in out
    assert "BROKEN at seq 2" in out  # the middle of the three seeded events

    result = audit.verify_database(app_conn)
    assert not result.ok and result.broken_seq == 2


def test_demo_tampers_with_chosen_seq(chain, app_conn):
    assert tamper_demo.main([*DEMO, "--yes", "--seq", str(chain[0]["seq"])]) == 0
    assert audit.verify_database(app_conn).broken_seq == chain[0]["seq"]


def test_demo_refuses_when_chain_already_broken(chain, super_conn, capsys):
    tamper(super_conn, "UPDATE audit_events SET actor_id = 'x' WHERE seq = %s",
           (chain[0]["seq"],))
    assert tamper_demo.main([*DEMO, "--yes"]) == 1
    assert "already broken" in capsys.readouterr().out
