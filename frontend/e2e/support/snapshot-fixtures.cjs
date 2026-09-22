const gib = 1024 ** 3;

const DEFAULT_TARGET = Object.freeze({
  host: "cluster-a",
  hostLabel: "Core Cluster",
  project: "tenant-a",
  instanceName: "tenant-owned-vm",
  alternativeProject: "operations",
  alternativeInstanceName: "stopped-worker",
});

function snapshotTarget(options = {}) {
  return { ...DEFAULT_TARGET, ...(options.snapshotTarget ?? {}) };
}

function snapshotFixturesEnabled(options = {}) {
  return (
    options.snapshotPage === true ||
    options.snapshotInstancePage === true ||
    options.snapshotCreate != null
  );
}

function validateSnapshotFixtureOptions(options = {}) {
  const snapshotCreate = options.snapshotCreate ?? null;
  if (snapshotCreate) {
    if (!snapshotCreate.instanceName) {
      throw new Error("snapshotCreate.instanceName is required so mutation fixtures stay exact");
    }
    if (!Object.hasOwn(snapshotCreate, "expectedBody")) {
      throw new Error("snapshotCreate.expectedBody is required so mutation fixtures stay exact");
    }
    if (Object.hasOwn(snapshotCreate, "rawBody") && typeof snapshotCreate.rawBody !== "string") {
      throw new Error("snapshotCreate.rawBody must be a string when provided");
    }
    if (Object.hasOwn(snapshotCreate, "rawBody") && Object.hasOwn(snapshotCreate, "body")) {
      throw new Error("snapshotCreate must declare either body or rawBody, not both");
    }
    const expectedCount = snapshotCreate.expectedCount ?? 1;
    if (!Number.isInteger(expectedCount) || expectedCount < 0) {
      throw new Error("snapshotCreate.expectedCount must be a non-negative integer");
    }
  }

  const reconciliation = options.snapshotReconciliation ?? null;
  if (!reconciliation) return;
  const freshInstanceReads = reconciliation.freshInstanceReads ?? 0;
  if (!Number.isInteger(freshInstanceReads) || freshInstanceReads < 0) {
    throw new Error(
      "snapshotReconciliation.freshInstanceReads must be a non-negative integer",
    );
  }
  const operation = reconciliation.operation ?? null;
  if (!operation) return;
  if (!operation.id || !operation.host) {
    throw new Error("snapshotReconciliation.operation requires exact id and host values");
  }
  if (!Object.hasOwn(operation, "body")) {
    throw new Error("snapshotReconciliation.operation.body is required");
  }
}

function initializeSnapshotState(state, options = {}) {
  validateSnapshotFixtureOptions(options);
  const snapshotCreate = options.snapshotCreate ?? null;
  const reconciliation = options.snapshotReconciliation ?? null;
  const operation = reconciliation?.operation ?? null;
  Object.assign(state, {
    snapshotMutations: [],
    unexpectedMutations: [],
    expectedSnapshotMutationCount: snapshotCreate?.expectedCount ?? (snapshotCreate ? 1 : 0),
    createdSnapshotName: null,
    snapshotReadCounts: {},
    snapshotReconciliation: {
      freshInstanceReads: 0,
      operationReads: [],
    },
    expectedSnapshotReconciliation: {
      freshInstanceReads: reconciliation?.freshInstanceReads ?? 0,
      operationReads: operation ? 1 : 0,
    },
  });
}

function canonicalJson(value) {
  if (Array.isArray(value)) return value.map(canonicalJson);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, nested]) => [key, canonicalJson(nested)]),
    );
  }
  return value;
}

function sameJson(left, right) {
  return JSON.stringify(canonicalJson(left)) === JSON.stringify(canonicalJson(right));
}

function parseMutation(request, entry) {
  let body = null;
  let bodyParseError = null;
  try {
    body = JSON.parse(request.postData() ?? "");
  } catch (error) {
    bodyParseError = error instanceof Error ? error.message : "Invalid JSON";
  }
  return {
    mutation: {
      ...entry,
      body,
      contentType: request.headers()["content-type"] ?? null,
    },
    bodyParseError,
  };
}

function isExactSnapshotCreate({ options, method, path, url, mutation, bodyParseError }) {
  const expected = options.snapshotCreate ?? null;
  if (!expected) return false;
  return (
    method === "POST" &&
    path === `/instances/${encodeURIComponent(expected.instanceName)}/snapshots` &&
    url.search === "" &&
    mutation.contentType?.startsWith("application/json") &&
    !bodyParseError &&
    sameJson(mutation.body, expected.expectedBody)
  );
}

