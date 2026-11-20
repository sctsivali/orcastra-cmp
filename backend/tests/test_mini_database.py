"""Mini's database path: recognising an RC1 database, and how existing operators get their email.

An RC1 database was built by create_all and never stamped. Recognising it wrongly either refuses
an upgrade that should run or stamps revisions its schema does not match, after which every later
migration fails the same way. The email rules decide which person keeps which organization
membership after the upgrade, so a wrong guess hands one operator another's access.
"""

import importlib.util
from pathlib import Path

import pytest

from app.core.db_bootstrap import _RC2_CORE_TABLES
from app.editions.mini import db_reconcile

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
RC1 = set(_RC2_CORE_TABLES) | {"image_upload_sessions", "local_identities", "audit_log", "audit_log_anchor"}


def _m002():
    path = next(VERSIONS.glob("*_m002_operator_identity.py"))
    spec = importlib.util.spec_from_file_location("m002_operator_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_unstamped_rc1_database_is_recognised():
    assert db_reconcile.recognise(RC1, stamped=False) == "rc1"


def test_a_stamped_database_is_left_to_alembic():
    assert db_reconcile.recognise(RC1, stamped=True) is None


def test_a_database_without_mini_tables_is_left_to_the_cmp_bootstrap():
    assert db_reconcile.recognise(set(_RC2_CORE_TABLES), stamped=False) is None
    assert db_reconcile.recognise(set(), stamped=False) is None


def test_mini_tables_without_the_rc1_core_are_refused_not_stamped():
    with pytest.raises(SystemExit, match="missing"):
        db_reconcile.recognise({"local_identities", "audit_log", "organizations"}, stamped=False)


@pytest.mark.parametrize("later", sorted(db_reconcile.LATER_TABLES))
def test_mini_tables_beside_a_later_release_table_are_refused(later):
    with pytest.raises(SystemExit, match="later"):
        db_reconcile.recognise(RC1 | {later}, stamped=False)


def test_the_stamp_names_revisions_that_exist():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    for revision in db_reconcile.RC1_STAMP:
        assert script.get_revision(revision) is not None


def _row(i, cn, display, fp=None):
    return {"id": i, "common_name": cn, "display_name": display, "fingerprint": fp or f"{i:064x}"}


def test_a_unique_address_display_name_becomes_the_email():
    plan, _ = _m002().plan_emails([_row(1, "alice.users.orcastra.internal", "Alice@Example.org")], {})
    assert plan[0][1] == "alice@example.org"


def test_a_display_name_that_is_not_an_address_gets_a_synthetic_one():
    plan, _ = _m002().plan_emails([_row(1, "qa-partner.users.orcastra.internal", "QA Partner")], {})
    assert plan[0][1] == "qa-partner@users.orcastra.internal"


def test_a_shared_display_name_is_given_to_neither_identity():
    plan, counts = _m002().plan_emails(
        [_row(1, "a.users.orcastra.internal", "ops@example.org"), _row(2, "b.users.orcastra.internal", "ops@example.org")],
        {},
    )
    assert [email for _, email in plan] == ["a@users.orcastra.internal", "b@users.orcastra.internal"]
    assert counts["ops@example.org"] == 2


def test_the_mapping_file_wins_and_no_two_identities_share_an_email():
    rows = [_row(1, "x.y", "x@example.org", fp="AA"), _row(2, "x.z", "Other"), _row(3, "x", "Third")]
    plan, _ = _m002().plan_emails(rows, {"aa": "boss@example.org"})
    emails = [email for _, email in plan]
    assert emails[0] == "boss@example.org"
    assert len(set(emails)) == 3
    assert emails[1:] == ["x@users.orcastra.internal", "x-2@users.orcastra.internal"]


def test_a_synthetic_address_is_valid_for_the_validator_the_api_uses():
    from email_validator import validate_email

    plan, _ = _m002().plan_emails([_row(1, "Weird Name!!.users.orcastra.internal", None)], {})
    assert validate_email(plan[0][1], check_deliverability=False).normalized == plan[0][1]


def _access_db(rows):
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE organization_members (id INTEGER PRIMARY KEY, organization_id INTEGER, user_email TEXT)"))
        conn.execute(sa.text("CREATE TABLE organizations (id INTEGER PRIMARY KEY, created_by TEXT)"))
        for org, email in rows:
            conn.execute(sa.text("INSERT INTO organization_members (organization_id, user_email) VALUES (:o, :e)"),
                         {"o": org, "e": email})
    return engine


def _members(engine):
    import sqlalchemy as sa

    with engine.connect() as conn:
        return sorted(conn.execute(sa.text("SELECT organization_id, user_email FROM organization_members")).all())


def test_a_move_never_carries_rows_another_move_just_wrote():
    # A is mapped to the address that was C's display name. Moving one identity at a time would
    # hand A's org 1 membership to C; every row must follow the key it had before the upgrade.
    m002 = _m002()
    a, c = _row(1, "a.users.orcastra.internal", "Alice"), _row(2, "c.users.orcastra.internal", "alice@example.com")
    plan, counts = m002.plan_emails([a, c], {a["fingerprint"]: "alice@example.com"})
    emails = {row["id"]: email for row, email in plan}
    assert emails[1] == "alice@example.com" and emails[2].endswith("@users.orcastra.internal")
    engine = _access_db([(1, "Alice"), (2, "alice@example.com")])
    with engine.begin() as conn:
        m002._move_access_rows(conn, plan, counts)
    assert _members(engine) == [(1, "alice@example.com"), (2, emails[2])]


def test_rows_keyed_on_a_shared_display_name_stay_where_they_are():
    m002 = _m002()
    rows = [_row(1, "t1.users.orcastra.internal", "QA Tenant"), _row(2, "t2.users.orcastra.internal", "QA Tenant")]
    plan, counts = m002.plan_emails(rows, {})
    engine = _access_db([(1, "QA Tenant")])
    with engine.begin() as conn:
        m002._move_access_rows(conn, plan, counts)
    assert _members(engine) == [(1, "QA Tenant")]
