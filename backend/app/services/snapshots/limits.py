"""The bounds a schedule is held to, in one leaf module.

Here rather than on the ORM model because the request schemas need them too, and importing the
model for two integers drags the database engine into a module whose only job is the wire shape.
Here rather than in services/lxd/snapshots.py because that package is what models/schemas.py
deliberately does not import.

Nothing in this file imports anything.
"""

# Strictly below MAX_OPS_SNAPSHOTS_PER_INSTANCE, which is 5. The two remaining slots are the
# operator's own headroom, reserved by arithmetic rather than by policy: a schedule whose ceiling is
# the instance's ceiling starves the manual snapshot an operator takes before maintenance, which is
# the thing the whole surface exists for. A test asserts the relationship rather than the number.
MAX_SCHEDULE_KEEP_LAST = 3

# 63 minus "ops-", minus a 16-character stamp, minus the joining dash. Two labels sharing their
# first 42 characters would render to one name, so the bound is on the label rather than on the
# result, where it would be discovered as a collision instead of refused as an input.
MAX_SCHEDULE_LABEL = 42

# Mirrors MAX_EXPIRY_DAYS / DEFAULT_EXPIRY_DAYS in services/lxd/snapshots.py, which remains the
# source of truth for the snapshot itself. A test asserts the two agree.
MAX_SCHEDULE_EXPIRY_DAYS = 7
DEFAULT_SCHEDULE_EXPIRY_DAYS = 7

SCHEDULE_FREQUENCIES = ("every_6_hours", "every_12_hours", "daily", "weekly")
SCHEDULE_STATES = ("active", "paused")
SCHEDULE_RUN_STATES = ("idle", "inflight")
