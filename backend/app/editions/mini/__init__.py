"""Orcastra Mini: one host, TLS client-certificate sign-in, a hash-chained audit log in Postgres.

Selected with ORCASTRA_EDITION=mini. Everything Mini changes about CMP is registered here through
the seams in app/editions/__init__.py; nothing else in the tree knows which edition runs.
"""

from app.editions import Edition
from app.editions.mini import db_reconcile

EDITION = Edition(
    name="mini",
    db_reconcilers=(db_reconcile.reconcile,),
)
