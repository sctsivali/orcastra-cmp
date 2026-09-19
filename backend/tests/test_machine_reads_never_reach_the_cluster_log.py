"""The cluster log keeps what changed, what was denied and who signed in; not every read.

Forty minutes of the first live journal held 3,829 log rows, 3,355 of them `vault.secret.read`
(the certificate read behind every dial of a dead cluster) and 367 `integration.proxy.request`
(the hub's polling), which buried the twenty rows an operator would look for and would have
grown to millions inside one retention window. Those records stay on stdout, the audit of
record. This pins the set of suppressed reads against the catalogue and the message wording
for an action the description table does not name.
"""

from app.services.activity_logger import ACTION_CATEGORIES
from app.services.task_journal.sink_mapping import record_to_log_row
from app.services.task_journal.vocabulary import LOG_SUPPRESSED_ACTIONS, humanise_action


def _record(action, *, host="nuc1", target_type="vault", target_id="secret/x", result="success"):
    return {
        "@timestamp": "2026-09-19T05:14:31.638317+00:00",
        "event_id": "evt_1",
        "action": action,
        "actor": {"user_id": "ops@example.test", "role": "admin"},
        "target": {"type": target_type, "id": target_id, "host": host},
        "result": result,
        "details": {},
    }


def test_every_suppressed_read_is_a_catalogued_action():
    unknown = sorted(LOG_SUPPRESSED_ACTIONS - set(ACTION_CATEGORIES))
    assert unknown == [], f"suppressing an action the catalogue does not name: {unknown}"


def test_the_live_noise_makers_produce_no_log_row_whatever_their_result():
    for action in ("vault.secret.read", "integration.proxy.request", "vault.kv.list", "vault.health.check"):
        for result in ("success", "failure"):
            assert record_to_log_row(_record(action, result=result)) is None, (action, result)


def test_writes_denials_and_sign_ins_still_land():
    assert record_to_log_row(_record("vault.kv.put"))["message"] == "Vault kv put secret/x"


def test_a_vault_write_with_no_person_behind_it_is_plumbing():
    """Live: 204 `vault.kv.put` rows in two hours, every one the last-used stamp on an
    integration key's metadata, written by nobody. A person's or a key's Vault write lands."""
    record = _record("vault.kv.put")
    record["actor"] = {"user_id": None, "user_type": None, "role": None}
    assert record_to_log_row(record) is None
    keyed = _record("vault.kv.put")
    keyed["actor"] = {"user_id": None, "user_type": "api_key", "role": None}
    keyed["details"] = {"api_key_id": "oak_1"}
    row = record_to_log_row(keyed)
    assert row is not None
    denied = record_to_log_row(_record("rbac.access_denied", target_type="instance", target_id="web-01", result="denied"))
    assert denied is not None and denied["message"].endswith(": denied")
    assert record_to_log_row(_record("auth.login", host=None, target_type="user", target_id="ops@example.test")) is not None
    assert record_to_log_row(_record("lxd.connection.establish", target_type="cluster", target_id="nuc1")) is not None


def test_an_unnamed_action_reads_as_words_not_as_an_identifier():
    assert humanise_action("vault.kv.put") == "Vault kv put"
    assert humanise_action("rbac.access_denied") == "Rbac access denied"
    assert humanise_action("integration.instance.exec") == "Integration instance exec"
    row = record_to_log_row(_record("integration.instance.exec", target_type="instance", target_id="web-01"))
    assert row["message"] == "Integration instance exec web-01"
