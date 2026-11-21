// Mini's own API endpoints for the hermetic browser suite (e2e/support/edition.cjs loads this file
// and fixtures.cjs asks it only for a GET no CMP fixture answers). Specs shape the answers through
// `options.mini`; without it, only the reads every Mini shell page makes are answered, so a CMP
// spec that wanders into a Mini endpoint still fails as an unexpected request.

const DAY = 86_400_000;
const MINI_ROLES = new Set(["admin", "partner", "tenant"]);

function iso(offsetDays) {
  return new Date(Date.now() + offsetDays * DAY).toISOString();
}

function fingerprint(seed) {
  const hex = Buffer.from(seed.padEnd(32, "-")).toString("hex").slice(0, 64);
  return hex.match(/../g).join(":").toUpperCase();
}

/** The signed-in certificate, far from expiry unless a spec says otherwise. */
function myCertificate(role, overrides = {}) {
  const miniRole = MINI_ROLES.has(role) ? role : "tenant";
  return {
    email: `${role}@e2e.invalid`,
    display_name: null,
    role: miniRole,
    fingerprint: fingerprint(`${role}-e2e`),
    common_name: `${role}-e2e`,
    not_after: iso(200),
    days_left: 200,
    expiring: false,
    renew_available: false,
    renew_opens_at: iso(170),
    other_active_certificates: 0,
    ...overrides,
  };
}

function certificate(seed, notAfterDays, status = "active") {
  return {
    fingerprint: fingerprint(seed),
    common_name: seed,
    status,
    not_after: iso(notAfterDays),
    created_at: iso(-30),
    last_used_at: status === "active" ? iso(-1) : null,
    renewed_from_fingerprint: null,
  };
}

function operator(fields) {
  const expiresDays = fields.expiresDays ?? 300;
  return {
    uuid: fields.uuid,
    email: fields.email,
    display_name: fields.display_name ?? null,
    role: fields.role,
    status: fields.status ?? "active",
    owner_org_id: fields.org ? 7 : null,
    owner_org_name: fields.org ?? null,
    issued_by: "admin@e2e.invalid",
    last_login_at: fields.status === "revoked" ? null : iso(-1),
    created_at: iso(-30),
    expires_at: iso(expiresDays),
    expiring: expiresDays <= 30,
    certificates: [certificate(fields.email, expiresDays, fields.status === "revoked" ? "revoked" : "active")],
    manageable: fields.manageable ?? true,
  };
}

/** What GET /operators answers an Administrator; a Partner sees only its organization's Tenants. */
const OPERATORS = Object.freeze([
  operator({ uuid: "op-admin", email: "admin@e2e.invalid", role: "admin", manageable: false }),
  operator({ uuid: "op-partner", email: "partner@e2e.invalid", display_name: "Pat Partner", role: "partner", org: "Acme Hosting", expiresDays: 200 }),
  operator({ uuid: "op-tenant", email: "tina@e2e.invalid", display_name: "Tina Tenant", role: "tenant", org: "Acme Hosting", expiresDays: 12 }),
  operator({ uuid: "op-revoked", email: "rex@e2e.invalid", role: "tenant", status: "revoked", org: "Acme Hosting", expiresDays: 90 }),
]);

function operatorsFor(role) {
  if (role === "admin") return OPERATORS;
  if (role === "partner") return OPERATORS.filter((o) => o.role === "tenant" && o.owner_org_name === "Acme Hosting");
  return null;
}

const ORGANIZATIONS = Object.freeze([
  { id: 7, name: "Acme Hosting", slug: "acme-hosting", description: null, created_by: "admin@e2e.invalid", is_active: true, created_at: iso(-90) },
]);

const AUDIT_ROWS = Object.freeze([
  { id: 3, event_id: "evt-3", occurred_at: iso(-0.01), actor_email: "admin@e2e.invalid", actor_role: "admin", action: "operator.issue", category: "identity", severity: "info", result: "success", target_type: "operator", target_id: "tina@e2e.invalid", target_host: null, target_project: null, source_ip: "198.51.100.7", request_id: "req-3", details: { role: "tenant" } },
  { id: 2, event_id: "evt-2", occurred_at: iso(-0.02), actor_email: "partner@e2e.invalid", actor_role: "partner", action: "auth.cert_login", category: "auth", severity: "info", result: "success", target_type: null, target_id: null, target_host: null, target_project: null, source_ip: "198.51.100.8", request_id: "req-2", details: null },
  { id: 1, event_id: "evt-1", occurred_at: iso(-0.03), actor_email: null, actor_role: null, action: "auth.cert_login", category: "auth", severity: "warning", result: "denied", target_type: null, target_id: null, target_host: null, target_project: null, source_ip: "203.0.113.9", request_id: "req-1", details: { reason: "not_enrolled" } },
]);

const VMS = Object.freeze([
  { name: "shop-web-01", status: "Running", host: "cluster-a", project: "shop", type: "virtual-machine", ip_addresses: ["10.0.0.11"], tags: ["web"], description: "Storefront" },
  { name: "billing-db", status: "Stopped", host: "cluster-a", project: "billing", type: "container", ip_addresses: [], tags: [], description: "" },
]);

const PROJECTS = Object.freeze([
  { name: "shop", description: "Storefront", tags: [] },
  { name: "billing", description: "Invoices", tags: [] },
]);

function answer({ method, path, role, options }) {
  if (method !== "GET") return undefined;
  const mini = options.mini ?? {};

  if (path === "/me/certificate") return { body: myCertificate(role, mini.myCertificate) };

  if (!mini.pages) return undefined;
  if (path === "/operators") {
    const rows = operatorsFor(role);
    return rows ? { body: rows } : { status: 403, body: { detail: "Operators are managed by Partners and Administrators" } };
  }
  if (path === "/organizations") return { body: ORGANIZATIONS };
  if (role !== "admin" && path.startsWith("/audit-log")) {
    return { status: 403, body: { detail: "The audit log is open to Administrators only" } };
  }
  if (path === "/audit-log") return { body: { items: AUDIT_ROWS, total: AUDIT_ROWS.length, page: 1, page_size: 1000 } };
  if (path === "/audit-log/status") {
    return { body: { retention_days: 0, rows: AUDIT_ROWS.length, oldest: AUDIT_ROWS[2].occurred_at, pruned_through_id: 0, last_verified_max_id: 3, writer: { running: true, dropped: 0, failed: 0, last_error: null } } };
  }
  // The palette's resource reads (the list pages' own endpoints).
  if (path === "/vms") return { body: { total: VMS.length, vms: VMS, nodes: ["cluster-a"], lxd_count: VMS.length, microcloud_count: 0, incus_count: 0, offset: 0, limit: 0, has_more: false } };
  if (path === "/projects") return { body: [{ host: "cluster-a", projects: PROJECTS, total: PROJECTS.length, reachable: true }] };
  if (path === "/audit-log/verify") {
    return { body: { valid: true, checked: 3, broken_at_id: null, count: 3, max_id: 3, head_hash: "ab".repeat(32), pruned_through_id: 0, note: null } };
  }
  return undefined;
}

// The expiry notice on every shell page reads the signed-in certificate.
const shellReads = Object.freeze(["/me/certificate"]);

module.exports = { answer, shellReads, myCertificate, OPERATORS, AUDIT_ROWS };