async function fulfillSnapshotCreate(route, state, options, mutation) {
  const expected = options.snapshotCreate;
  state.snapshotMutations.push(mutation);
  if (state.snapshotMutations.length > state.expectedSnapshotMutationCount) {
    state.unexpectedMutations.push({ ...mutation, reason: "too many snapshot mutations" });
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Unexpected duplicate snapshot mutation" }),
    });
    return;
  }

  if (expected.abort === true) {
    await route.abort("timedout");
    return;
  }
  if (expected.delayMs) {
    await new Promise((resolve) => setTimeout(resolve, expected.delayMs));
  }

  const status = expected.status ?? 201;
  const body = expected.body ?? {
    success: true,
    status: status === 202 ? "pending" : "succeeded",
    instance_name: expected.instanceName,
    snapshot:
      status === 202
        ? "ops-maintenance-20260920t120000z"
        : "ops-maintenance-20260920t120001z",
    operation_id: status === 202 ? "fixture-operation-1" : null,
    message:
      status === 202
        ? "Snapshot creation is still in progress."
        : "Snapshot created.",
  };
  if (
    !Object.hasOwn(expected, "rawBody") &&
    status === 201 &&
    body.status === "succeeded"
  ) {
    state.createdSnapshotName = body.snapshot;
  }
  await route.fulfill({
    status,
    headers: expected.headers,
    contentType: "application/json",
    body: Object.hasOwn(expected, "rawBody") ? expected.rawBody : JSON.stringify(body),
  });
}

function targetInstance(fullInstance, target, status = "Running") {
  return {
    ...fullInstance(status),
    name: target.instanceName,
    host: target.host,
    host_endpoint: `https://${target.host}.invalid:8443`,
    project: target.project,
  };
}

function projectInventory(target) {
  return [
    {
      host: target.host,
      projects: [
        {
          name: target.project,
          description: "Snapshot fixture project",
          config: {},
          used_by: [`/1.0/instances/${target.instanceName}?project=${target.project}`],
          host: target.host,
          tags: [],
        },
        {
          name: target.alternativeProject,
          description: "Second project used to prove cascading target resets",
          config: {},
          used_by: [],
          host: target.host,
          tags: [],
        },
      ],
      total: 2,
      reachable: true,
      error: null,
    },
  ];
}

function clusterAvailability(target, iso) {
  return {
    clusters: [
      {
        name: target.host,
        display_name: target.hostLabel,
        endpoint: `https://${target.host}.invalid:8443`,
        status: "online",
        reason_code: "ok",
        reason: null,
        node_type: "LXD",
        checked_at: iso(),
        available: true,
      },
      {
        name: "cluster-unreachable",
        display_name: "Edge Cluster",
        endpoint: "https://edge.invalid:8443",
        status: "offline",
        reason_code: "unreachable",
        reason: "Connection timed out",
        node_type: "LXD",
        checked_at: iso(),
        available: false,
      },
      {
        name: "cluster-untrusted",
        display_name: "Untrusted Cluster",
        endpoint: "https://untrusted.invalid:8443",
        status: "not_authorized",
        reason_code: "untrusted_certificate",
        reason: "Trust has not been established",
        node_type: "LXD",
        checked_at: iso(),
        available: false,
      },
    ],
    online_count: 1,
    total_count: 3,
    source: "availability_cache",
    generated_at: iso(),
  };
}

function instanceInventory(fullInstance, target, options, unreachableOverride) {
  const unreachable =
    unreachableOverride ?? options.snapshotInstancesUnreachable === true;
  const vms = unreachable
    ? []
    : [
        targetInstance(fullInstance, target, options.instanceStatus || "Running"),
        {
          ...targetInstance(fullInstance, target, "Stopped"),
          name: target.alternativeInstanceName,
          description: "A second explicit target",
          type: "container",
          ip_addresses: [],
        },
      ];
  return {
    total: vms.length,
    vms,
    nodes: [target.host],
    lxd_count: vms.length,
    microcloud_count: 0,
    incus_count: 0,
    offset: 0,
    limit: 100,
    has_more: false,
    unreachable_nodes: unreachable ? [target.host] : [],
  };
}

