"""An instance created from this console must not arrive already owned by another product.

`snapshots.schedule` is the single key `_policy_from_config` derives `externally_managed` from
(services/lxd/snapshots.py). An instance carrying it is treated everywhere as belonging to the
external metered snapshot product: the operator's own create, restore, delete and copy on it are
refused behind a billing acknowledgement, each override writing a high severity audit row.

`POST /instances` and `POST /vms` therefore refuse the snapshot policy keys in `config`. The
refusal is on the shared parent model, so one rule covers both routes. The external product is
unaffected: it authenticates with an API key, which neither create route accepts, and writes these
keys through the proxy, which constructs neither model.
"""

import pathlib

import pytest

from app.core.lxd_names import POLICY_CONFIG_KEYS
from app.models.schemas import InstanceCreateRequest, VMCreateRequest
from app.services.lxd import snapshots as snapshots_service

BASE = {"name": "ct-probe", "host": "nuc2", "image": "alpine/3.19", "project": "default"}

_REPO = pathlib.Path(__file__).resolve().parents[2]


def test_the_reader_and_the_refusal_share_one_definition():
    """One definition, so a key cannot be added to one list and not the other."""
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
    """The control: without it this file would also pass against a create path that rejects
    every config key."""
    assert model(**BASE, config={"limits.cpu": "1"}).config == {"limits.cpu": "1"}


def test_all_four_keys_named_at_once_are_all_reported():
    with pytest.raises(ValueError) as caught:
        InstanceCreateRequest(**BASE, config={k: "x" for k in POLICY_CONFIG_KEYS})
    message = str(caught.value)
    for key in POLICY_CONFIG_KEYS:
        assert key in message


def test_the_subclass_validator_still_fires():
    """The refusal lives on the parent and the subclass has a validator of its own; a same-named
    one would silently replace it, leaving blank-VM creation unvalidated."""
    with pytest.raises(ValueError) as caught:
        InstanceCreateRequest(
            name="vm-probe", host="nuc2", source_type="none", instance_type="container"
        )
    assert "only supported for virtual-machine" in str(caught.value)


@pytest.mark.parametrize("model", [InstanceCreateRequest, VMCreateRequest])
@pytest.mark.parametrize("key", POLICY_CONFIG_KEYS)
def test_both_create_routes_refuse_the_key_before_any_handler_runs(model, key):
    # FastAPI validates the route's request model before invoking its handler.
    with pytest.raises(ValueError) as caught:
        model(**BASE, config={key: "@daily"})
    assert key in str(caught.value)


@pytest.mark.parametrize("model", [InstanceCreateRequest, VMCreateRequest])
def test_neither_route_refuses_an_ordinary_key(model):
    """The refusal is specific to these four keys rather than a blanket rejection of `config`.
    There is no cluster here, so the create itself is not exercised."""
    assert model(**BASE, config={"limits.cpu": "1"}).config == {"limits.cpu": "1"}


def test_the_create_form_no_longer_offers_the_keys():
    """The create form must not offer a field the backend refuses."""
    form = (_REPO / "frontend/src/components/instance-create-form.tsx").read_text()
    for key in POLICY_CONFIG_KEYS:
        assert key not in form, f"the create form still writes {key}"
    for draft_field in ("snapPattern", "snapExpiry", "snapStopped", "snapSchedule"):
        assert draft_field not in form, f"{draft_field} survived as dead draft state"
