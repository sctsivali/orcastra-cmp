"""The bounds a schedule is held to, in one leaf module that imports nothing.

Not on the ORM model, because the request schemas need them too and importing the model would
pull the database engine into the wire-shape module. Not in services/lxd/snapshots.py, which
models/schemas.py does not import.
"""

# Strictly below MAX_OPS_SNAPSHOTS_PER_INSTANCE (5): the remaining slots keep room for the manual
# snapshot an operator takes before maintenance. A test pins the relationship between the two.
MAX_SCHEDULE_KEEP_LAST = 3

# The slug room in `ops-<slug>-<stamp>`: 63 minus "ops-", minus a 16-character stamp, minus the
# joining dash. Bounded on the label, so a longer one is refused instead of colliding when cut.
MAX_SCHEDULE_LABEL = 42

# Mirrors MAX_EXPIRY_DAYS / DEFAULT_EXPIRY_DAYS in services/lxd/snapshots.py, the source of truth
# for the snapshot itself. A test asserts the two agree.
MAX_SCHEDULE_EXPIRY_DAYS = 7
DEFAULT_SCHEDULE_EXPIRY_DAYS = 7

SCHEDULE_FREQUENCIES = ("every_6_hours", "every_12_hours", "daily", "weekly")
SCHEDULE_STATES = ("active", "paused")
SCHEDULE_RUN_STATES = ("idle", "inflight")
