#!/usr/bin/env python3
"""Inventory QA entry points without importing the application or reading configuration."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
SOURCE_ROOTS = ("backend/app", "backend/tests", "frontend/src", "frontend/e2e", "scripts/checks")
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
SKIP_DIRS = {"node_modules", "__pycache__", ".next", ".worktrees", ".auth", "test-results"}
SCOPE_CALLS = {
    "org_scope_dependency", "set_org_scope_for_ws", "verify_cluster_access", "verify_project_access",
    "verify_tenant_permission", "check_tenant_permission", "get_current_allowed_clusters",
    "task_conditions", "log_conditions", "schedule_conditions", "task_is_visible", "log_is_visible",
    "filter_visible_lxd_references", "tenant_acl_active", "project_is_visible", "_verify_instance_access",
    "user_has_access", "get_scope_cache_key", "check_read_only_mode", "resolve_owner",
}
DECLARED_ROLE_GATES = {
    "require_admin": ["Admin"], "require_partner": ["Admin", "Partner"],
    "require_tenant": ["Admin", "Partner", "Tenant"],
    "require_authenticated": ["Authenticated actor"],
}
FEATURES = {
    "identity-and-organizations": [
        ("login-logout-actor", "Login establishes the intended actor; logout and actor changes retire its reads and sessions."),
        ("roles-policy-groups", "Role and policy changes alter server authorization; adjacent organization and project objects remain inaccessible."),
        ("preferences-branding", "Organization branding reaches its members, personal theme survives, and responses from a retired actor cannot apply."),
        ("api-keys", "Credentials enforce owner, organization, scope, expiry, revocation, and usage limits on each call."),
    ],
    "cluster-and-platform": [
        ("registration-trust", "Registration and trust bind the intended endpoint and certificate; failed or duplicate submissions leave no unintended registration."),
        ("members-server-resources", "Member, server, and resource reads reflect the target platform; pagination and incomplete sources remain truthful."),
        ("warnings-regions-technologies", "Warnings, regions, and technologies respect organization visibility and report failed cluster sources."),
        ("unreachable-recovery", "An unreachable cluster reports degraded availability and recovers without hiding successful sibling reads."),
    ],
    "compute": [
        ("create-lifecycle-specification-rebuild", "Accepted work reaches the requested instance state; durable operation records distinguish completion, failure, and unknown outcomes."),
        ("snapshots-schedules", "Snapshots exist on the requested instance; schedules execute once per claim and respect the snapshot permission."),
        ("migration-mobility", "Source and destination state, durable job state, leases, and cleanup agree after migration or failure."),
        ("console-terminal-sessions", "Only authorized actors open or resume the intended instance session; revoked access closes or prevents transport."),
        ("file-transfer", "Transfers stay within the authorized instance and permitted path; failed or duplicate writes leave the documented state."),
    ],
    "networking-and-storage": [
        ("networks", "Bridge and OVN configuration obey type, capability, project, and value constraints; completed operations are read back from the hypervisor."),
        ("acl-ipam", "ACL and IPAM updates apply only to the authorized resource and reconcile with target network state."),
        ("profiles", "Profiles resolve the authorized owner and project; dependent instances and shared profile constraints remain consistent."),
        ("pools-volumes", "Pools and volumes expose only permitted references; missing capacity is distinct from a valid zero reading."),
        ("images-iso-upload-catalogue", "Image and ISO uploads settle into verified storage; Tenant catalogue discovery excludes invisible workload volumes."),
    ],
    "observability": [
        ("monitoring", "Monitoring distinguishes unavailable measurements from zero and never renders non-finite values."),
        ("journal-queue-logs-audit", "Scoped journal counts, ranks, pagination, logs, and audit records agree with accepted and settled work."),
        ("websocket-collector-recovery", "HTTP and WebSocket scopes agree, revoked scope stops frames, and collector source loss and recovery change health truthfully."),
        ("cache", "Browser clearing preserves authentication and active mutations; shared clearing stays authorized, scoped, fenced, and truthful about partial failure."),
    ],
    "integrations-and-addons": [
        ("scopes-raw-proxy", "Integration scopes and proxy request validation constrain methods, routes, projects, and object ownership."),
        ("provisioning-sync-installation", "Provisioning, synchronization, and installation persist the intended scoped state without duplicate or orphan resources."),
        ("webhooks-revocation-usage", "Webhook delivery, retries, credential revocation, and usage budgets retain the documented isolation and outcome."),
    ],
    "operational": [
        ("startup-readiness", "Readiness reports dependency faults accurately and startup does not block unrelated healthy requests."),
        ("scratch-migrations", "A new scratch database upgrades to the expected migration head; graph and rollback implications are verified separately."),
        ("dependency-restart-recovery", "Dependency failures and process restarts recover durable work and caches without duplicates, lost scope, or false completion."),
        ("artifact-provenance-cleanup", "The running build matches the tested source; exact owned resource IDs are removed and the lab baseline is restored."),
    ],
}
FAMILY_TOKENS = {
    "identity-and-organizations": ("auth", "organization", "user_group", "user_preference", "tenant_access", "break_glass", "settings", "my_keys"),
    "cluster-and-platform": ("cluster", "nodes", "lxd_server", "lxd_warnings", "regions", "technolog", "cert_manager", "register-node"),
    "compute": ("instance", "vms", "snapshot", "migration", "console", "terminal", "session", "file_transfer", "create"),
    "networking-and-storage": ("network", "ipam", "profile", "storage", "images", "image_upload", "iso_upload", "iso-images"),
    "observability": ("monitoring", "metrics", "tasks", "task_journal", "cluster_log", "events", "audit", "cache", "archive"),
    "integrations-and-addons": ("integration", "extension", "webhook", "mcp", "desktop"),
    "operational": ("main.py", "health", "database", "alembic", "system.py", "recovery"),
}


def callable_name(node: ast.AST) -> str:
    return callable_name(node.func) if isinstance(node, ast.Call) else ast.unparse(node)


def leaf_name(node: ast.AST) -> str:
    return callable_name(node).rsplit(".", 1)[-1]


def keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def source_ref(path: str, node: ast.AST) -> str:
    return f"{path}:{getattr(node, 'lineno', 1)}"


def evidence() -> dict:
    return {"status": "unverified", "role": None, "verified_scope": None, "build": None,
            "fixture": None, "commands": [], "assertions": [], "result": None,
            "sanitized_artifact_links": [], "source_commit": None, "source_tree_digest": None,
            "verification_kind": None}


def families(text: str) -> list[str]:
    return [family for family, tokens in FAMILY_TOKENS.items() if any(token in text.lower() for token in tokens)]


def new_surface(kind: str, identity: str, file_path: str, node: ast.AST, **fields: object) -> dict:
    return {"id": f"{kind}:{hashlib.sha256(identity.encode()).hexdigest()[:20]}", "kind": kind,
            "source": source_ref(file_path, node), "feature_family_candidates": families(file_path + " " + identity),
            "effective_role_and_scope": "unverified", "evidence": evidence(), **fields}


def source_files(root: Path) -> list[Path]:
    files = []
    for relative in SOURCE_ROOTS:
        base = root / relative
        if not base.is_dir() or base.is_symlink():
            continue
        for directory, names, entries in os.walk(base, followlinks=False):
            names[:] = sorted(name for name in names if name not in SKIP_DIRS and not name.startswith(".")
                              and not (Path(directory) / name).is_symlink())
            files.extend(Path(directory) / name for name in entries if Path(name).suffix in SOURCE_SUFFIXES
                         and not name.startswith(".") and not (Path(directory) / name).is_symlink())
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


class SourceModule:
    def __init__(self, path: str, text: str):
        self.path = path
        relative = path.removeprefix("backend/").removesuffix(".py")
        self.name = relative.replace("/", ".").removesuffix(".__init__")
        self.tree = ast.parse(text, filename=path)
        self.parents = {child: parent for parent in ast.walk(self.tree) for child in ast.iter_child_nodes(parent)}
        self.imports = {}
        self.assignments = {}
        self.functions = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                package = self.name if path.endswith("/__init__.py") else self.name.rsplit(".", 1)[0]
                base = package.split(".")[:len(package.split(".")) - node.level + 1] if node.level else []
                module = ".".join(base + ([node.module] if node.module else []))
                self.imports.update({item.asname or item.name: (module, item.name) for item in node.names})
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                self.assignments.update({item.id: node.value for item in targets if isinstance(item, ast.Name)})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[self.owner(node)] = node

    def owner(self, node: ast.AST) -> str:
        parts = []
        current = node
        while current in self.parents or current is self.tree:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parts.append(current.name)
            if current not in self.parents:
                break
            current = self.parents[current]
        return ".".join(reversed(parts)) or "<module>"

    def conditions(self, node: ast.AST) -> list[dict]:
        conditions = []
        current = node
        while current in self.parents:
            parent = self.parents[current]
            if isinstance(parent, ast.If):
                conditions.append({"expression": ast.unparse(parent.test),
                                   "branch": "else" if current in parent.orelse else "then",
                                   "source": source_ref(self.path, parent)})
            current = parent
        return list(reversed(conditions))


def dependencies(nodes: list[ast.AST], module: SourceModule) -> list[dict]:
    found = {}
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Call) and leaf_name(node.func) in {"Depends", "Security"} and node.args:
                dependency = node.args[0]
                name = callable_name(dependency)
                item = {"callable": name, "source": source_ref(module.path, node)}
                if leaf_name(dependency) in DECLARED_ROLE_GATES:
                    item["roles_from_declared_gate"] = DECLARED_ROLE_GATES[leaf_name(dependency)]
                if leaf_name(dependency) in {"require_roles", "require_role", "require_permissions"}:
                    item["arguments"] = [ast.unparse(arg) for arg in dependency.args] if isinstance(dependency, ast.Call) else []
                found[(name, node.lineno)] = item
    return list(found.values())


def controls(module: SourceModule, node: ast.AST) -> list[dict]:
    return [{"callable": callable_name(call.func), "source": source_ref(module.path, call)}
            for call in ast.walk(node) if isinstance(call, ast.Call)
            and (leaf_name(call.func) in SCOPE_CALLS or leaf_name(call.func).startswith("require_"))]


def resolve_symbol(modules: dict, module: SourceModule, name: str, seen: set | None = None) -> str:
    seen = set() if seen is None else seen
    key = f"{module.name}:{name}"
    if key in seen:
        return key
    seen.add(key)
    if name in module.imports:
        target, symbol = module.imports[name]
        if target in modules:
            return resolve_symbol(modules, modules[target], symbol, seen)
        if f"{target}.{symbol}" in modules:
            return f"{target}.{symbol}"
    value = module.assignments.get(name)
    if isinstance(value, ast.Name):
        return resolve_symbol(modules, module, value.id, seen)
    return key


def path_value(node: ast.AST | None, bindings: dict) -> dict:
    if node is None:
        return {"template": "", "source_default": "", "status": "literal"}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {"template": node.value, "source_default": node.value, "status": "literal"}
    expression = ast.unparse(node)
    if expression in bindings:
        return {"template": "{" + expression + "}", "source_default": bindings[expression]["value"],
                "status": "source-default", "binding": bindings[expression]}
    if isinstance(node, ast.JoinedStr):
        parts = [path_value(part.value, bindings) if isinstance(part, ast.FormattedValue)
                 else path_value(part, bindings) for part in node.values]
        return combine_paths(parts, separator="")
    return {"template": "{" + expression + "}", "source_default": None, "status": "unresolved"}


def combine_paths(parts: list[dict], separator: str = "") -> dict:
    return {"template": separator.join(part["template"] for part in parts),
            "source_default": separator.join(part["source_default"] for part in parts)
            if all(part["source_default"] is not None for part in parts) else None,
            "status": "unresolved" if any(part["status"] == "unresolved" for part in parts)
            else "source-default" if any(part["status"] == "source-default" for part in parts) else "literal"}


def backend_inventory(modules: dict, bindings: dict) -> tuple[list[dict], list[dict]]:
    routers, mounts, surfaces, warnings = {}, [], [], []
    for module in modules.values():
        for variable, value in module.assignments.items():
            if isinstance(value, ast.Call) and leaf_name(value.func) in {"APIRouter", "FastAPI"}:
                routers[f"{module.name}:{variable}"] = {
                    "prefix": path_value(keyword(value, "prefix"), bindings),
                    "dependencies": dependencies([value], module), "source": source_ref(module.path, value),
                    "is_application": leaf_name(value.func) == "FastAPI",
                }
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router":
                if not node.args:
                    warnings.append({"source": source_ref(module.path, node), "reason": "Router target is not positional."})
                    continue
                mount = {"parent": resolve_symbol(modules, module, ast.unparse(node.func.value)),
                         "child": resolve_symbol(modules, module, ast.unparse(node.args[0])),
                         "prefix": path_value(keyword(node, "prefix"), bindings),
                         "dependencies": dependencies([node], module), "conditions": module.conditions(node),
                         "source": source_ref(module.path, node)}
                mounts.append(mount)

    def locations(symbol: str, trail: tuple = ()) -> list[dict]:
        if symbol in trail:
            return [{"path": path_value(None, bindings), "status": "unresolved-cycle", "dependencies": [], "mounts": []}]
        definition = routers.get(symbol)
        if definition is None:
            return [{"path": path_value(None, bindings), "status": "unresolved-router", "dependencies": [], "mounts": []}]
        if definition["is_application"]:
            return [{"path": definition["prefix"], "status": "application", "dependencies": definition["dependencies"], "mounts": []}]
        edges = [mount for mount in mounts if mount["child"] == symbol]
        if not edges:
            return [{"path": definition["prefix"], "status": "unmounted", "dependencies": definition["dependencies"], "mounts": []}]
        return [{"path": combine_paths([parent["path"], edge["prefix"], definition["prefix"]]),
                 "status": parent["status"], "dependencies": parent["dependencies"] + edge["dependencies"] + definition["dependencies"],
                 "mounts": parent["mounts"] + [edge]}
                for edge in edges for parent in locations(edge["parent"], trail + (symbol,))]

    def add_route(module: SourceModule, call: ast.Call, owner: ast.AST, action: str) -> None:
        route = path_value(call.args[0] if call.args else keyword(call, "path"), bindings)
        router_symbol = resolve_symbol(modules, module, ast.unparse(call.func.value))
        method_value = keyword(call, "methods")
        if action in {"websocket", "websocket_route", "add_websocket_route"}:
            methods = ["WEBSOCKET"]
        elif action in HTTP_METHODS:
            methods = [action.upper()]
        elif method_value is None:
            methods = ["GET"]
        else:
            try:
                methods = ast.literal_eval(method_value)
                if not isinstance(methods, (list, tuple, set)) or not all(isinstance(item, str) for item in methods):
                    raise ValueError("Nonliteral methods")
            except (ValueError, TypeError, SyntaxError):
                methods = ["UNRESOLVED"]
        for location in locations(router_symbol):
            mounted = combine_paths([location["path"], route])
            for method in sorted(methods):
                kind = "backend_websocket" if method == "WEBSOCKET" else "backend_http"
                identity = f"{module.name}:{module.owner(owner)}:{method}:{mounted['template']}"
                surfaces.append(new_surface(kind, identity, module.path, call, symbol=module.owner(owner), method=method,
                    path=mounted, registration=location["status"], mount_chain=location["mounts"],
                    declared_dependencies=location["dependencies"] + dependencies([owner.args, call], module)
                    if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) else location["dependencies"] + dependencies([call], module),
                    scope_control_candidates=controls(module, owner), conditions=module.conditions(call)))

    for module in modules.values():
        for function in module.functions.values():
            for decorator in function.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                    action = decorator.func.attr
                    if action in HTTP_METHODS | {"api_route", "route", "websocket", "websocket_route"}:
                        add_route(module, decorator, function, action)
                    elif action == "on_event":
                        surfaces.append(new_surface("backend_lifecycle", f"{module.name}:{function.name}", module.path,
                                                    decorator, symbol=module.owner(function), event=path_value(decorator.args[0], bindings)))
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Call):
                continue
            name, leaf = callable_name(node.func), leaf_name(node.func)
            owner = module.owner(node)
            if leaf in {"add_api_route", "add_route", "add_websocket_route"} and isinstance(node.func, ast.Attribute):
                add_route(module, node, node, leaf)
            if leaf in {"Route", "WebSocketRoute", "Mount"}:
                surfaces.append(new_surface("asgi_route_definition", f"{module.name}:{owner}:{node.lineno}", module.path,
                    node, path=path_value(node.args[0] if node.args else keyword(node, "path"), bindings),
                    callable=name, registration="unverified", declared_dependencies=[], conditions=module.conditions(node)))
            if leaf == "append" and isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value).endswith(".routes"):
                surfaces.append(new_surface("asgi_registration", f"{module.name}:{owner}:{node.lineno}", module.path,
                    node, callable=name, target=ast.unparse(node.args[0]) if node.args else None,
                    path={"status": "unresolved", "template": None, "source_default": None}, conditions=module.conditions(node)))
            is_background = leaf in {"create_task", "ensure_future", "add_task", "add_job", "Thread", "ThreadPoolExecutor"}
            is_startup = module.path == "backend/app/main.py" and owner.startswith("lifespan") and (
                leaf.startswith(("start_", "stop_", "recover_", "resume_", "hydrate_", "prewarm_"))
                or leaf in {"init_db", "warm_up", "start", "stop"})
            if is_background or is_startup:
                kind = "background_dispatch" if is_background else "startup_service_call"
                surfaces.append(new_surface(kind, f"{module.name}:{owner}:{node.lineno}:{name}", module.path,
                    node, symbol=owner, callable=name,
                    target=callable_name(node.args[0]) if is_background and node.args else None,
                    conditions=module.conditions(node), scope_control_candidates=[], registration="static-callsite"))
    for mount in mounts:
        if mount["parent"] not in routers or mount["child"] not in routers:
            warnings.append({"source": mount["source"], "reason": "Router mount symbol unresolved.",
                             "parent": mount["parent"], "child": mount["child"]})
    return surfaces, warnings


def frontend_inventory(texts: dict[str, str]) -> list[dict]:
    surfaces = []
    for path, text in texts.items():
        if not path.startswith("frontend/src/app/") or not re.search(r"/(page|route)\.(tsx?|jsx?)$", path):
            continue
        segments = path.removeprefix("frontend/src/app/").split("/")[:-1]
        visible = [part for part in segments if not (part.startswith("(") and part.endswith(")")) and not part.startswith("@")]
        route = "/" + "/".join(visible)
        alias_methods = {method for block in re.findall(r"\bexport\s*\{([^}]*)\}", text)
                         for method in re.findall(r"\bas\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", block)}
        destructured_methods = {method for block in re.findall(r"\bexport\s+const\s*\{([^}]*)\}", text)
                                for method in re.findall(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", block)}
        methods = sorted(set(re.findall(r"\bexport\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", text))
                         | set(re.findall(r"\bexport\s+const\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", text))
                         | alias_methods | destructured_methods)
        kind = "frontend_handler" if "/route." in path else "frontend_page"
        surfaces.append(new_surface(kind, path, path, ast.Constant(value=None), path_template=route,
            methods=methods, dynamic=any("[" in part for part in segments),
            routing_requires_review=any(part.startswith(("(", "@")) for part in segments),
            declared_dependencies=[], scope_control_candidates=[{"token": token, "source": f"{path}:{index}"}
                for index, line in enumerate(text.splitlines(), 1)
                for token in ("usePermissions", "isPartner", "isAdmin", "isTenant", "useAuth", "notFound", "redirect")
                if re.search(r"\b" + token + r"\b", line)]))
    tabs_path = "frontend/src/app/settings/settings-tabs.ts"
    tabs = texts.get(tabs_path, "")
    declaration = re.search(r"SETTINGS_TABS\s*=\s*\[([\s\S]*?)\]", tabs)
    if declaration:
        for slug in re.findall(r'["\']([a-z-]+)["\']', declaration.group(1)):
            surfaces.append(new_surface("frontend_route_variant", f"settings:{slug}", tabs_path,
                ast.Constant(value=None), path_template="/settings" if slug == "general" else f"/settings/{slug}",
                declared_slug=slug, registration="source-declared-variant", declared_dependencies=[],
                scope_control_candidates=[]))
    return surfaces


def critical_contracts(modules: dict, texts: dict) -> list[dict]:
    wanted = {
        "app.routers.tasks": {"list_tasks", "get_queue"}, "app.routers.tasks_ws": {"tasks_websocket"},
        "app.routers.cluster_log": None,
        "app.routers.networks": {"get_network_creation_capabilities", "create_network", "update_network"},
        "app.routers.system": {"clear_cache"}, "app.routers.nodes": {"invalidate_node_cache", "invalidate_all_nodes_cache"},
        "app.routers.console": {"list_isos"}, "app.services.console_service": {"ConsoleService.get_available_isos"},
        "app.routers.organizations": {"get_my_org_preferences", "get_my_org_preferences_version", "get_org_preferences", "update_org_preferences"},
    }
    contracts = []
    for name, selected in wanted.items():
        module = modules.get(name)
        if not module:
            contracts.append({"symbol": name, "source": None, "status": "unresolved"})
            continue
        for symbol, function in module.functions.items():
            if selected is None or symbol in selected:
                contracts.append({"symbol": f"{name}:{symbol}", "source": source_ref(module.path, function),
                    "status": "source-observed-runtime-unverified", "declared_dependencies": dependencies([function.args] + function.decorator_list, module),
                    "scope_control_candidates": controls(module, function)})
        for missing in sorted((selected or set()) - module.functions.keys()):
            contracts.append({"symbol": f"{name}:{missing}", "source": None, "status": "unresolved"})
    frontend = {
        "frontend/src/app/settings/settings-view.tsx": ("denied", "isLoading", "isPartner", "navigate"),
        "frontend/src/app/settings/[tab]/page.tsx": ("isSettingsTab", "notFound", "redirect"),
        "frontend/src/app/settings/components/cache-settings.tsx": ("clear", "isAdmin", "scopeKey"),
        "frontend/src/components/personalization/use-personalization-sync.ts": ("scopeKey", "isActorMutationFenceCurrent", "sameOrgVersion", "appliedVersionRef", "visibilitychange", "45_000"),
        "frontend/src/components/personalization/preferences.ts": ("organization_id", "updated_at", "sameOrgVersion"),
    }
    for path, tokens in frontend.items():
        contracts.append({"symbol": path, "source": f"{path}:1" if path in texts else None,
            "status": "source-observed-runtime-unverified" if path in texts else "unresolved",
            "scope_control_candidates": [{"token": token, "source": f"{path}:{index}"}
                for index, line in enumerate(texts.get(path, "").splitlines(), 1) for token in tokens if token in line]})
    return contracts


def inventory(root: Path) -> dict:
    texts, modules, warnings = {}, {}, []
    digest = hashlib.sha256()
    for path in source_files(root):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode() + b"\0" + hashlib.sha256(content).digest())
        texts[relative] = content.decode("utf-8")
        if relative.startswith("backend/app/") and path.suffix == ".py":
            try:
                module = SourceModule(relative, texts[relative])
                modules[module.name] = module
            except SyntaxError as error:
                warnings.append({"source": f"{relative}:{error.lineno}", "reason": "Python source did not parse."})
    bindings = {}
    config = modules.get("app.core.config")
    if config:
        for node in ast.walk(config.tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "api_prefix" and isinstance(node.value, ast.Constant):
                bindings["settings.api_prefix"] = {"value": node.value.value, "source": source_ref(config.path, node), "runtime_verified": False}
    backend, unresolved = backend_inventory(modules, bindings)
    surfaces = sorted(backend + frontend_inventory(texts), key=lambda row: (row["kind"], row["source"], row["id"]))
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        commit = ""
    feature_matrix = [{"id": f"{family}/{feature}", "family": family, "feature": feature,
        "required_assertion": assertion, "platforms_to_verify_when_applicable": ["LXD", "Incus", "MicroCloud"],
        "role_cases": [{"role": role, "evidence": {**evidence(), "role": role}} for role in ("Admin", "Partner", "Tenant")],
        "scenarios_to_reconcile": ["success", "denial", "adjacent organization", "adjacent project", "refresh/retry",
            "timeout", "partial failure", "duplicate submission", "permission revocation"],
        "surface_candidates": [row["id"] for row in surfaces if family in row["feature_family_candidates"]],
        "status": "unverified"} for family, features in FEATURES.items() for feature, assertion in features]
    for row in surfaces:
        row["evidence"].update(source_commit=commit or None, source_tree_digest=digest.hexdigest())
    for requirement in feature_matrix:
        for case in requirement["role_cases"]:
            case["evidence"].update(source_commit=commit or None, source_tree_digest=digest.hexdigest())
    return {"schema_version": 1, "provenance": {"source_commit": commit or None,
            "source_tree_digest": digest.hexdigest(), "digest_algorithm": "sha256(sorted relative path + NUL + file sha256)",
            "digest_roots": list(SOURCE_ROOTS), "source_file_count": len(texts), "runtime_build": None},
        "counts": dict(sorted(Counter(row["kind"] for row in surfaces).items())),
        "evidence_contract": {
            "statuses": ["unverified", "passed", "failed", "blocked", "not-run", "not-applicable"],
            "verification_kinds": ["source-review", "unit", "fixture-browser", "database-integration", "live-end-to-end"],
            "required_for_pass": ["matching source and runtime build", "role and verified scope", "fixture",
                "exact commands", "observed assertions", "result", "sanitized artifact links"],
            "not_applicable_requires": "A source-grounded reason and reviewer acknowledgement; absence of a test is not a reason.",
            "refresh_semantics": "Generation creates a new unverified template. Reconcile external evidence by ID and provenance; do not overwrite a completed evidence ledger.",
        },
        "source_defaults": bindings, "surfaces": surfaces, "critical_contracts": critical_contracts(modules, texts),
        "feature_matrix": feature_matrix, "warnings": warnings + unresolved,
        "limitations": ["Source discovery is not execution or proof of effective authorization.",
            "Runtime flags and API prefix overrides have not been read; conditional registrations remain candidates.",
            "Frontend TypeScript is scanned for route files and declarations, not parsed or evaluated.",
            "Background dispatch includes transport and worker call sites; it does not prove a service is enabled or continuously running.",
            "Imported or delegated authorization and object scope must be traced and tested beyond candidate call sites.",
            "Dynamic registrations, generated framework routes, middleware, proxy routes, and arbitrary job dispatch may require manual additions.",
            "Family assignment is navigation metadata; candidate surfaces do not establish scenario coverage.",
            "A source digest identifies scanned files, not an image, dependency lock, configuration, database head, or running deployment."]}


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="orcastra-qa-inventory-") as directory:
        root = Path(directory)
        sources = {
            "backend/app/main.py": 'from app.routers import sample_router\nfrom fastapi import FastAPI\napp = FastAPI()\nif feature_enabled:\n app.include_router(sample_router, prefix=settings.api_prefix)\n',
            "backend/app/core/config.py": 'class Settings:\n api_prefix: str = "/api/v1"\n',
            "backend/app/routers/__init__.py": 'from .sample import router as sample_router\n',
            "backend/app/routers/sample.py": 'from fastapi import APIRouter, Depends\nrouter = APIRouter(prefix="/sample", dependencies=[Depends(require_tenant())])\n@router.get("/{id}")\nasync def read(id, user=Depends(require_partner())):\n verify_project_access(id)\n@router.websocket("/ws")\nasync def feed(websocket, user=Depends(get_ws_user)):\n await set_org_scope_for_ws(user)\nasync def worker():\n asyncio.create_task(reconcile())\n',
            "frontend/src/app/(group)/settings/[tab]/page.tsx": 'export default function Page() { return null; }\n',
            "frontend/src/app/settings/settings-tabs.ts": 'export const SETTINGS_TABS = ["general", "integrations"] as const;\n',
            "frontend/src/app/api/auth/[...nextauth]/route.ts": 'export { handler as GET, handler as POST };\n',
            "frontend/src/app/api/destructured/route.ts": 'export const { GET, POST } = handlers;\n',
            "backend/app/unmounted.py": 'router=APIRouter(prefix="/orphan")\n@router.post("/write")\nasync def write(): pass\n',
        }
        for relative, text in sources.items():
            file = root / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)
        # A source-like symlink must not turn the scan into a read outside the selected roots.
        (root / "backend/app/excluded.py").symlink_to(root / "not-source.txt")
        first = inventory(root)
        assert first == inventory(root), "The same input must produce identical inventory."
        route = next(row for row in first["surfaces"] if row.get("symbol") == "read")
        assert route["path"]["template"] == "{settings.api_prefix}/sample/{id}"
        assert route["path"]["source_default"] == "/api/v1/sample/{id}"
        assert route["path"]["status"] == "source-default"
        assert route["mount_chain"][0]["conditions"][0]["expression"] == "feature_enabled"
        assert [row["callable"] for row in route["declared_dependencies"]] == ["require_tenant", "require_partner"]
        assert route["scope_control_candidates"][0]["callable"] == "verify_project_access"
        assert first["counts"]["backend_websocket"] == 1 and first["counts"]["background_dispatch"] == 1
        orphan = next(row for row in first["surfaces"] if row.get("symbol") == "write")
        assert orphan["registration"] == "unmounted"
        page = next(row for row in first["surfaces"] if row["kind"] == "frontend_page")
        assert page["path_template"] == "/settings/[tab]" and page["routing_requires_review"]
        assert all(row["methods"] == ["GET", "POST"] for row in first["surfaces"] if row["kind"] == "frontend_handler")
        assert first["provenance"]["source_file_count"] == len(sources)
        assert all(row["evidence"]["status"] == "unverified" for row in first["surfaces"])
        (root / "backend/app/unmounted.py").write_text(sources["backend/app/unmounted.py"] + "\n# changed\n")
        assert inventory(root)["provenance"]["source_tree_digest"] != first["provenance"]["source_tree_digest"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, help="Write JSON to an explicit local evidence path.")
    parser.add_argument("--self-test", action="store_true", help="Run bounded synthetic parser checks without application imports.")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("QA surface inventory self-test passed.")
        return
    report = inventory(args.root.resolve())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"counts": report["counts"], "source_tree_digest": report["provenance"]["source_tree_digest"],
                          "warnings": len(report["warnings"]), "feature_requirements": len(report["feature_matrix"])}, sort_keys=True))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
