"""The instance screens send the project of the row they act on or read.

The backend now resolves an omitted project by searching only the projects the caller may
see, so an old client keeps working. It still has to guess when two visible projects hold the
same instance name, and the metrics read honours the project it is sent, so a screen that sent
"default" for an instance in another project would read nothing. These checks pin the calls
that carry the row's project instead.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"


def _read(*parts: str) -> str:
    return (FRONTEND.joinpath(*parts)).read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    start = source.index(f"export function {name}(")
    following = source.find("\nexport function ", start + 1)
    return source[start : following if following != -1 else len(source)]


def _object_member(source: str, owner: str, member: str) -> str:
    """The text of `owner.member`, from its key to the next key at the same indent."""
    start = source.index(f"export const {owner} = {{")
    key = source.index(f"\n  {member}: async", start)
    following = re.search(r"\n  [a-zA-Z]+: async", source[key + 1 :])
    end = key + 1 + following.start() if following else len(source)
    return source[key:end]


@pytest.mark.parametrize(
    "hook,call",
    [
        ("useStartInstance", "api.vms.start(name, { host, project })"),
        ("useStopInstance", "api.vms.stop(name, { host, project,"),
        ("useRestartInstance", "api.vms.restart(name, { host, project,"),
    ],
)
def test_the_power_hooks_send_the_project(hook: str, call: str) -> None:
    body = _function_body(_read("hooks", "use-lxd.ts"), hook)
    assert call in body, f"{hook} drops the project, so the backend has to search for the instance"


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_the_instances_page_passes_the_row_project(action: str) -> None:
    source = _read("app", "inventory", "instances", "page.tsx")
    assert (
        f'handleAction(instance.name, instance.host, instance.project || "default", "{action}")'
        in source
    )
    assert "confirmAction.project" in source, "the stop and restart confirm dialog loses the project"


def test_the_confirm_state_carries_the_project() -> None:
    source = _read("app", "inventory", "instances", "components", "instance-action-dialogs.tsx")
    state = source[source.index("export interface ConfirmActionState") :]
    state = state[: state.index("}")]
    assert "project: string" in state


@pytest.mark.parametrize("owner", ["vmAPI", "instanceAPI"])
def test_the_single_read_can_name_the_project(owner: str) -> None:
    body = _object_member(_read("lib", "api.ts"), owner, "get")
    assert "project?: string" in body
    assert 'params.append("project", project)' in body
    assert "?${params.toString()}`" in body, "the params carrying the project are never sent"


def test_the_detail_dialog_reads_the_row_project() -> None:
    source = _read("components", "instance-detail", "instance-detail-dialog.tsx")
    assert "const instanceProject = instance?.project || project" in source
    read = source[source.index("useInstance(") : source.index("useInstanceMetrics(")]
    assert "instanceProject" in read
    metrics = source[source.index("useInstanceMetrics(") :]
    metrics = metrics[: metrics.index(");")]
    assert "instanceProject" in metrics


def test_the_single_read_hook_forwards_the_project() -> None:
    body = _function_body(_read("hooks", "use-lxd.ts"), "useInstance")
    assert "api.instances.get(name, host, project)" in body


def test_the_dashboard_detail_dialog_is_not_pinned_to_default() -> None:
    source = _read("components", "dashboard.tsx")
    dialog = source[source.index("<InstanceDetailDialog") :]
    dialog = dialog[: dialog.index("/>")]
    assert 'project="default"' not in dialog
    assert 'project={vm.project || "default"}' in dialog


def test_the_event_stream_refetch_names_the_project() -> None:
    source = _read("hooks", "use-instance-stream.ts")
    assert "api.instances.get(name, host, project)" in source
    assert "fetchAndUpsert(host, name, msg.project)" in source
    assert "fetchAndUpsert(host, p.name, msg.project || p.project)" in source


POWER_FETCH = re.compile(r"/instances/\$\{encodeURIComponent\(instanceName\)\}/\$\{operation\}`")


def _power_fetch_bodies() -> list:
    """Every direct power POST in the frontend, with the JSON body it sends."""
    found = []
    for path in sorted(FRONTEND.rglob("*.tsx")):
        source = path.read_text(encoding="utf-8")
        for match in POWER_FETCH.finditer(source):
            body_at = source.index("body: JSON.stringify(", match.end())
            body = source[body_at : source.index(")", body_at)]
            found.append((path.relative_to(FRONTEND).as_posix(), body))
    return found


def test_the_scan_finds_the_console_and_terminal_power_buttons() -> None:
    files = {name for name, _ in _power_fetch_bodies()}
    assert {
        "components/console/console-toolbar.tsx",
        "app/console/[id]/page.tsx",
        "app/terminal/[id]/page.tsx",
        "components/terminal/instance-operations.tsx",
    } <= files


def test_every_direct_power_request_sends_the_project() -> None:
    missing = [name for name, body in _power_fetch_bodies() if not re.search(r"\bproject\b", body)]
    assert missing == [], "a power button that omits the project can act on a same-named instance elsewhere"


def test_the_terminal_window_hands_its_project_to_the_power_buttons() -> None:
    source = _read("components", "terminal", "terminal-window.tsx")
    block = source[source.index("<InstanceOperations") : source.index("/>", source.index("<InstanceOperations"))]
    assert "project={terminal.project}" in block
