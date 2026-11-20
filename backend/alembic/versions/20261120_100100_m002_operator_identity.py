"""Operator identities: a stable UUID, a unique email, several certificates, a token floor.

Mini 1.0.0-RC1 keyed an identity on one certificate fingerprint and signed its sessions with the
display name as the email claim, which is not unique. This revision:

- gives every identity a UUID (what sessions and Migration Center pin) and a unique email
  (what organization membership and every email-keyed table know the operator by);
- moves each identity's certificate into local_identity_certificates, so a renewal or a second
  device adds a row instead of replacing who the operator is;
- adds tokens_valid_after (a revoke, role change or delete refuses every older session token),
  owner_org_id and issued_by (a Partner manages the Tenants it issued), last_login_at;
- gives audit rows a chain_version and the anchor a retention cut (pruned_through_id/hash);
- converges the two shapes an RC1 database can have (built by migrations, or by the RC1 image's
  create_all, which had a unique index instead of a constraint and no bootstrap guard).

Emails for existing identities: the address in the optional mapping file (MINI_EMAIL_MAP_FILE,
default /app/config/mini-email-map.json, {"<fingerprint>": "<email>"}) written by the installer's
upgrade preflight; else the display name when it is a valid address no other identity uses; else
<common-name-label>@users.orcastra.internal. Rows that granted access to the old session email
(the display name) are moved to the new email, but only when exactly one identity had that display
name; an ambiguous one is left as it was and reported in the log. Audit rows are never rewritten:
they are part of the hash chain.

Downgrade restores the first release's columns and single-certificate shape; it does not move
rewritten membership rows back (restore the pre-upgrade backup for that).
"""

import json
import logging
import os
import re
import uuid as uuid_module

import sqlalchemy as sa
from alembic import op

revision = "m002_operator_identity"
down_revision = "m001_mini_merge_047"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

SYNTHETIC_DOMAIN = "users.orcastra.internal"
MAP_FILE_DEFAULT = "/app/config/mini-email-map.json"

# Tables of the first release whose rows grant access to whoever holds the email in them.
ACCESS_COLUMNS = (
    ("organization_members", "user_email"),
    ("organizations", "created_by"),
    ("organization_clusters", "registered_by"),
    ("user_preferences", "user_id"),
    ("user_preferences", "user_email"),
    ("user_tag_visibility", "user_id"),
    ("tags", "created_by"),
)


def _valid_email(value):
    if not value:
        return None
    try:
        from email_validator import EmailNotValidError, validate_email
    except ImportError:  # pragma: no cover - the backend image always ships it
        return value.strip().lower() if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()) else None
    try:
        return validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def _label(common_name):
    label = (common_name or "").split(".", 1)[0].strip().lower()
    label = re.sub(r"[^a-z0-9._-]+", "-", label).strip("-.") or "operator"
    return label[:64]


def _load_map():
    path = os.environ.get("MINI_EMAIL_MAP_FILE", MAP_FILE_DEFAULT)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise RuntimeError(f"{path} must be a JSON object of fingerprint -> email")
    mapped = {}
    for fingerprint, email in raw.items():
        valid = _valid_email(email)
        if not valid:
            raise RuntimeError(f"{path}: {email!r} is not a valid email address")
        mapped[str(fingerprint).lower()] = valid
    return mapped


def _columns(bind, table):
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_table(bind, table):
    return sa.inspect(bind).has_table(table)


