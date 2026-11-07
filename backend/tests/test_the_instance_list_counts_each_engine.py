"""GET /vms reports how many answering clusters are LXD, MicroCloud and Incus.

The old count split connected clusters into "config nodes" (assumed LXD) and the rest, and only
read a type for the rest. Every connected cluster is a registered one, so the rest was always
empty and incus_count and microcloud_count were 0 on every fleet. It also read a hard-coded
`secret/data/clusters` path instead of the configured registry.
"""

import unittest

from app.routers.vms import count_engines


class CountEnginesTests(unittest.TestCase):
    def test_each_connected_cluster_is_counted_by_its_registered_type(self):
        types = {"nuc2": "LXD", "incus-lab": "Incus", "incus-lts": "incus", "mc": "MicroCloud"}
        self.assertEqual(count_engines(list(types), types.get), (1, 1, 2))

    def test_an_undetermined_or_unreadable_type_counts_as_lxd(self):
        def read(name):
            if name == "broken":
                raise RuntimeError("vault down")
            return None

        self.assertEqual(count_engines(["pending", "broken"], read), (2, 0, 0))

    def test_no_connected_cluster_counts_nothing(self):
        self.assertEqual(count_engines([], lambda _name: "Incus"), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
