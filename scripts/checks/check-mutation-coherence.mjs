#!/usr/bin/env node
/**
 * Fail closed when a frontend API write has no declared cache/terminal contract.
 *
 * This is intentionally structural. The project has no frontend unit runner, while this guard
 * can still cover every direct API member call and every exported mutation hook in CI.
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(
  process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."),
);
const requireFromFrontend = createRequire(join(root, "frontend/package.json"));
const ts = requireFromFrontend("typescript");

function loadRegistry() {
  const path = "frontend/src/lib/mutation-coherence.ts";
  const output = ts.transpileModule(readFileSync(join(root, path), "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
    },
    fileName: path,
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", output)(
    (specifier) => {
      throw new Error(`Unexpected runtime import in mutation registry: ${specifier}`);
    },
    module,
    module.exports,
  );
  return module.exports;
}

function sourceFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(?:ts|tsx)$/.test(name) ? [path] : [];
  });
}

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const FRONTEND_SOURCE_PATHS = sourceFiles(join(root, "frontend/src"))
  .map((path) => relative(root, path))
  .sort();

function unwrapExpression(node) {
  let current = node;
  while (
    current &&
    (ts.isAsExpression(current) ||
      ts.isTypeAssertionExpression(current) ||
      ts.isParenthesizedExpression(current) ||
      ts.isSatisfiesExpression?.(current))
  ) {
    current = current.expression;
  }
  return current;
}

function staticPropertyName(node) {
  if (!node) return null;
  if (
    ts.isIdentifier(node) ||
    ts.isStringLiteral(node) ||
    ts.isNumericLiteral(node) ||
    ts.isNoSubstitutionTemplateLiteral(node)
  ) {
    return node.text;
  }
  if (ts.isComputedPropertyName(node)) {
    const expression = unwrapExpression(node.expression);
    if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
      return expression.text;
    }
  }
  return null;
}

function isFunctionNode(node) {
  return (
    ts.isArrowFunction(node) ||
    ts.isFunctionExpression(node) ||
    ts.isFunctionDeclaration(node) ||
    ts.isMethodDeclaration(node)
  );
}

/**
 * Build a fail-closed inventory from the transport implementation, not the member name.
 * One function may be reachable through several exported aliases (`vmAPI.start` and
 * `api.vms.start`); those paths share one implementation identity and one mutation contract.
 */
