"""An instance created from this console must not arrive already owned by another product.

`snapshots.schedule` is the single key `_policy_from_config` derives `externally_managed` from
(services/lxd/snapshots.py). Setting it does not merely record a preference: the instance is then
treated everywhere as belonging to the metered snapshot product sold upstream. Its detail tab says
so, and the operator's own create, restore, delete and copy on it are refused behind a billing
acknowledgement, each override writing a high severity audit row.

Until this refusal existed, `POST /instances` and `POST /vms` accepted `config` with no key filter
and the create form offered all four keys in its Advanced panel, so any Partner could put an
instance into that state by filling in a box labelled "Schedule". The refusal is on the shared
parent model, so both routes are covered by one rule.

The upstream product is unaffected by construction rather than by care: it authenticates with an
API key, API key handling exists only under routers/integrations.py, mcp/ and the upload routers,
and neither create route can be reached with one. It writes these keys through the proxy, which
constructs neither model.
"""

import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.lxd_names import POLICY_CONFIG_KEYS
from app.models.schemas import InstanceCreateRequest, VMCreateRequest
from app.services.lxd import snapshots as snapshots_service

BASE = {"name": "ct-probe", "host": "nuc2", "image": "alpine/3.19", "project": "default"}

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _client() -> TestClient:
    from app.core.dependencies import check_read_only_mode
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import instances, vms

    app = FastAPI()
    app.include_router(instances.router)
    app.include_router(vms.router)
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_the_reader_and_the_refusal_share_one_definition():
    """Two copies is how a key gets added to one list and not the other."""
    assert snapshots_service.POLICY_KEYS is POLICY_CONFIG_KEYS


@pytest.mark.parametrize("model", [VMCreateRequest, InstanceCreateRequest])
@pytest.mark.parametrize("key", POLICY_CONFIG_KEYS)
def test_every_policy_key_is_refused_on_both_create_models(model, key):
    with pytest.raises(ValueError) as caught:
        model(**BASE, config={key: "@daily"})
    # The message names the offending key, so an operator is not left guessing which box to clear.
    assert key in str(caught.value)


@pytest.mark.parametrize("model", [VMCreateRequest, InstanceCreateRequest])
def test_an_ordinary_config_key_still_passes(model):
    """The control. Without it this file would also pass against a create path that rejects
    every config key, or one that is simply broken."""
    assert model(**BASE, config={"limits.cpu": "1"}).config == {"limits.cpu": "1"}


def test_all_four_keys_named_at_once_are_all_reported():
    with pytest.raises(ValueError) as caught:
        InstanceCreateRequest(**BASE, config={k: "x" for k in POLICY_CONFIG_KEYS})
    message = str(caught.value)
    for key in POLICY_CONFIG_KEYS:
        assert key in message


def test_the_subclass_validator_still_fires():
    """Ablation. The refusal lives on the parent and the subclass has a validator of its own; a
    same-named one would have silently replaced it, leaving blank-VM creation unvalidated."""
    with pytest.raises(ValueError) as caught:
        InstanceCreateRequest(
            name="vm-probe", host="nuc2", source_type="none", instance_type="container"
        )
    assert "only supported for virtual-machine" in str(caught.value)


@pytest.mark.parametrize("path", ["/instances", "/vms"])
@pytest.mark.parametrize("key", POLICY_CONFIG_KEYS)
def test_both_create_routes_refuse_the_key_before_any_handler_runs(path, key):
    response = _client().post(path, json={**BASE, "config": {key: "@daily"}})
    assert response.status_code == 422, response.text
    assert key in response.text


@pytest.mark.parametrize("path", ["/instances", "/vms"])
def test_neither_route_refuses_an_ordinary_key(path):
    """Not an assertion that the create succeeds: there is no cluster here. Only that the refusal
    is specific to these four keys rather than a blanket rejection of `config`."""
    response = _client().post(path, json={**BASE, "config": {"limits.cpu": "1"}})
    assert response.status_code != 422, response.text


def test_the_create_form_no_longer_offers_the_keys():
    """A UI offering a field the backend must refuse is how the convention got written in the
    first place, so the form is checked rather than trusted."""
    form = (_REPO / "frontend/src/components/instance-create-form.tsx").read_text()
    for key in POLICY_CONFIG_KEYS:
        assert key not in form, f"the create form still writes {key}"
    for draft_field in ("snapPattern", "snapExpiry", "snapStopped", "snapSchedule"):
        assert draft_field not in form, f"{draft_field} survived as dead draft state"
