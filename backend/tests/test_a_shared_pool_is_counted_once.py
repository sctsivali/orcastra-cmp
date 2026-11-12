"""A clustered host's storage total must not count a shared backing store per member.

/1.0/resources sends no `storage.pools` key, and its `storage.disks` block is physical media:
raw device capacity double counts a mirror and counts disks belonging to no pool. Pool space
exists only per pool per member, and adding those up is wrong the other way: a 400 GB Ceph pool
seen from three members is 1.2 TB of capacity that does not exist.
"""

import unittest

from app.services.lxd.storage_model import (
    PoolReading,
    classify_driver,
    is_shared_driver,
    summarise_cluster_storage,
)


GB = 10 ** 9

# A three-member cluster. `remote` is the same Ceph store on two members.
FIXTURE = [
    PoolReading("microcloud20", "local", "zfs", 200 * GB, 50 * GB),
    PoolReading("microcloud20", "remote", "ceph", 400 * GB, 100 * GB),
    PoolReading("microcloud30", "local", "zfs", 300 * GB, 30 * GB),
    PoolReading("microcloud40", "local", "zfs", 100 * GB, 10 * GB),
    PoolReading("microcloud40", "remote", "ceph", 400 * GB, 120 * GB),
    PoolReading("microcloud40", "scratch", "dir", 50 * GB, 5 * GB),
]


class DriverClassificationTests(unittest.TestCase):
    def test_the_network_drivers_are_shared(self):
        for driver in ("ceph", "cephfs", "lvmcluster", "powerflex", "pure"):
            self.assertTrue(is_shared_driver(driver), driver)
            self.assertEqual("shared", classify_driver(driver))

    def test_the_newer_remote_drivers_are_shared(self):
        """Remote storage per the hypervisors' own storage docs: LXD lists powerstore and
        alletra beside ceph and pure, and Incus lists linstor and truenas, whose volumes are
        available from any cluster member."""
        for driver in ("powerstore", "alletra", "linstor", "truenas"):
            self.assertTrue(is_shared_driver(driver), driver)
            self.assertEqual("shared", classify_driver(driver), driver)

    def test_a_remote_pool_seen_from_three_members_is_counted_once(self):
        for driver in ("powerstore", "alletra", "linstor", "truenas"):
            summary = summarise_cluster_storage([
                PoolReading(member, "remote", driver, 400 * GB, 100 * GB)
                for member in ("m1", "m2", "m3")
            ])
            self.assertEqual(400 * GB, summary.total, driver)
            self.assertEqual(100 * GB, summary.used, driver)
            self.assertEqual(1200 * GB, summary.raw_total, driver)
            self.assertEqual([], summary.unknown_drivers, driver)

    def test_the_per_member_drivers_are_local(self):
        for driver in ("zfs", "btrfs", "lvm", "dir"):
            self.assertFalse(is_shared_driver(driver), driver)
            self.assertEqual("local", classify_driver(driver))

    def test_lvm_and_lvmcluster_are_not_the_same_answer(self):
        """The family name is not the signal. Plain LVM is one member's disks; the cluster
        variant sits on shared block storage."""
        self.assertEqual("local", classify_driver("lvm"))
        self.assertEqual("shared", classify_driver("lvmcluster"))

    def test_an_unknown_driver_counts_as_local_and_is_named(self):
        """An unknown driver is summed per member, which overstates a pool that is in fact
        shared, so it is named and the gap stays visible."""
        self.assertEqual("unknown", classify_driver("some-future-driver"))
        summary = summarise_cluster_storage([
            PoolReading("m1", "p", "some-future-driver", 10, 1),
            PoolReading("m2", "p", "some-future-driver", 10, 1),
        ])
        self.assertEqual(20, summary.total)
        self.assertEqual(["some-future-driver"], summary.unknown_drivers)

    def test_casing_and_whitespace_do_not_change_the_answer(self):
        self.assertTrue(is_shared_driver(" Ceph "))
        self.assertEqual("local", classify_driver("ZFS"))
        self.assertEqual("unknown", classify_driver(None))


class ClusterStorageTests(unittest.TestCase):
    def setUp(self):
        self.summary = summarise_cluster_storage(FIXTURE)

    def test_the_shared_pool_is_counted_once(self):
        self.assertEqual(1050 * GB, self.summary.total)

    def test_the_naive_sum_is_still_reported(self):
        """The naive sum stays beside the total, as gpu_cards_total sits beside gpu_total."""
        self.assertEqual(1450 * GB, self.summary.raw_total)

    def test_the_difference_is_exactly_the_duplicate(self):
        self.assertEqual(400 * GB, self.summary.raw_total - self.summary.total)

    def test_used_follows_the_same_rule(self):
        # local 50 + 30 + 10 + scratch 5 = 95, shared ceph counted once at its largest, 120
        self.assertEqual(215 * GB, self.summary.used)
        self.assertEqual(315 * GB, self.summary.raw_used)

    def test_available_is_derived_not_summed(self):
        self.assertEqual(self.summary.total - self.summary.used, self.summary.available)

    def test_a_shared_pool_keeps_one_row_naming_its_members(self):
        remote = next(p for p in self.summary.pools if p["name"] == "remote")
        self.assertEqual("shared", remote["scope"])
        self.assertEqual(["microcloud20", "microcloud40"], remote["members"])
        self.assertEqual(400 * GB, remote["space_total"])

    def test_a_local_pool_is_summed_and_names_every_member(self):
        local = next(p for p in self.summary.pools if p["name"] == "local")
        self.assertEqual("local", local["scope"])
        self.assertEqual(["microcloud20", "microcloud30", "microcloud40"], local["members"])
        self.assertEqual(600 * GB, local["space_total"])

    def test_a_pool_is_never_hidden_to_tidy_a_number(self):
        """A shared pool is marked, never dropped."""
        self.assertEqual({"local", "remote", "scratch"}, {p["name"] for p in self.summary.pools})

    def test_the_rows_add_up_to_the_totals(self):
        self.assertEqual(self.summary.total, sum(p["space_total"] for p in self.summary.pools))
        self.assertEqual(self.summary.used, sum(p["space_used"] for p in self.summary.pools))

    def test_a_healthy_cluster_reports_no_unknown_drivers(self):
        self.assertEqual([], self.summary.unknown_drivers)

    def test_a_member_mid_resync_understates_itself_not_the_cluster(self):
        """Two members disagree about a shared pool's size. The larger reading wins, so a
        member still catching up does not shrink the cluster's reported capacity."""
        summary = summarise_cluster_storage([
            PoolReading("m1", "remote", "ceph", 400 * GB, 100 * GB),
            PoolReading("m2", "remote", "ceph", 120 * GB, 10 * GB),
        ])
        self.assertEqual(400 * GB, summary.total)

    def test_a_standalone_host_is_unchanged(self):
        """One member, no sharing: usable and raw are the same number."""
        one = [
            PoolReading("solo", "default", "dir", 123887923200, 97336033280),
        ]
        summary = summarise_cluster_storage(one)
        self.assertEqual(summary.raw_total, summary.total)
        self.assertEqual(summary.raw_used, summary.used)
        self.assertEqual(26551889920, summary.available)

    def test_nothing_read_is_zero_not_an_error(self):
        summary = summarise_cluster_storage([])
        self.assertEqual(0, summary.total)
        self.assertEqual([], summary.pools)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