function snapshotRow(name, target, { billingLinked = false } = {}) {
  return {
    name,
    source_name: target.instanceName,
    created_at: "2026-09-19T09:00:00Z",
    expires_at: "2026-09-26T09:00:00Z",
    stateful: false,
    size: 2 * gib,
    architecture: "x86_64",
    operator_managed: true,
    origin: "operator",
    size_measured: true,
    host: target.host,
    project: target.project,
    source_status: "Running",
    source_externally_managed: billingLinked,
    source_schedule: billingLinked ? "daily" : null,
    source_expiry: billingLinked ? "7d" : null,
  };
}

function snapshotRows(options, state, target) {
  const count = Math.max(0, Math.min(options.snapshotCount ?? 1, 5));
  const rows = Array.from({ length: count }, (_, index) =>
    snapshotRow(`ops-baseline-${index + 1}-20260919t090000z`, target, {
      billingLinked: options.snapshotBillingLinked === true,
    }),
  );
  if (state.createdSnapshotName) rows.unshift(snapshotRow(state.createdSnapshotName, target));
  return rows;
}

function policyResponse(options, state, target) {
  const items = snapshotRows(options, state, target);
  return {
    total: items.length,
    items,
    policy: {
      schedule: options.snapshotBillingLinked === true ? "daily" : null,
      schedule_stopped: null,
      expiry: options.snapshotBillingLinked === true ? "7d" : null,
      pattern: options.snapshotBillingLinked === true ? "billing-*" : null,
      externally_managed: options.snapshotBillingLinked === true,
    },
  };
}

function fleetResponse(options, state, target) {
  const items = snapshotRows(options, state, target);
  return {
    total: items.length,
    items,
    unreachable_nodes: [],
    queried_nodes: [target.host],
    partial_nodes: [],
    truncated: false,
    size_measurement: "measured",
    measured_instances: items.length > 0 ? 1 : 0,
    unmeasured_instances: 0,
  };
}

function nextRead(options, state, kind, body) {
  const call = state.snapshotReadCounts[kind] ?? 0;
  state.snapshotReadCounts[kind] = call + 1;
  const override = options.snapshotReadSequences?.[kind]?.[call];
  if (!override) return { status: 200, body };
  return {
    status: override.status ?? 200,
    headers: override.headers,
    body: Object.hasOwn(override, "body") ? override.body : body,
    delayMs: override.delayMs,
    abort: override.abort,
  };
}

function invalidScope(state, entry, detail) {
  state.unexpected.push(entry);
  return { status: 599, body: { detail } };
}

