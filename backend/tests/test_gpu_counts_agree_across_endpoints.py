"""Every endpoint must answer "how many GPUs" from the same code.

Four places used to count for themselves. /nodes counted compute cards, /cluster-members and
/regions counted every DRM card, and /external/regions returned a hardcoded zero. The same
Jetson therefore reported 1, 2, 2 and 0 depending on which screen an operator was looking at.
"""

import ast
import inspect
from pathlib import Path

from app.core import cache as cache_module
from app.models.schemas import MemberResources, NodeResources
from app.services.lxd import gpu as gpu_module
from app.services.lxd import lxd_service, node_resources, resources

BACKEND = Path(__file__).resolve().parents[1]
ROUTERS = BACKEND / "app" / "routers"


def _keywords_passed_to(source: str, call_name: str) -> set[str]:
    """Keyword names handed to `call_name(...)` anywhere in the source."""
    passed: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == call_name:
            passed.update(k.arg for k in node.keywords if k.arg)
    return passed


def test_no_router_counts_gpu_cards_by_hand() -> None:
    for name in ("nodes", "cluster_members", "regions", "integrations", "monitoring"):
        source = (ROUTERS / f"{name}.py").read_text()
        assert "len(gpu_cards" not in source, f"{name}.py counts cards instead of asking the inventory"
        assert "gpu_info.get(" not in source, f"{name}.py reads the raw LXD gpu block directly"


def test_gpu_logic_lives_in_one_place() -> None:
    moved = (
        "_classify_gpu_card",
        "_parse_gpu_cards",
        "_get_gpu_usage_map",
        "_count_compute_gpu_instances",
        "_COMPUTE_GPU_NODE_RE",
    )
    source = inspect.getsource(resources)
    for name in moved:
        assert name not in source, f"{name} must live in services/lxd/gpu*, not resources.py"
    assert "parse_gpu_cards" not in (ROUTERS / "cluster_members.py").read_text(), (
        "cluster_members must not reach into the parser; it takes an inventory"
    )


def test_node_resources_is_built_in_exactly_one_place() -> None:
    """Three hand-built constructions is how gpu_cards_total came to be set on only one."""
    built_in = [
        path
        for path in (BACKEND / "app").rglob("*.py")
        if "NodeResources(" in path.read_text()
        and path.name not in ("schemas.py", "node_resources.py")
    ]
    assert built_in == [], f"NodeResources built outside node_resources.py: {built_in}"


def test_the_node_resources_builder_sets_every_declared_field() -> None:
    passed = _keywords_passed_to(inspect.getsource(node_resources), "NodeResources")
    missing = set(NodeResources.model_fields) - passed
    assert not missing, f"NodeResources declares fields nothing sets: {sorted(missing)}"


def test_the_member_resources_response_sets_every_declared_field() -> None:
    source = (ROUTERS / "cluster_members.py").read_text()
    passed = _keywords_passed_to(source, "MemberResources")
    missing = set(MemberResources.model_fields) - passed - {"member_name"}
    assert not missing, f"MemberResources declares fields the handler never sets: {sorted(missing)}"


def test_external_regions_does_not_hardcode_zero_gpu() -> None:
    source = (ROUTERS / "integrations.py").read_text()
    assert '"total_gpu": 0' not in source, (
        "the partner-facing endpoint told every integration their GPUs did not exist"
    )
    assert "_integration_hardware_totals" in source


def test_gpu_utilization_is_never_fabricated() -> None:
    """LXD reports no GPU telemetry. A field named like usage would have to be invented."""
    schemas = (BACKEND / "app" / "models" / "schemas.py").read_text()
    for invented in ("gpu_usage_percent", "gpu_memory_used", "vram_used", "gpu_temperature"):
        assert invented not in schemas, f"{invented} cannot be sourced from LXD"
    assert NodeResources.model_fields["gpu_utilization_supported"].default is False


def test_the_versioned_cache_keys_move_together() -> None:
    """A cached body written before the meaning changed must not be served after deploy."""
    assert cache_module.RESOURCE_SCHEMA_VERSION
    for key in (
        cache_module.NODES_STATUS_CACHE_KEY,
        cache_module.REGIONS_OVERVIEW_CACHE_KEY,
        cache_module.MEMBER_RESOURCES_CACHE_PREFIX,
        cache_module.MICROCLOUD_RESOURCES_CACHE_PREFIX,
    ):
        assert key.endswith(cache_module.RESOURCE_SCHEMA_VERSION) or f":{cache_module.RESOURCE_SCHEMA_VERSION}" in key

    for path in (ROUTERS / "nodes.py", ROUTERS / "regions.py", ROUTERS / "cluster_members.py"):
        source = path.read_text()
        assert 'get_scope_cache_key("nodes:status:all")' not in source
        assert 'get_scope_cache_key("regions:overview")' not in source


def test_the_gpu_picker_is_guarded_by_require_client() -> None:
    """Without the guard, an unreachable cluster answered 400 "bad request"."""
    source = inspect.getsource(lxd_service.LXDService.get_node_gpus)
    assert "require_client(" in source


def test_the_picker_payload_keeps_what_the_create_form_reads() -> None:
    from app.models.schemas import GPUCardInfo, GpuCardListResponse

    for field in ("vendor", "product", "driver", "driver_version", "pci_address", "id"):
        assert field in GPUCardInfo.model_fields, f"the GPU picker reads {field}"
    for field in ("gpu_class", "is_compute", "attachable", "model_name", "in_use"):
        assert field in GPUCardInfo.model_fields, f"the picker needs {field} to rank and mark cards"
    import typing

    assert typing.get_args(GpuCardListResponse.model_fields["gpus"].annotation) == (GPUCardInfo,), (
        "one card shape on the wire: the flat GpuCardInfo forced a cast in the create form"
    )


def test_the_inventory_exposes_one_public_surface() -> None:
    for name in ("build_gpu_inventory", "classify_gpu_card", "parse_gpu_cards", "GpuInventory"):
        assert name in gpu_module.__all__, f"{name} is part of the GPU API"
