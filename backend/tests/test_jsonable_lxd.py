"""jsonable_lxd handles pylxd's actual shapes, whatever its class names suggest.

`pylxd.models._model.AttributeDict` is not a dict subclass despite the name. It is a plain
object whose __init__ sets every key as an attribute, so `isinstance(x, dict)` is False and its
fields live only in __dict__. A serialiser that trusted the name would return the object's repr,
`{"state": "<pylxd.models.network.NetworkState object at 0x...>"}`, which looks like success.
"""

import json
import unittest

from app.services.lxd.utils import jsonable_lxd


class _AttributeDict:
    """Mirrors pylxd.models._model.AttributeDict exactly."""

    def __init__(self, dct):
        for key, value in dct.items():
            setattr(self, key, value)

    def __iter__(self):
        return iter(self.__dict__.items())


class _Slotted:
    __slots__ = ("space", "inodes")

    def __init__(self, space, inodes):
        self.space = space
        self.inodes = inodes


class JsonableLxdTests(unittest.TestCase):
    def test_attribute_dict_becomes_a_real_dict(self) -> None:
        out = jsonable_lxd(_AttributeDict({"hwaddr": "aa:bb", "mtu": 1500}))
        self.assertEqual(out, {"hwaddr": "aa:bb", "mtu": 1500})

    def test_nested_models_are_converted_too(self) -> None:
        state = _AttributeDict(
            {
                "addresses": [_AttributeDict({"family": "inet", "address": "10.0.0.1"})],
                "counters": _AttributeDict({"bytes_received": 12}),
            }
        )
        self.assertEqual(
            jsonable_lxd(state),
            {"addresses": [{"family": "inet", "address": "10.0.0.1"}], "counters": {"bytes_received": 12}},
        )

    def test_slotted_models_are_converted(self) -> None:
        self.assertEqual(
            jsonable_lxd(_Slotted({"total": 1}, {"used": 2})),
            {"space": {"total": 1}, "inodes": {"used": 2}},
        )

    def test_the_result_is_json_serialisable(self) -> None:
        """This is what FastAPI hands to the JSON encoder."""
        state = _AttributeDict({"addresses": [_AttributeDict({"a": 1})], "mtu": 1500})
        json.dumps(jsonable_lxd(state))  # must not raise

    def test_no_repr_strings_leak_through(self) -> None:
        """No object repr appears where a dict belongs."""
        out = jsonable_lxd(_AttributeDict({"counters": _AttributeDict({"bytes_sent": 1})}))
        self.assertNotIn("object at 0x", json.dumps(out))

    def test_plain_values_pass_through_untouched(self) -> None:
        self.assertEqual(jsonable_lxd({"a": [1, "b", None, True]}), {"a": [1, "b", None, True]})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
