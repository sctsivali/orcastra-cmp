"""Unit tests for the LXD lifecycle error -> HTTP mapping (app.routers._lxd_action_errors).

Verifies timeouts/connection failures map to 504/503 and that raw driver/urllib3
strings never leak into the client-facing detail (regression guard for the
"HTTPSConnectionPool(...): Read timed out" 500 reported from the Stop button).
"""

from app.routers._lxd_action_errors import lxd_action_http_error
from app.services.lxd_service import (
    LXDConnectionError,
    LXDOperationError,
    LXDTimeoutError,
)

_RAW = "HTTPSConnectionPool(host='100.66.12.102', port=8443): Read timed out."


def test_timeout_maps_to_504_without_leaking_raw():
    exc = LXDTimeoutError(_RAW)
    http = lxd_action_http_error(exc, "vm.stop", "vm-x", "nuc-2", target_type="vm")
    assert http.status_code == 504
    assert "HTTPSConnectionPool" not in http.detail
    assert "nuc-2" in http.detail


def test_connection_error_maps_to_503():
    http = lxd_action_http_error(LXDConnectionError("connection refused"), "vm.stop", "vm-x", "nuc-2")
    assert http.status_code == 503
    assert "nuc-2" in http.detail
    assert "connection refused" not in http.detail


def test_not_found_maps_to_404():
    http = lxd_action_http_error(LXDOperationError("VM vm-x not found on nuc-2"), "vm.stop", "vm-x", "nuc-2")
    assert http.status_code == 404


def test_operation_error_maps_to_400():
    http = lxd_action_http_error(LXDOperationError("is running, stop it first"), "vm.delete", "vm-x", "nuc-2")
    assert http.status_code == 400


def test_unknown_exception_maps_to_clean_500():
    http = lxd_action_http_error(RuntimeError(_RAW), "instance.stop", "vm-x", "nuc-2")
    assert http.status_code == 500
    assert "HTTPSConnectionPool" not in http.detail  # raw never leaked
    assert "vm-x" in http.detail


if __name__ == "__main__":
    test_timeout_maps_to_504_without_leaking_raw()
    test_connection_error_maps_to_503()
    test_not_found_maps_to_404()
    test_operation_error_maps_to_400()
    test_unknown_exception_maps_to_clean_500()
    print("lxd_action_errors tests passed")
