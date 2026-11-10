"""The shared LXD error to HTTP status mapper, at its substring boundaries.

`app/core/lxd_errors.py` matches substrings of the message because pylxd surfaces most failures
as a bare exception with prose in it. A miss costs a 400 with the real text; a match on prose
that means something else sends the operator to the wrong remedy.
"""

from app.core.lxd_errors import lxd_http_error


class TestTheInUseMatchIsNotTooLoose:
    """`used by` as a bare substring matches prose that is not a conflict.

    Starting a VM from an unsigned image answers "The image used by this instance is
    incompatible with secureboot ...". A 409 would tell the operator to wait; the remedy is a
    config change.
    """

    def test_a_genuine_in_use_refusal_is_still_409(self):
        exc = Exception("Network default is used by instance web-01")
        assert lxd_http_error(exc, "Network", "default").status_code == 409

    def test_the_bare_in_use_wording_is_still_409(self):
        exc = Exception("The storage volume is in use")
        assert lxd_http_error(exc, "Storage volume", "backup").status_code == 409

    def test_a_busy_instance_is_409(self):
        exc = Exception('Instance is busy running a "create" operation')
        assert lxd_http_error(exc, "Instance", "web-01").status_code == 409

    def test_a_secureboot_incompatibility_is_not_a_conflict(self):
        exc = Exception(
            "The image used by this instance is incompatible with secureboot. "
            "Please set security.secureboot=false on the instance"
        )
        result = lxd_http_error(exc, "Instance", "web-01")
        assert result.status_code == 400, (
            "this is a configuration problem the caller must fix, not something to retry"
        )
