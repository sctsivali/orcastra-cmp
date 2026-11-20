"""Join the Mini tables (009-011, first shipped in Mini 1.0.0-RC1) to the CMP 1.0.2 chain.

The Mini revisions hang off 008_image_upload_sessions like the CMP line does, so the graph has two
heads until this merge. A database stamped at either side upgrades through it.
"""

revision = "m001_mini_merge_047"
down_revision = ("047_org_cluster_granted_by", "011_audit_log_anchor")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
