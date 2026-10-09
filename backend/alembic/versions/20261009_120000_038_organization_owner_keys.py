"""Merge the RC4 hotfix line's owner-set revision into this chain.

Revision ID: 038_organization_owner_keys
Revises: 037_migration_unknown_phase, 015a_organization_owner_keys

This revision used to create `organization_owner_keys` and the deferred primary-in-set foreign key
itself. The 1.0.0-RC4 hotfix line shipped the same schema as `015a_organization_owner_keys`, branched
from 015, and production databases on that line have already run it. Had this revision kept the DDL,
upgrading one of them would create the table a second time and fail. So the schema lives in 015a
alone, carried here exactly as the hotfix line shipped it, and this revision only joins the branches.

How each database reaches this point:

- a fresh install runs 015a after 015, then 016 to 037, then this merge (the branch listed last in
  down_revision runs first; see 009_merge_org_heads);
- a database from the hotfix line, already at 015a, runs 016 to 037, then this merge;
- a database at 037 from this line runs 015a, which backfills one owner row per claimed organization,
  then this merge.

None of 016 to 037 touch `organizations` or its key column, so 015a gives the same result on either
side of them.

The id is kept so that a database that ran this revision while it still carried the DDL needs nothing:
alembic counts 015a as applied below it, and the schema it holds is the one 015a creates.

important: never give this revision DDL again, and never edit 015a. Production ran 015a as shipped; a
change to the owner set belongs in a new revision after this one.
"""


revision = "038_organization_owner_keys"
down_revision = ("037_migration_unknown_phase", "015a_organization_owner_keys")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
