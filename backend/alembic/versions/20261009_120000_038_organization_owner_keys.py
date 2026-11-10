"""Merge 015a_organization_owner_keys into this chain.

Revision ID: 038_organization_owner_keys
Revises: 037_migration_unknown_phase, 015a_organization_owner_keys

The owner-set schema lives in 015a alone, which branches from 015 because a maintenance release
line runs it there. This revision only joins the branches:

- a fresh install runs 015a after 015, then 016 to 037, then this merge (the branch listed last in
  down_revision runs first; see 009_merge_org_heads);
- a database already at 015a runs 016 to 037, then this merge;
- a database at 037 runs 015a, which backfills one owner row per claimed organization, then this
  merge. None of 016 to 037 touch `organizations` or its key column, so the order does not matter.

A database that applied 038 when it carried the same DDL itself needs nothing: alembic counts 015a
as applied below it.

important: never give this revision DDL again, and never edit 015a: deployed databases ran it as
written. A change to the owner set belongs in a new revision after this one.
"""


revision = "038_organization_owner_keys"
down_revision = ("037_migration_unknown_phase", "015a_organization_owner_keys")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