def plan_emails(rows, mapped):
    """[(row, email)] for identity rows (dicts with id, fingerprint, common_name, display_name).

    Pure, so the rules are tested without a database: the mapping file first, then a display
    name that is a valid address no other identity shares, else a synthetic address; never two
    identities on one address.
    """
    display_counts = {}
    for row in rows:
        key = (row["display_name"] or "").strip()
        display_counts[key] = display_counts.get(key, 0) + 1

    taken = set()
    assignments = []
    for row in rows:
        email = mapped.get((row["fingerprint"] or "").lower())
        if email is None:
            candidate = _valid_email(row["display_name"])
            if candidate and display_counts[(row["display_name"] or "").strip()] == 1 and candidate not in taken:
                email = candidate
        if email is None or email in taken:
            base = _label(row["common_name"])
            email, n = f"{base}@{SYNTHETIC_DOMAIN}", 2
            while email in taken:
                email, n = f"{base}-{n}@{SYNTHETIC_DOMAIN}", n + 1
        taken.add(email)
        assignments.append((row, email))
    return assignments, display_counts


def _assign_emails(bind):
    rows = bind.execute(sa.text(
        "SELECT id, fingerprint, common_name, display_name FROM local_identities ORDER BY id"
    )).mappings().all()
    assignments, display_counts = plan_emails(rows, _load_map())
    for row, email in assignments:
        bind.execute(
            sa.text("UPDATE local_identities SET email = :email, uuid = :uuid WHERE id = :id"),
            {"email": email, "uuid": str(uuid_module.uuid4()), "id": row["id"]},
        )
    return assignments, display_counts


