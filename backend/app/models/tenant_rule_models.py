"""A tenant access rule seen naming a project or instance that is not there (services/tenant_rule_audit.py)."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.core.database import Base


class TenantRuleObservation(Base):
    """One rule the audit read as missing on its cluster, kept until it is present again or removed.

    `reported_at` is set once the rule was missing on enough complete reads to be shown to a partner
    in Tenant Access; nothing is ever removed automatically.
    """

    __tablename__ = "tenant_rule_observations"

    rule_id = Column(Integer, ForeignKey("tenant_policy_rules.id", ondelete="CASCADE"), primary_key=True)
    cluster_id = Column(String(255), nullable=False, index=True)
    project_name = Column(String(255), nullable=False)
    instance_name = Column(String(255), nullable=False)
    first_missing_at = Column(DateTime(timezone=True), nullable=False)
    last_missing_at = Column(DateTime(timezone=True), nullable=False)
    misses = Column(Integer, nullable=False, default=1)
    reported_at = Column(DateTime(timezone=True), nullable=True)
