"""The MCP instance tools say that cloud-init user data or vendor data costs instance.exec.

A key without it is refused (tests/test_cloud_init_data_costs_exec.py), so an agent reading the tool
list learns the price before it pays for a 403. An update names the price for a value it sets or
changes, since a value the instance already holds passes (core/cloud_init_config.py).
"""

from typing import Any, Dict

import pytest

from app.mcp import tools_write


class _Descriptions:
    """Stands in for the MCP server and keeps each tool's description as registered."""

    def __init__(self) -> None:
        self.described: Dict[str, str] = {}

    def tool(self, name: str, description: str = "", **_: Any) -> Any:
        self.described[name] = description
        return lambda function: function


@pytest.mark.parametrize("tool", ["lxd_create_instance", "lxd_update_instance"])
def test_the_instance_tools_name_the_cloud_init_price(tool: str) -> None:
    server = _Descriptions()
    tools_write.register(server)

    description = server.described[tool]
    assert "cloud-init user data or vendor data" in description.lower()
    assert "needs the instance.exec capability" in description


def test_the_update_tool_prices_only_a_value_it_sets_or_changes() -> None:
    server = _Descriptions()
    tools_write.register(server)

    assert "Setting or changing cloud-init" in server.described["lxd_update_instance"]