def _move_access_rows(bind, assignments, display_counts):
    for row, email in assignments:
        old = (row["display_name"] or "").strip()
        if not old or old.lower() == email:
            continue
        if display_counts.get(old, 0) != 1:
            log.warning(
                "operator identity %s: display name %r is shared by %d identities; rows keyed on it "
                "were not moved to %s and grant access to no one until re-added",
                row["id"], old, display_counts[old], email,
            )
            continue
        for table, column in ACCESS_COLUMNS:
            if not _has_table(bind, table) or column not in _columns(bind, table):
                continue
            bind.execute(sa.text(f'UPDATE "{table}" SET "{column}" = :new WHERE "{column}" = :old'),
                         {"new": email, "old": old})


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("local_identities", sa.Column("uuid", sa.String(36), nullable=True))
    op.add_column("local_identities", sa.Column("email", sa.String(320), nullable=True))
    op.add_column("local_identities", sa.Column(
        "owner_org_id", sa.Integer, sa.ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True))
    op.add_column("local_identities", sa.Column("issued_by", sa.String(320), nullable=True))
    op.add_column("local_identities", sa.Column("tokens_valid_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("local_identities", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "local_identity_certificates",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("identity_id", sa.Integer, sa.ForeignKey("local_identities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("common_name", sa.String(255), nullable=False),
        sa.Column("serial", sa.String(128), nullable=True),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("renewed_from_id", sa.Integer,
                  sa.ForeignKey("local_identity_certificates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("fingerprint", name="uq_local_identity_certificates_fingerprint"),
    )
    op.create_index("ix_local_identity_certificates_identity_id", "local_identity_certificates", ["identity_id"])
    op.create_index("ix_local_identity_certificates_status", "local_identity_certificates", ["status"])

    assignments, display_counts = _assign_emails(bind)
    _move_access_rows(bind, assignments, display_counts)

    # Each identity's one certificate becomes its first certificate row. "pending" from the first
    # release never issued a usable session, so it is carried as revoked.
    bind.execute(sa.text(
        "INSERT INTO local_identity_certificates (identity_id, fingerprint, common_name, serial, not_after, status, "
        "created_at, revoked_at) "
        "SELECT id, lower(fingerprint), common_name, serial, not_after, "
        "CASE WHEN status = 'active' THEN 'active' ELSE 'revoked' END, created_at, "
        "CASE WHEN status = 'active' THEN NULL ELSE now() END "
        "FROM local_identities WHERE fingerprint IS NOT NULL"
    ))
    bind.execute(sa.text("UPDATE local_identities SET status = 'revoked' WHERE status NOT IN ('active', 'revoked')"))

    op.alter_column("local_identities", "uuid", nullable=False)
    op.alter_column("local_identities", "email", nullable=False)
    op.create_unique_constraint("uq_local_identities_uuid", "local_identities", ["uuid"])
    op.create_index("uq_local_identities_email", "local_identities", [sa.text("lower(email)")], unique=True)

    # The fingerprint is a certificate's, not an identity's: drop its uniqueness in either shape
    # (constraint from the migration, unique index from create_all) and keep a plain index.
    bind.execute(sa.text("ALTER TABLE local_identities DROP CONSTRAINT IF EXISTS uq_local_identities_fingerprint"))
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_local_identities_fingerprint"))
    op.alter_column("local_identities", "fingerprint", nullable=True)
    op.create_index("ix_local_identities_fingerprint", "local_identities", ["fingerprint"])
    # The RC1 image's create_all never built the single-bootstrap-admin guard.
    bind.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_local_identities_single_bootstrap_admin ON local_identities (role) "
        "WHERE role = 'admin' AND status = 'active' AND created_by = 'bootstrap'"
    ))

    # create_all set these defaults in Python only; the migrations set them in the database.
    for table, column, default in (
        ("local_identities", "role", "'tenant'"), ("local_identities", "status", "'active'"),
        ("local_identities", "created_at", "now()"), ("local_identities", "updated_at", "now()"),
        ("audit_log", "occurred_at", "now()"), ("audit_log", "severity", "'info'"),
        ("audit_log", "result", "'success'"),
        ("audit_log_anchor", "max_id", "'0'::bigint"), ("audit_log_anchor", "row_count", "'0'::bigint"),
    ):
        bind.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {default}'))

    op.add_column("audit_log", sa.Column("chain_version", sa.SmallInteger, nullable=False, server_default="1"))
    if "event_id" in _columns(bind, "audit_log"):
        bind.execute(sa.text("ALTER TABLE audit_log ALTER COLUMN event_id TYPE varchar(64)"))
    op.add_column("audit_log_anchor", sa.Column("pruned_through_id", sa.BigInteger, nullable=False, server_default="0"))
    op.add_column("audit_log_anchor", sa.Column("pruned_head_hash", sa.String(64), nullable=True))

    for row, email in assignments:
        log.info("operator identity %s (%s) signs in as %s", row["id"], row["common_name"], email)


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_column("audit_log_anchor", "pruned_head_hash")
    op.drop_column("audit_log_anchor", "pruned_through_id")
    op.drop_column("audit_log", "chain_version")

    # Back to one certificate per identity: the newest active one, else the newest at all.
    bind.execute(sa.text(
        "UPDATE local_identities li SET fingerprint = c.fingerprint, serial = c.serial, not_after = c.not_after "
        "FROM (SELECT DISTINCT ON (identity_id) identity_id, fingerprint, serial, not_after FROM "
        "local_identity_certificates ORDER BY identity_id, (status = 'active') DESC, id DESC) c "
        "WHERE c.identity_id = li.id"
    ))
    bind.execute(sa.text("DELETE FROM local_identities WHERE fingerprint IS NULL"))
    bind.execute(sa.text("UPDATE local_identities SET status = 'revoked' WHERE status = 'deleted'"))
    op.drop_index("ix_local_identities_fingerprint", table_name="local_identities")
    op.alter_column("local_identities", "fingerprint", nullable=False)
    op.create_unique_constraint("uq_local_identities_fingerprint", "local_identities", ["fingerprint"])
    op.create_index("ix_local_identities_fingerprint", "local_identities", ["fingerprint"])

    op.drop_index("ix_local_identity_certificates_status", table_name="local_identity_certificates")
    op.drop_index("ix_local_identity_certificates_identity_id", table_name="local_identity_certificates")
    op.drop_table("local_identity_certificates")
    op.drop_index("uq_local_identities_email", table_name="local_identities")
    op.drop_constraint("uq_local_identities_uuid", "local_identities", type_="unique")
    for column in ("last_login_at", "tokens_valid_after", "issued_by", "owner_org_id", "email", "uuid"):
        op.drop_column("local_identities", column)
