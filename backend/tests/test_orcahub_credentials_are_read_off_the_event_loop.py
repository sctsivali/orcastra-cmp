"""OrcaHub callback credentials are read from Vault on a worker thread, never on the event loop.

Every instance lifecycle webhook and member push looks up the organization's callback key: two
blocking Vault reads per candidate key, each with its own timeouts and retries. On the event loop a
slow Vault stalled the whole worker, every request included. The reads now run on a thread with a
bound on the wait; a lookup that outlives it gives no callback, as a missing key does.
"""

import threading
from types import SimpleNamespace

from app.services import orcahub_sync


async def test_the_vault_reads_run_on_a_worker_thread(monkeypatch) -> None:
    seen = []

    def credentials_of(org_id, key_id):
        seen.append((threading.current_thread() is threading.main_thread(), org_id, key_id))
        return {"app_url": "https://hub.example.test", "api_key_id": key_id, "api_key_secret": "s"}

    monkeypatch.setattr(orcahub_sync, "_credentials_of", credentials_of)
    org = SimpleNamespace(id=7, source="orcahub", api_key_id="oak_primary")
    creds = await orcahub_sync._get_orcahub_credentials(org)
    assert creds["api_key_id"] == "oak_primary"
    assert seen == [(False, 7, "oak_primary")]


async def test_a_lookup_that_outlives_its_bound_gives_no_callback(monkeypatch) -> None:
    import time

    def slow(org_id, key_id):
        time.sleep(0.3)
        return {"app_url": "https://hub.example.test", "api_key_id": key_id, "api_key_secret": "s"}

    monkeypatch.setattr(orcahub_sync, "_credentials_of", slow)
    monkeypatch.setattr(orcahub_sync, "CREDENTIAL_READ_SECONDS", 0.05)
    org = SimpleNamespace(id=7, source="orcahub", api_key_id="oak_primary")
    assert await orcahub_sync._get_orcahub_credentials(org) is None