function discoverApiWritersFromSources(sources) {
  const writers = new Map();
  const diagnostics = [];

  for (const { path, source } of sources) {
    const sourceFile = ts.createSourceFile(
      path,
      source,
      ts.ScriptTarget.Latest,
      true,
      path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );
    const bindings = new Map();
    const exportedRoots = [];

    for (const statement of sourceFile.statements) {
      if (ts.isFunctionDeclaration(statement) && statement.name) {
        bindings.set(statement.name.text, statement);
        continue;
      }
      if (!ts.isVariableStatement(statement)) continue;
      const exported = statement.modifiers?.some(
        (modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword,
      );
      for (const declaration of statement.declarationList.declarations) {
        if (!ts.isIdentifier(declaration.name) || !declaration.initializer) continue;
        bindings.set(declaration.name.text, declaration.initializer);
        if (exported && /^(?:api|[A-Za-z0-9_]+API)$/.test(declaration.name.text)) {
          exportedRoots.push(declaration.name.text);
        }
      }
    }

    function httpMethods(node, publicPath) {
      const methods = new Set();
      const dynamicMethods = [];
      function visit(current) {
        if (ts.isPropertyAssignment(current)) {
          const name = staticPropertyName(current.name);
          if (name === "method") {
            const value = unwrapExpression(current.initializer);
            if (ts.isStringLiteral(value) || ts.isNoSubstitutionTemplateLiteral(value)) {
              const method = value.text.toUpperCase();
              if (WRITE_METHODS.has(method)) methods.add(method);
            } else {
              const line =
                sourceFile.getLineAndCharacterOfPosition(current.getStart(sourceFile)).line + 1;
              dynamicMethods.push(`${path}:${line}: ${publicPath} has a dynamic HTTP method`);
            }
          }
        }
        ts.forEachChild(current, visit);
      }
      visit(node);
      return { methods, dynamicMethods };
    }

    function registerFunction(node, publicPath) {
      const { methods, dynamicMethods } = httpMethods(node, publicPath);
      diagnostics.push(...dynamicMethods);
      if (methods.size === 0) return;
      const identity = `${path}:${node.getStart(sourceFile)}:${node.getEnd()}`;
      let writer = writers.get(identity);
      if (!writer) {
        writer = { identity, path, methods: new Set(), publicPaths: new Set() };
        writers.set(identity, writer);
      }
      methods.forEach((method) => writer.methods.add(method));
      writer.publicPaths.add(publicPath);
    }

    function walk(value, publicPath, resolving = new Set()) {
      const node = unwrapExpression(value);
      if (!node) return;
      if (ts.isIdentifier(node)) {
        if (resolving.has(node.text)) return;
        const target = bindings.get(node.text);
        if (!target) return;
        const nextResolving = new Set(resolving);
        nextResolving.add(node.text);
        walk(target, publicPath, nextResolving);
        return;
      }
      if (isFunctionNode(node)) {
        registerFunction(node, publicPath);
        return;
      }
      if (!ts.isObjectLiteralExpression(node)) return;

      for (const property of node.properties) {
        if (ts.isSpreadAssignment(property)) {
          walk(property.expression, publicPath, resolving);
          continue;
        }
        if (ts.isShorthandPropertyAssignment(property)) {
          walk(property.name, `${publicPath}.${property.name.text}`, resolving);
          continue;
        }
        if (ts.isMethodDeclaration(property)) {
          const name = staticPropertyName(property.name);
          if (name) registerFunction(property, `${publicPath}.${name}`);
          continue;
        }
        if (!ts.isPropertyAssignment(property)) continue;
        const name = staticPropertyName(property.name);
        if (!name) {
          const line =
            sourceFile.getLineAndCharacterOfPosition(property.getStart(sourceFile)).line + 1;
          diagnostics.push(`${path}:${line}: ${publicPath} has a non-static API member`);
          continue;
        }
        walk(property.initializer, `${publicPath}.${name}`, resolving);
      }
    }

    for (const rootName of exportedRoots) {
      walk(bindings.get(rootName), rootName, new Set([rootName]));
    }
  }

  return { writers: [...writers.values()], diagnostics };
}

const frontendSources = FRONTEND_SOURCE_PATHS.map((path) => ({
  path,
  source: readFileSync(join(root, path), "utf8"),
}));
const writerInventory = discoverApiWritersFromSources(frontendSources);
const WRITER_IMPLEMENTATION_PATHS = new Set(
  writerInventory.writers.map((writer) => writer.path),
);

// Runtime self-test: transport semantics must expose writers even when their names do not use
// the old mutation-prefix convention. It also proves aliases converge on one implementation.
const inventorySelfTest = discoverApiWritersFromSources([
  {
    path: "mutation-inventory-self-test.ts",
    source: `
      const inner = {
        bindCluster: () => fetchAPI("/clusters", { method: "POST" }),
        publishVersion: () => fetchAPI("/versions", { method: "PATCH" }),
        flushQueue: () => fetch("/queue", { method: "DELETE" }),
        lookup: () => fetchAPI("/clusters"),
      };
      export const fixtureAPI = { organizations: inner };
      export const api = { organizations: inner };
    `,
  },
]);
const selfTestPaths = new Set(
  inventorySelfTest.writers.flatMap((writer) => [...writer.publicPaths]),
);
assert.ok(selfTestPaths.has("fixtureAPI.organizations.bindCluster"));
assert.ok(selfTestPaths.has("api.organizations.bindCluster"));
assert.ok(selfTestPaths.has("fixtureAPI.organizations.publishVersion"));
assert.ok(selfTestPaths.has("fixtureAPI.organizations.flushQueue"));
assert.equal(selfTestPaths.has("fixtureAPI.organizations.lookup"), false);
assert.equal(inventorySelfTest.writers.length, 3);

const { MUTATION_IMPACTS, MUTATION_CALL_EXCEPTIONS } = loadRegistry();
const registered = new Map();
const failures = [...writerInventory.diagnostics];
for (const [impactId, impact] of Object.entries(MUTATION_IMPACTS)) {
  assert.ok(
    impact.completion === "response" || impact.completion === "terminal-operation",
    `${impactId} has an invalid completion boundary`,
  );
  assert.ok(impact.queryFamilies.length > 0, `${impactId} has no affected query family`);
  assert.ok(impact.apiCalls.length > 0, `${impactId} has no registered API call`);
  for (const call of impact.apiCalls) {
    assert.equal(
      registered.has(call),
      false,
      `${call} is ambiguously registered by ${registered.get(call)} and ${impactId}`,
    );
    registered.set(call, impactId);
  }
}
for (const [impactId, families] of Object.entries({
  "instance.create": ["instances", "instanceDetails", "monitoring", "ipam", "projectUsage", "resourceTags", "tagStats", "profiles", "networks", "storagePools", "storageVolumes"],
  "instance.create-legacy": ["instances", "instanceDetails", "monitoring", "ipam", "projectUsage", "profiles", "networks", "storagePools", "storageVolumes"],
  "instance.delete": ["instances", "instanceDetails", "monitoring", "ipam", "projectUsage", "profiles", "networks", "storagePools", "storageVolumes", "snapshots"],
  "image.upload": ["images", "storagePoolDetails", "nodeResources"],
  "iso.upload": ["storageVolumes", "storagePoolDetails", "nodeResources"],
  "node.update": ["nodes", "nodePicker", "clusters", "clusterDetails", "resourceTags", "tagStats"],
  "cluster.register": ["clusters", "clusterDetails", "nodes", "nodePicker", "organizations"],
  "cluster.auth-check": ["clusters", "clusterDetails", "nodes", "nodePicker"],
  "cluster.delete": ["clusters", "clusterDetails", "nodes", "nodePicker", "organizations"],
  "cluster.certificate": ["clusters", "clusterDetails", "certificates", "warnings"],
  "snapshot.write": ["snapshots", "snapshotSchedules", "instances", "instanceDetails", "monitoring", "ipam", "profiles", "networks", "storagePools", "storageVolumes"],
  "snapshot.schedule": ["snapshotSchedules", "snapshots"],
  "snapshot.schedule-run": ["snapshotSchedules", "snapshots", "instances", "storagePoolDetails"],
  "profile.write": ["profiles", "profileDetails", "instances", "monitoring", "ipam", "networks", "storagePools", "storageVolumes"],
  "instance.copy": ["instances", "instanceDetails", "monitoring", "ipam", "projectUsage", "profiles", "networks", "storagePools", "storageVolumes"],
})) {
  for (const family of families) {
    if (!MUTATION_IMPACTS[impactId]?.queryFamilies.includes(family)) {
      failures.push(`${impactId} must reconcile ${family}`);
    }
  }
}

const exceptions = new Set(Object.keys(MUTATION_CALL_EXCEPTIONS));
const writerByPublicPath = new Map();
const contractByWriter = new Map();

function declaredWriterPaths(writer, impactRegistry, exceptionRegistry) {
  return {
    impactPaths: [...writer.publicPaths].filter((publicPath) =>
      impactRegistry.has(publicPath),
    ),
    exceptionPaths: [...writer.publicPaths].filter((publicPath) =>
      exceptionRegistry.has(publicPath),
    ),
  };
}

const selfTestDeclarations = new Map([
  ["fixtureAPI.organizations.flushQueue", "self-test.write"],
]);
const selfTestUnregistered = inventorySelfTest.writers.filter((writer) => {
  const declarations = declaredWriterPaths(writer, selfTestDeclarations, new Set());
  return declarations.impactPaths.length === 0 && declarations.exceptionPaths.length === 0;
});
assert.deepEqual(
  new Set(selfTestUnregistered.map((writer) => [...writer.publicPaths].sort()[0])),
  new Set([
    "api.organizations.bindCluster",
    "api.organizations.publishVersion",
  ]),
);

function preferredWriterPath(writer) {
  return [...writer.publicPaths].sort((left, right) => {
    const leftRank = left.startsWith("api.") ? 0 : 1;
    const rightRank = right.startsWith("api.") ? 0 : 1;
    return leftRank - rightRank || left.localeCompare(right);
  })[0];
}

for (const writer of writerInventory.writers) {
  for (const publicPath of writer.publicPaths) {
    if (writerByPublicPath.has(publicPath)) {
      failures.push(`${publicPath}: exported API path resolves to multiple implementations`);
    }
    writerByPublicPath.set(publicPath, writer);
  }
  const { impactPaths, exceptionPaths } = declaredWriterPaths(
    writer,
    registered,
    exceptions,
  );
  const impactIds = new Set(impactPaths.map((publicPath) => registered.get(publicPath)));
  if (impactIds.size > 1) {
    failures.push(
      `${preferredWriterPath(writer)}: aliases have conflicting impacts ${[...impactIds].join(", ")}`,
    );
  }
  if (impactPaths.length > 0 && exceptionPaths.length > 0) {
    failures.push(
      `${preferredWriterPath(writer)}: aliases mix a mutation impact with a read-only/staged exception`,
    );
  }
  if (impactPaths.length === 0 && exceptionPaths.length === 0) {
    failures.push(
      `${preferredWriterPath(writer)}: unregistered exported ${[...writer.methods].join("/")} writer`,
    );
    continue;
  }
  contractByWriter.set(
    writer.identity,
    impactPaths.length > 0
      ? { kind: "impact", impactId: registered.get(impactPaths[0]), declaredBy: impactPaths }
      : { kind: "exception", declaredBy: exceptionPaths },
  );
}

for (const call of registered.keys()) {
  if (!writerByPublicPath.has(call)) {
    failures.push(`${call}: registered mutation is not an exported HTTP writer`);
  }
}
for (const call of exceptions) {
  if (!writerByPublicPath.has(call)) {
    failures.push(`${call}: mutation exception is not an exported HTTP writer`);
  }
  const rationale = MUTATION_CALL_EXCEPTIONS[call];
  if (typeof rationale !== "string" || rationale.trim().length < 12) {
    failures.push(`${call}: mutation exception requires a specific rationale`);
  }
}

function writerContract(expression) {
  const writer = writerByPublicPath.get(expression);
  return writer ? contractByWriter.get(writer.identity) : undefined;
}

const seenWrites = new Set();
const seenCustomSites = new Set();

/**
 * Raw fetch is reserved for protocols fetchAPI cannot represent and non-product exchanges.
 * Every entry matches one path, method, and endpoint marker; stale or duplicated entries fail.
 */
const RAW_FETCH_WRITE_ALLOWLIST = [
  {
    id: "oidc-token-exchange",
    path: "frontend/src/auth.ts",
    method: "POST",
    endpointMarker: "tokenEndpoint",
    markers: ["application/x-www-form-urlencoded", "response.ok"],
  },
  {
    id: "multipart-guest-file-push",
    path: "frontend/src/lib/file-transfer-api.ts",
    method: "POST",
    endpointMarker: "/file-transfer/push",
    markers: ["new FormData()", "getAccessToken()", "response.ok"],
  },
];
const seenRawFetchWrites = new Set();

/**
 * A few call sites authoritatively patch or invalidate a narrower cache themselves. Keeping these
 * exceptions explicit prevents a newly registered call from silently passing without any
 * reconciliation, while avoiding a forced rewrite of well-scoped optimistic/UI-local paths.
 */
const CUSTOM_RECONCILIATION_SITES = {
  "frontend/src/app/extensions/[installId]/page.tsx:extensionsAPI.updateSettings": {
    impact: "extension.write",
    markers: ["onSuccess:", "invalidate()"],
  },
  "frontend/src/app/extensions/[installId]/page.tsx:extensionsAPI.rotateCredential": {
    impact: "extension.write",
    markers: ["onSuccess:", "invalidate()"],
  },
  "frontend/src/app/extensions/[installId]/page.tsx:extensionsAPI.suspend": {
    impact: "extension.write",
    markers: ["onSuccess: invalidate"],
  },
  "frontend/src/app/extensions/[installId]/page.tsx:extensionsAPI.resume": {
    impact: "extension.write",
    markers: ["onSuccess: invalidate"],
  },
  "frontend/src/app/extensions/[installId]/page.tsx:extensionsAPI.uninstall": {
    impact: "extension.write",
    markers: ['queryKey: ["extensions", "installs"]'],
  },
  "frontend/src/app/my-keys/page.tsx:api.myKeys.create": {
    impact: "key.write",
    markers: ['queryKey: ["my-keys"]'],
  },
  "frontend/src/app/my-keys/page.tsx:api.myKeys.delete": {
    impact: "key.write",
    markers: ['queryKey: ["my-keys"]'],
  },
  "frontend/src/components/extensions/extensions-overview.tsx:extensionsAPI.install": {
    impact: "extension.write",
    markers: ['queryKey: ["extensions", "installs"]'],
  },
  "frontend/src/components/extensions/extensions-overview.tsx:extensionsAPI.create": {
    impact: "extension.write",
    markers: ['queryKey: ["extensions", "catalogue"]'],
  },
  "frontend/src/components/extensions/extensions-overview.tsx:extensionsAPI.setStatus": {
    impact: "extension.write",
    markers: ['queryKey: ["extensions", "catalogue"]'],
  },
  "frontend/src/components/extensions/extensions-overview.tsx:extensionsAPI.withdraw": {
    impact: "extension.write",
    markers: ['queryKey: ["extensions", "catalogue"]'],
  },
  "frontend/src/components/extensions/extensions-overview.tsx:extensionsAPI.suspend": {
    impact: "extension.write",
    markers: [
      'queryKey: ["extensions", "installs"]',
      'queryKey: ["extensions", "install", variables.id]',
    ],
  },
  "frontend/src/components/extensions/extensions-overview.tsx:extensionsAPI.resume": {
    impact: "extension.write",
    markers: [
      'queryKey: ["extensions", "installs"]',
      'queryKey: ["extensions", "install", variables.id]',
    ],
  },
  "frontend/src/components/extensions/extensions-overview.tsx:extensionsAPI.uninstall": {
    impact: "extension.write",
    markers: ['queryKey: ["extensions", "installs"]'],
  },
  "frontend/src/components/extensions/webhook-panel.tsx:extensionsAPI.redeliver": {
    impact: "extension.write",
    markers: ['queryKey: ["extensions", "deliveries", installId]'],
  },
  "frontend/src/components/extensions/webhook-panel.tsx:extensionsAPI.createSubscription": {
    impact: "extension.write",
    markers: ["onSuccess:", "invalidate()"],
  },
  "frontend/src/components/extensions/webhook-panel.tsx:extensionsAPI.verifySubscription": {
    impact: "extension.write",
    markers: ["onSuccess:", "invalidate()"],
  },
  "frontend/src/components/extensions/webhook-panel.tsx:extensionsAPI.deleteSubscription": {
    impact: "extension.write",
    markers: ["onSuccess: invalidate"],
  },
  "frontend/src/components/integrations-settings.tsx:api.integrations.createAPIKey": {
    impact: "integration.write",
    markers: ['queryKey: ["integrations", "api-keys"]'],
  },
  "frontend/src/components/integrations-settings.tsx:api.integrations.updateAPIKeyPermissions": {
    impact: "integration.write",
    markers: ['queryKey: ["integrations", "api-keys"]'],
  },
  "frontend/src/components/integrations-settings.tsx:api.integrations.revokeAPIKey": {
    impact: "integration.write",
    markers: ['queryKey: ["integrations", "api-keys"]'],
  },
  "frontend/src/components/integrations-settings.tsx:api.integrations.deleteAPIKey": {
    impact: "integration.write",
    markers: ['queryKey: ["integrations", "api-keys"]'],
  },
};

function scopeReconcilesImpact(scope, impactId) {
  const compact = scope.replace(/\s+/g, " ");
  return (
    compact.includes(`reconcile(queryClient, "${impactId}"`) ||
    compact.includes(`reconcileMutationImpact(queryClient, "${impactId}"`) ||
    compact.includes(`reconcileExternalMutation("${impactId}"`) ||
    (impactId === "task.cancel" && compact.includes("followTaskCancellation(")) ||
    (impactId === "instance.state" && compact.includes("refreshInstanceQueries(queryClient)")) ||
    (impactId === "instance.copy" && compact.includes("reconcileCopyState(queryClient")) ||
    (impactId === "snapshot.write" && compact.includes("reconcileSnapshotState(queryClient"))
  );
}

function isFunctionScope(node) {
  return (
    ts.isArrowFunction(node) ||
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isMethodDeclaration(node)
  );
}

function reconciliationScope(node, sourceFile) {
  let current = node.parent;
  let nearestFunction = null;
  while (current) {
    if (
      ts.isCallExpression(current) &&
      /(?:^|\.)useMutation$/.test(current.expression.getText(sourceFile))
    ) {
      return current;
    }
    if (!nearestFunction && isFunctionScope(current)) {
      const parent = current.parent;
      const isActorMutationThunk =
        ts.isCallExpression(parent) &&
        parent.expression.getText(sourceFile) === "dispatchActorMutation" &&
        parent.arguments.some((argument) => argument === current);
      // This wrapper is deliberately transparent to coherence: its only job is to prevent
      // dispatch after an actor boundary. Reconciliation lives in the enclosing mutation
      // function and must still be present there.
      if (!isActorMutationThunk) nearestFunction = current;
    }
    current = current.parent;
  }
  return nearestFunction ?? sourceFile;
}

const writerObjectPrefixes = new Set();
const writerRootNames = new Set();
for (const publicPath of writerByPublicPath.keys()) {
  const parts = publicPath.split(".");
  writerRootNames.add(parts[0]);
  for (let index = 1; index < parts.length; index += 1) {
    writerObjectPrefixes.add(parts.slice(0, index).join("."));
  }
}

function inspectIndirectWriterReferences(absolutePath) {
  const path = relative(root, absolutePath);
  if (WRITER_IMPLEMENTATION_PATHS.has(path) || path.endsWith("/lib/mutation-coherence.ts")) return;
  const source = readFileSync(absolutePath, "utf8");
  const sourceFile = ts.createSourceFile(
    path,
    source,
    ts.ScriptTarget.Latest,
    true,
    path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );

  function report(node, message) {
    const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
    failures.push(`${path}:${line}: ${message}`);
  }

  function visit(node) {
    if (ts.isImportDeclaration(node) && node.importClause) {
      const bindings = node.importClause.namedBindings;
      if (bindings && ts.isNamespaceImport(bindings)) {
        const moduleName = ts.isStringLiteral(node.moduleSpecifier)
          ? node.moduleSpecifier.text
          : "";
        if (/(?:^|\/)(?:api|[^/]+-api)$/.test(moduleName)) {
          report(node, `namespace import ${bindings.name.text} hides API writer paths`);
        }
      } else if (bindings && ts.isNamedImports(bindings)) {
        for (const element of bindings.elements) {
          const exportedName = element.propertyName?.text ?? element.name.text;
          if (writerRootNames.has(exportedName) && element.name.text !== exportedName) {
            report(
              element,
              `API writer root ${exportedName} must not be imported as ${element.name.text}`,
            );
          }
        }
      }
    } else if (ts.isPropertyAccessExpression(node)) {
      const expression = node.getText(sourceFile);
      const parent = node.parent;
      const usedByParentMember =
        ts.isPropertyAccessExpression(parent) && parent.expression === node;
      const directCall = ts.isCallExpression(parent) && parent.expression === node;
      const mutationReference =
        ts.isPropertyAssignment(parent) &&
        staticPropertyName(parent.name) === "mutationFn" &&
        parent.initializer === node;
      if (
        writerByPublicPath.has(expression) &&
        !directCall &&
        !mutationReference
      ) {
        report(
          node,
          `${expression} is an indirect writer reference; use a direct call or mutationFn member`,
        );
      } else if (
        writerObjectPrefixes.has(expression) &&
        !writerByPublicPath.has(expression) &&
        !usedByParentMember
      ) {
        report(node, `${expression} aliases an API object that contains writers`);
      }
    } else if (ts.isElementAccessExpression(node)) {
      const object = node.expression.getText(sourceFile);
      if (writerObjectPrefixes.has(object) || writerByPublicPath.has(object)) {
        report(node, `${node.getText(sourceFile)} uses computed access on an API writer surface`);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
}

function inspectDirectWriteSites(absolutePath) {
  const path = relative(root, absolutePath);
  if (
    path.endsWith("/lib/api.ts") ||
    path.endsWith("/lib/mutation-coherence.ts")
  ) {
    return;
  }
  const source = readFileSync(absolutePath, "utf8");
  const sourceFile = ts.createSourceFile(
    path,
    source,
    ts.ScriptTarget.Latest,
    true,
    path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );

  function inspectRawFetch(node) {
    const expression = node.expression.getText(sourceFile);
    if (!/^(?:fetch|window\.fetch|globalThis\.fetch)$/.test(expression)) return;
    const options = node.arguments[1];
    if (!options || !ts.isObjectLiteralExpression(options)) return;
    const methodProperty = options.properties.find((property) => {
      if (!property.name) return false;
      const name = property.name.getText(sourceFile).replace(/^["']|["']$/g, "");
      return name === "method";
    });
    if (!methodProperty) return;

    if (!ts.isPropertyAssignment(methodProperty)) {
      if (path !== "frontend/src/app/api/v1/[[...path]]/route.ts") {
        const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
        failures.push(`${path}:${line}: raw fetch uses a dynamic method that cannot be audited`);
      }
      return;
    }
    const initializer = methodProperty.initializer;
    if (!ts.isStringLiteral(initializer) && !ts.isNoSubstitutionTemplateLiteral(initializer)) {
      if (path !== "frontend/src/app/api/v1/[[...path]]/route.ts") {
        const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
        failures.push(`${path}:${line}: raw fetch uses a dynamic method that cannot be audited`);
      }
      return;
    }
    const method = initializer.text.toUpperCase();
    if (!new Set(["POST", "PUT", "PATCH", "DELETE"]).has(method)) return;

    const call = node.getText(sourceFile);
    const scope = reconciliationScope(node, sourceFile).getText(sourceFile);
    const matches = RAW_FETCH_WRITE_ALLOWLIST.filter(
      (entry) =>
        entry.path === path &&
        entry.method === method &&
        call.includes(entry.endpointMarker),
    );
    const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
    if (matches.length !== 1) {
      failures.push(`${path}:${line}: unallowlisted raw ${method} fetch`);
      return;
    }
    const [allowed] = matches;
    if (seenRawFetchWrites.has(allowed.id)) {
      failures.push(`${path}:${line}: duplicate raw fetch allowlist match ${allowed.id}`);
      return;
    }
    seenRawFetchWrites.add(allowed.id);
    for (const marker of allowed.markers) {
      if (!scope.includes(marker)) {
        failures.push(`${path}:${line}: raw fetch ${allowed.id} is missing ${marker}`);
      }
    }
  }

  function inspectWrite(node, expression) {
    const writer = writerByPublicPath.get(expression);
    if (!writer) return;

    seenWrites.add(expression);
    const contract = contractByWriter.get(writer.identity);
    if (!contract) {
      const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
      failures.push(`${path}:${line}: unregistered exported writer call ${expression}`);
      return;
    }
    if (contract.kind === "exception") return;

    const impactId = contract.impactId;
    const scope = reconciliationScope(node, sourceFile).getText(sourceFile);
    const customKey = `${path}:${expression}`;
    const custom = CUSTOM_RECONCILIATION_SITES[customKey];
    if (scopeReconcilesImpact(scope, impactId)) return;
    if (!custom) {
      const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
      failures.push(
        `${path}:${line}: ${expression} is registered as ${impactId} but has no reconciliation`,
      );
      return;
    }
    seenCustomSites.add(customKey);
    if (custom.impact !== impactId) {
      failures.push(`${customKey}: custom impact ${custom.impact} does not match ${impactId}`);
    }
    for (const marker of custom.markers) {
      if (!scope.includes(marker)) {
        failures.push(`${customKey}: missing custom reconciliation marker ${marker}`);
      }
    }
  }

  function visit(node) {
    if (ts.isCallExpression(node)) {
      inspectRawFetch(node);
      inspectWrite(node, node.expression.getText(sourceFile));
    } else if (
      ts.isPropertyAssignment(node) &&
      node.name.getText(sourceFile) === "mutationFn" &&
      ts.isPropertyAccessExpression(node.initializer)
    ) {
      inspectWrite(node, node.initializer.getText(sourceFile));
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
}

for (const absolutePath of sourceFiles(join(root, "frontend/src"))) {
  const path = relative(root, absolutePath);
  inspectIndirectWriterReferences(absolutePath);
  if (path.endsWith("/lib/api.ts") || path.endsWith("/lib/mutation-coherence.ts")) continue;
  inspectDirectWriteSites(absolutePath);
}
for (const customKey of Object.keys(CUSTOM_RECONCILIATION_SITES)) {
  if (!seenCustomSites.has(customKey)) {
    failures.push(`${customKey}: stale custom reconciliation exception`);
  }
}
for (const entry of RAW_FETCH_WRITE_ALLOWLIST) {
  if (!seenRawFetchWrites.has(entry.id)) {
    failures.push(`${entry.path}: stale raw fetch allowlist entry ${entry.id}`);
  }
}
for (const contract of [
  {
    path: "frontend/src/hooks/use-instance-power-action.ts",
    markers: [
      "api.vms.start(target.name, request)",
      "api.vms.stop(target.name, request)",
      "api.vms.restart(target.name, request)",
      'reconcileMutationImpact(queryClient, "instance.state")',
      "isAuthActorSynchronized(actorScope)",
    ],
    forbidden: ["api.vms[action]"],
  },
  {
    path: "frontend/src/components/console/iso-manager-panel.tsx",
    markers: [
      "consoleAPI.attachISO",
      "consoleAPI.ejectISO",
      'reconcileMutationImpact(queryClient, "instance.media")',
      "isAuthActorSynchronized(actorScope)",
    ],
  },
  {
    path: "frontend/src/components/console/boot-priority-panel.tsx",
    markers: [
      "consoleAPI.updateStartupOrder",
      'reconcileMutationImpact(queryClient, "instance.boot-order")',
      "isAuthActorSynchronized(actorScope)",
    ],
  },
  {
    path: "frontend/src/components/personalization/use-personalization-mutations.ts",
    markers: [
      "personalizationAPI.update",
      "personalizationAPI.delete",
      'reconcileMutationImpact(queryClient, "personalization.write")',
      "captureActorMutationFence(scopeKey)",
      "dispatchActorMutation(",
      "isActorMutationFenceCurrent(actorFence)",
      "onAuthActorChanged(() =>",
    ],
    forbidden: ['method: "PUT"', 'method: "DELETE"'],
  },
  {
    path: "frontend/src/components/terminal/file-upload-dialog.tsx",
    markers: [
      "fileTransferAPI.push",
      'reconcileMutationImpact(queryClient, "file-transfer.push")',
      "isAuthActorSynchronized(actorScope)",
    ],
    forbidden: ['/file-transfer/push?', 'method: "POST"'],
  },
  {
    path: "frontend/src/app/console/[id]/page.tsx",
    markers: [
      "tasksAPI.killConsoleSession",
      "tasksAPI.killAllConsoleSessionsOnUnload",
      'reconcileMutationImpact(queryClient, "task.session")',
    ],
    forbidden: ["navigator.sendBeacon"],
  },
]) {
  const source = readFileSync(join(root, contract.path), "utf8");
  for (const marker of contract.markers) {
    if (!source.includes(marker)) failures.push(`${contract.path} is missing ${marker}`);
  }
  for (const marker of contract.forbidden ?? []) {
    if (source.includes(marker)) failures.push(`${contract.path} must not contain ${marker}`);
  }
}

const personalizationProvider = readFileSync(
  join(root, "frontend/src/components/personalization/use-personalization-mutations.ts"),
  "utf8",
);
const personalizationSaveStart = personalizationProvider.indexOf("const saveToAPI");
const personalizationDeleteStart = personalizationProvider.indexOf("const deleteFromAPI");
const personalizationSaveScope = personalizationProvider.slice(
  personalizationSaveStart,
  personalizationDeleteStart,
);
if (personalizationSaveStart < 0 || personalizationDeleteStart <= personalizationSaveStart) {
  failures.push("personalization-provider save/delete scopes could not be inspected");
} else if (/for\s*\(/.test(personalizationSaveScope)) {
  failures.push("personalizationAPI.update must not be retried in a loop");
}

const hooksPath = join(root, "frontend/src/hooks/use-lxd.ts");
const hooks = readFileSync(hooksPath, "utf8");
const hooksSourceFile = ts.createSourceFile(
  "frontend/src/hooks/use-lxd.ts",
  hooks,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TS,
);
const observedHookImpacts = new Set();

function namedFunctionSource(name) {
  let found = null;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) found = node;
    if (!found) ts.forEachChild(node, visit);
  }
  visit(hooksSourceFile);
  return found?.getText(hooksSourceFile) ?? null;
}

function writerExpressionsInSource(source, path) {
  const sourceFile = ts.createSourceFile(
    path,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const expressions = new Set();
  function visit(node) {
    if (ts.isCallExpression(node)) {
      const expression = node.expression.getText(sourceFile);
      if (writerByPublicPath.has(expression)) expressions.add(expression);
    } else if (
      ts.isPropertyAssignment(node) &&
      staticPropertyName(node.name) === "mutationFn" &&
      ts.isPropertyAccessExpression(node.initializer)
    ) {
      const expression = node.initializer.getText(sourceFile);
      if (writerByPublicPath.has(expression)) expressions.add(expression);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return [...expressions];
}

for (const match of hooks.matchAll(
  /export function (use[A-Za-z0-9_]+)\([^]*?(?=\nexport function |\n\/\/ [A-Z][^\n]* Hooks|\n\/\/ =|$)/g,
)) {
  const [block, hookName] = match;
  if (!block.includes("useMutation({")) continue;
  const calls = writerExpressionsInSource(block, `${hookName}.ts`);
  if (calls.length === 0) {
    failures.push(`frontend/src/hooks/use-lxd.ts: ${hookName} has no inspectable API call`);
    continue;
  }
  for (const call of calls) {
    seenWrites.add(call);
    const contract = writerContract(call);
    if (!contract) {
      failures.push(`frontend/src/hooks/use-lxd.ts: ${hookName} uses unregistered writer ${call}`);
      continue;
    }
    if (contract.kind === "exception") continue;
    const impactId = contract.impactId;
    observedHookImpacts.add(`${hookName}:${impactId}`);
    const aliases = {
      "instance.state": ["refreshInstanceQueries(queryClient)"],
      "instance.copy": ["reconcileCopyState(queryClient, scopeKey)"],
      "snapshot.write": ["reconcileSnapshotState(queryClient, scopeKey)"],
    };
    if (
      !scopeReconcilesImpact(block, impactId) &&
      !(aliases[impactId] ?? []).some((marker) => block.includes(marker))
    ) {
      failures.push(
        `frontend/src/hooks/use-lxd.ts: ${hookName} registers ${call} as ${impactId} but does not reconcile it`,
      );
    }
  }
}
for (const [hookName, impactId] of Object.entries({
  useCheckClusterAuth: "cluster.auth-check",
  useInitiateCertRenewal: "cluster.certificate",
  useVerifyCertRenewal: "cluster.certificate",
  useCancelPendingCert: "cluster.certificate",
  useCreateSnapshotSchedule: "snapshot.schedule",
  useUpdateSnapshotSchedule: "snapshot.schedule",
  usePauseSnapshotSchedule: "snapshot.schedule",
  useResumeSnapshotSchedule: "snapshot.schedule",
  useRunSnapshotScheduleNow: "snapshot.schedule-run",
  useDeleteSnapshotSchedule: "snapshot.schedule",
  useCreateSnapshot: "snapshot.write",
  useDeleteSnapshot: "snapshot.write",
  useRestoreSnapshot: "snapshot.write",
  useCopyInstance: "instance.copy",
})) {
  if (!observedHookImpacts.has(`${hookName}:${impactId}`)) {
    failures.push(`frontend/src/hooks/use-lxd.ts: ${hookName} must reconcile ${impactId}`);
  }
}

for (const required of [
  "instance.create",
  "instance.specification",
  "instance.rebuild",
  "instance.copy",
  "image.upload",
  "iso.upload",
  "snapshot.write",
  "task.cancel",
]) {
  if (MUTATION_IMPACTS[required]?.completion !== "terminal-operation") {
    failures.push(`${required} must reconcile at terminal operation state`);
  }
}

for (const [path, impactId] of [
  ["frontend/src/components/image-upload-dialog.tsx", "image.upload"],
  ["frontend/src/components/iso-upload-dialog.tsx", "iso.upload"],
]) {
  const upload = readFileSync(join(root, path), "utf8");
  const terminalAt = upload.indexOf("await followUploadToTerminal({");
  const successReconcileAt = upload.indexOf(
    "if (!(await reconcileOutcome())) return;",
    terminalAt,
  );
  const reconcileAt = upload.indexOf(`reconcileMutationImpact(queryClient, "${impactId}"`);
  const catchAt = upload.indexOf("} catch (e: unknown) {", terminalAt);
  const failureReconcileAt = upload.indexOf(
    "if (!(await reconcileOutcome())) return;",
    catchAt,
  );
  if (
    terminalAt < 0 ||
    reconcileAt < 0 ||
    successReconcileAt < terminalAt ||
    catchAt < terminalAt ||
    failureReconcileAt < catchAt
  ) {
    failures.push(
      `${path}: ${impactId} must reconcile ready, failed, and unknown terminal outcomes`,
    );
  }
}

const pendingCreateJobs = readFileSync(
  join(root, "frontend/src/hooks/use-pending-create-jobs.ts"),
  "utf8",
);
const terminalCreateStart = pendingCreateJobs.indexOf("if (TERMINAL.has(job.status))");
const terminalCreateEnd = pendingCreateJobs.indexOf(
  "if (!wasNonSuccess",
  terminalCreateStart,
);
const terminalCreateScope = terminalCreateStart >= 0 && terminalCreateEnd > terminalCreateStart
  ? pendingCreateJobs.slice(terminalCreateStart, terminalCreateEnd)
  : "";
for (const marker of [
  'reconcileMutationImpact(queryClient, "instance.create")',
  "reconcileInstanceFleet(queryClient, scopeKey, { fresh: true })",
]) {
  if (!terminalCreateScope.includes(marker)) {
    failures.push(`terminal instance.create reconciliation is missing ${marker}`);
  }
}
for (const snippet of [
  "TERMINAL_INSTANCE_OPERATION.has(status)",
  "reconcileInstanceFleet(queryClient, scopeKey, { fresh: true })",
  "result.operation_id",
  "fetchAPI<CopyOperationInfo>(",
  "copyOperationVerdict(operation)",
  'verdict === "success" || verdict === "failure" || verdict === "partial"',
  "isAuthActorSynchronized(actorScope)",
  "retry: false",
]) {
  if (!hooks.includes(snippet)) failures.push(`use-lxd.ts is missing ${snippet}`);
}
if ((hooks.match(/api\.mobility\.copy\s*\(/g) ?? []).length !== 1) {
  failures.push("use-lxd.ts must submit instance.copy exactly once; polling may never replay it");
}

const copyReconcile = namedFunctionSource("reconcileCopyState") ?? "";
for (const marker of [
  'reconcile(queryClient, "instance.copy")',
  "reconcileInstanceFleet(queryClient, actorScope, { fresh: true })",
]) {
  if (!copyReconcile.includes(marker)) {
    failures.push(`reconcileCopyState is missing ${marker}`);
  }
}
for (const [name, source, impact] of [
  ["reconcileSnapshotState", namedFunctionSource("reconcileSnapshotState") ?? "", "snapshot.write"],
  ["reconcileCopyState", copyReconcile, "instance.copy"],
]) {
  const invalidateAt = source.indexOf(`reconcile(queryClient, "${impact}")`);
  const actorFenceAt = source.indexOf(
    "if (!isAuthActorSynchronized(actorScope)) return",
    invalidateAt + 1,
  );
  const fleetAt = source.indexOf(
    "reconcileInstanceFleet(queryClient, actorScope, { fresh: true })",
  );
  if (invalidateAt < 0 || actorFenceAt < invalidateAt || fleetAt < actorFenceAt) {
    failures.push(`${name} must finish invalidation, recheck the actor, then run the final fleet read`);
  }
}
const copyPoll = namedFunctionSource("reconcileCopyOperation") ?? "";
for (const marker of [
  "/operations/${encodeURIComponent(operationId)}?host=${encodeURIComponent(targetHost)}",
  'verdict === "success" || verdict === "failure" || verdict === "partial"',
  "await reconcileCopyState(queryClient, actorScope)",
  "COPY_OPERATION_MAX_POLL_MS",
]) {
  if (!copyPoll.includes(marker)) failures.push(`reconcileCopyOperation is missing ${marker}`);
}
if (copyPoll.includes("api.mobility.copy")) {
  failures.push("reconcileCopyOperation must never replay api.mobility.copy");
}

const apiSource = readFileSync(join(root, "frontend/src/lib/api.ts"), "utf8");
const runNowSource = apiSource.match(
  /runNow: async \(id: number\)[\s\S]*?\n\s*remove: async/,
)?.[0] ?? "";
for (const marker of ['method: "POST"', "retryAfterAuthRefresh: false"]) {
  if (!runNowSource.includes(marker)) {
    failures.push(`snapshot schedule run-now is missing ${marker}`);
  }
}
const runNowHook = namedFunctionSource("useRunSnapshotScheduleNow") ?? "";
for (const marker of ["retry: false", 'reconcile(queryClient, "snapshot.schedule-run")']) {
  if (!runNowHook.includes(marker)) failures.push(`useRunSnapshotScheduleNow is missing ${marker}`);
}

const copyVerdictSource = namedFunctionSource("copyOperationVerdict");
if (!copyVerdictSource) {
  failures.push("use-lxd.ts is missing copyOperationVerdict");
} else {
  const output = ts.transpileModule(
    `${copyVerdictSource}\nmodule.exports = { copyOperationVerdict };`,
    {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
      fileName: "copy-operation-verdict.ts",
    },
  ).outputText;
  const module = { exports: {} };
  new Function("module", "exports", output)(module, module.exports);
  const verdict = module.exports.copyOperationVerdict;
  const cases = [
    [{ status: "Running", status_code: 103 }, "running"],
    [{ status: "Success", status_code: 200 }, "success"],
    [{ status: "Failure", status_code: 400 }, "failure"],
    [{ status: "Cancelled", status_code: 401 }, "failure"],
    [{ status: "Partial success", status_code: 0 }, "partial"],
    [{ status: "A future daemon state", status_code: 0 }, "unknown"],
  ];
  for (const [operation, expected] of cases) {
    if (verdict(operation) !== expected) {
      failures.push(
        `copyOperationVerdict(${JSON.stringify(operation)}) must be ${expected}`,
      );
    }
  }
}

const breakGlassStore = readFileSync(join(root, "frontend/src/lib/break-glass-store.ts"), "utf8");
if (!breakGlassStore.includes('reconcileExternalMutation("break-glass.write"')) {
  failures.push("break-glass-store must reconcile break-glass.write after grant creation");
}
const breakGlassPanel = readFileSync(
  join(root, "frontend/src/components/break-glass/break-glass-panel.tsx"),
  "utf8",
);
if (!breakGlassPanel.includes('reconcileMutationImpact(queryClient, "break-glass.write"')) {
  failures.push("break-glass-panel must reconcile break-glass.write after grant revocation");
}

if (failures.length > 0) {
  console.error("Mutation coherence contract failed:");
  failures.forEach((failure) => console.error(`  ${failure}`));
  process.exit(1);
}

console.log(
  `mutation coherence: ${writerInventory.writers.length} exported HTTP writers inventoried, ` +
    `${registered.size} API paths registered, ${exceptions.size} explicit exceptions, ` +
    `${seenWrites.size} call-site paths inspected`,
);
