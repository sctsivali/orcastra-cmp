
import sys
import os
import time
from collections import defaultdict
from unittest.mock import MagicMock, patch

# Add backend directory to path so we can import app
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

with patch('app.core.config.get_settings') as mock_get_settings:
    settings_mock = MagicMock()
    settings_mock.rate_limit_enabled = True
    settings_mock.rate_limit_requests = 100
    settings_mock.api_prefix = "/api/v1"
    settings_mock.get_cors_origins.return_value = ["*"]
    settings_mock.app_version = "test"
    settings_mock.api_title = "Test API"
    settings_mock.debug = True

    mock_get_settings.return_value = settings_mock

    with patch('app.services.lxd_service.get_lxd_service'):
        # Now import the app and the new function
        from app.main import rate_limit_storage, cleanup_rate_limit_storage

def test_memory_leak_fix():
    """
    Test that cleanup_rate_limit_storage removes expired keys.
    """
    print(f"Initial storage size: {len(rate_limit_storage)}")

    # Clear storage
    rate_limit_storage.clear()

    # Simulate 100 different IPs making requests
    print("Simulating requests from 100 IPs...")
    current_time = time.time()

    for i in range(100):
        client_ip = f"192.168.1.{i}"
        # All requests are at current_time
        rate_limit_storage[client_ip].append(current_time)

    print(f"Storage size after 100 IPs: {len(rate_limit_storage)}")
    assert len(rate_limit_storage) == 100

    # Run cleanup immediately (should not remove anything as they are fresh)
    removed = cleanup_rate_limit_storage(current_time)
    print(f"Removed {removed} keys immediately (should be 0).")
    assert removed == 0
    assert len(rate_limit_storage) == 100

    # Simulate passage of time (61 seconds)
    print("Simulating passage of time (61 seconds)...")
    future_time = current_time + 61

    # Run cleanup
    print("Running cleanup...")
    removed = cleanup_rate_limit_storage(future_time)
    print(f"Removed {removed} keys.")

    # All 100 IPs should be removed because their newest request is > 60s old
    assert removed == 100
    assert len(rate_limit_storage) == 0
    print(f"Storage size after cleanup: {len(rate_limit_storage)}")

    # Test Mixed Case: Some old, some new
    print("\nTesting Mixed Case...")

    # Add 50 old IPs
    for i in range(50):
        rate_limit_storage[f"old_ip_{i}"].append(current_time)

    # Add 50 new IPs
    for i in range(50):
        rate_limit_storage[f"new_ip_{i}"].append(future_time)

    print(f"Storage size (50 old, 50 new): {len(rate_limit_storage)}")
    assert len(rate_limit_storage) == 100

    # Run cleanup at future_time + 1 (old are > 60s, new are 1s old)
    removed = cleanup_rate_limit_storage(future_time + 1)
    print(f"Removed {removed} keys (should be 50).")

    assert removed == 50
    assert len(rate_limit_storage) == 50

    # Verify remaining keys are the new ones
    for i in range(50):
        assert f"new_ip_{i}" in rate_limit_storage
        assert f"old_ip_{i}" not in rate_limit_storage

    print("Mixed case passed.")
    print("Test passed: Memory leak fix verified.")

if __name__ == "__main__":
    try:
        test_memory_leak_fix()
    except AssertionError as e:
        print(f"Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)