function snapshotReadDescriptor({ path, url, entry, role, options, state, iso, fullInstance }) {
  if (!snapshotFixturesEnabled(options)) return null;
  const target = snapshotTarget(options);
  const encodedInstance = encodeURIComponent(target.instanceName);

  if (path === "/nodes/names") {
    return nextRead(options, state, "nodeNames", [
      target.host,
      "cluster-unreachable",
      "cluster-untrusted",
    ]);
  }
  if (path === "/nodes/availability") {
    return nextRead(options, state, "nodeAvailability", clusterAvailability(target, iso));
  }
  if (path === "/projects") {
    return nextRead(options, state, "fleetProjects", projectInventory(target));
  }
  if (path === `/projects/${encodeURIComponent(target.host)}`) {
    return nextRead(options, state, "projects", projectInventory(target)[0]);
  }
  if (path === "/snapshot-schedules") {
    return nextRead(options, state, "schedules", { total: 0, items: [] });
  }
  if (path === "/snapshots") {
    const host = url.searchParams.get("host");
    const project = url.searchParams.get("project");
    const fresh = url.searchParams.get("fresh");
    const allowedKeys = new Set(["host", "project", "fresh"]);
    const hasUnexpectedKey = Array.from(url.searchParams.keys()).some(
      (key) => !allowedKeys.has(key),
    );
    const unscoped = host === null && project === null;
    const exactKnownScope =
      host === target.host &&
      [target.project, target.alternativeProject].includes(project);
    if (
      role === "tenant" ||
      hasUnexpectedKey ||
      (fresh !== null && fresh !== "true") ||
      (!unscoped && !exactKnownScope)
    ) {
      return invalidScope(state, entry, `Unexpected fleet snapshot scope: ${url.search}`);
    }
    const descriptor = nextRead(
      options,
      state,
      "fleetSnapshots",
      fleetResponse(options, state, target),
    );
    if (url.searchParams.get("fresh") === "true" && options.snapshotFreshFleetDelayMs) {
      descriptor.delayMs = options.snapshotFreshFleetDelayMs;
    }
    return descriptor;
  }
  if (path === "/instances") {
    const host = url.searchParams.get("host");
    const project = url.searchParams.get("project");
    const unscoped = host === null && project === null;
    const queryKeys = Array.from(url.searchParams.keys());
    const exactFreshFleetRead =
      unscoped &&
      url.searchParams.get("fresh") === "true" &&
      queryKeys.length === 1 &&
      queryKeys[0] === "fresh";
    // An instance-page fixture already declares the fleet stream whose initial sync uses this
    // same read. Snapshot-only pages must opt into each post-mutation reconciliation explicitly.
    if (exactFreshFleetRead && options.snapshotInstancePage !== true) {
      state.snapshotReconciliation.freshInstanceReads += 1;
      if (
        state.snapshotReconciliation.freshInstanceReads >
        state.expectedSnapshotReconciliation.freshInstanceReads
      ) {
        return invalidScope(state, entry, "Undeclared snapshot instance reconciliation read");
      }
      return nextRead(
        options,
        state,
        "reconciliationInstances",
        instanceInventory(fullInstance, target, options),
      );
    }
    if (unscoped && (options.snapshotInstancePage === true || role === "tenant")) {
      return nextRead(
        options,
        state,
        "instancePage",
        instanceInventory(fullInstance, target, options),
      );
    }
    if (host !== target.host || ![target.project, target.alternativeProject].includes(project)) {
      return invalidScope(state, entry, `Unexpected snapshot instance scope: ${url.search}`);
    }
    const instanceCall = state.snapshotReadCounts.instances ?? 0;
    const unreachable =
      options.snapshotInstancesUnreachable === true ||
      instanceCall < (options.snapshotInstancesUnreachableReads ?? 0);
    const body =
      project === target.alternativeProject
        ? { ...instanceInventory(fullInstance, target, options), total: 0, vms: [], lxd_count: 0 }
        : instanceInventory(fullInstance, target, options, unreachable);
    return nextRead(options, state, "instances", body);
  }
  if (path === `/instances/${encodedInstance}/snapshots`) {
    if (
      url.searchParams.get("host") !== target.host ||
      url.searchParams.get("project") !== target.project
    ) {
      return invalidScope(state, entry, `Unexpected snapshot policy scope: ${url.search}`);
    }
    return nextRead(options, state, "policy", policyResponse(options, state, target));
  }
  if (path === `/instances/${encodedInstance}`) {
    if (
      url.searchParams.get("host") !== target.host ||
      url.searchParams.get("project") !== target.project
    ) {
      return invalidScope(state, entry, `Unexpected snapshot detail scope: ${url.search}`);
    }
    state.detailCalls += 1;
    return nextRead(
      options,
      state,
      "detail",
      targetInstance(fullInstance, target, options.instanceStatus || "Running"),
    );
  }
  const expectedOperation = options.snapshotReconciliation?.operation ?? null;
  if (
    expectedOperation &&
    path === `/operations/${encodeURIComponent(expectedOperation.id)}`
  ) {
    const exactHost = url.searchParams.get("host") === expectedOperation.host;
    const queryKeys = Array.from(url.searchParams.keys());
    const exactQuery = exactHost && queryKeys.length === 1 && queryKeys[0] === "host";
    state.snapshotReconciliation.operationReads.push({
      id: expectedOperation.id,
      host: url.searchParams.get("host"),
    });
    if (
      !exactQuery ||
      state.snapshotReconciliation.operationReads.length >
        state.expectedSnapshotReconciliation.operationReads
    ) {
      return invalidScope(state, entry, `Unexpected snapshot operation scope: ${url.search}`);
    }
    return {
      status: expectedOperation.status ?? 200,
      headers: expectedOperation.headers,
      body: expectedOperation.body,
      delayMs: expectedOperation.delayMs,
    };
  }
  return null;
}

async function fulfillSnapshotRead(route, descriptor) {
  if (descriptor.abort) {
    await route.abort(descriptor.abort === true ? "timedout" : descriptor.abort);
    return;
  }
  if (descriptor.delayMs) {
    await new Promise((resolve) => setTimeout(resolve, descriptor.delayMs));
  }
  await route.fulfill({
    status: descriptor.status ?? 200,
    headers: descriptor.headers,
    contentType: "application/json",
    body: JSON.stringify(descriptor.body),
  });
}

module.exports = {
  fulfillSnapshotCreate,
  fulfillSnapshotRead,
  initializeSnapshotState,
  isExactSnapshotCreate,
  parseMutation,
  snapshotFixturesEnabled,
  snapshotReadDescriptor,
  snapshotTarget,
};
