"""Every kernel advisory entry has the shape the assessment relies on.

The data file is edited by hand when a vendor ships a fix, so a typo there must fail here
rather than quietly flip hosts to "patched" in production.
"""

from datetime import date

import pytest

from app.services.kernel_advisory_data import ADVISORIES
from app.services.kernel_cve_feed.record import CVE_ID

_TRACKS = {"generic", "aws", "azure", "gcp", "gke", "kvm", "oracle", "ibm", "nvidia", "raspi", "realtime"}


@pytest.mark.parametrize("advisory", ADVISORIES, ids=lambda a: a.id)
def test_an_advisory_entry_is_well_formed(advisory):
    # the id is also the path the background check reads from Ubuntu (services/kernel_cve_feed/)
    assert CVE_ID.match(advisory.id) and advisory.summary
    assert advisory.reference_url.startswith("https://")
    assert date(2026, 1, 1) <= advisory.data_as_of <= date.today()
    assert advisory.stale_after_days > 0
    assert advisory.scores and all(s.source for s in advisory.scores)
    assert all(s.score is None or 0 < s.score <= 10 for s in advisory.scores)
    assert advisory.introduced.key < advisory.mainline_fixed.key
    for (major, minor), sublevel in advisory.stable_fixes.items():
        assert sublevel > 0
        assert advisory.introduced.key < (major, minor, sublevel, 0)
        assert (major, minor) < advisory.mainline_fixed.branch
    for (os_version, branch, track), abi in advisory.ubuntu_fixed_abi.items():
        assert os_version.count(".") == 1 and branch.count(".") == 1
        assert track in _TRACKS and abi > 0


def test_advisory_ids_are_unique():
    ids = [advisory.id for advisory in ADVISORIES]
    assert len(ids) == len(set(ids))
