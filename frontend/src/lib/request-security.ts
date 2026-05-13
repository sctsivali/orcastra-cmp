type RequestSecurityDecision = {
  statusCode: number;
  reason: string;
};

const TRAVERSAL_TARGETS = [
  "/etc/passwd",
  "/etc/shadow",
  "/proc/self/environ",
  "/proc/self/cmdline",
  "c:/windows/win.ini",
  "windows/win.ini",
];

const SENSITIVE_SEGMENTS = new Set([
  ".aws",
  ".azure",
  ".bzr",
  ".config",
  ".docker",
  ".dockerenv",
  ".env",
  ".git",
  ".hg",
  ".netrc",
  ".npmrc",
  ".runtimeconfig.json",
  ".ssh",
  ".svn",
  "adminer.php",
  "composer.json",
  "composer.lock",
  "docker-compose.override.yml",
  "docker-compose.prod.yml",
  "docker-compose.yml",
  "dockerfile",
  "id_dsa",
  "id_ecdsa",
  "id_ed25519",
  "id_rsa",
  "package-lock.json",
  "phpinfo.php",
  "server-status",
  "web.config",
]);

const SENSITIVE_PREFIXES = [
  "/adminer",
  "/actuator",
  "/cgi-bin",
  "/debug",
  "/horizon",
  "/laravel",
  "/phpmyadmin",
  "/telescope",
  "/vendor/phpunit",
  "/wp-admin",
  "/wp-content",
  "/wp-includes",
  "/wp-login",
];

const SENSITIVE_SUFFIXES = [
  ".bak",
  ".backup",
  ".conf",
  ".config",
  ".dump",
  ".ini",
  ".old",
  ".orig",
  ".sql",
  ".sqlite",
  ".swp",
  ".tar",
  ".tgz",
];

const RISKY_QUERY_KEYS = new Set([
  "dir",
  "directory",
  "download",
  "file",
  "file_path",
  "filepath",
  "filename",
  "include",
  "page",
  "path",
  "template",
]);

function decodeRepeatedly(value: string): string {
  let decoded = value;
  for (let index = 0; index < 3; index += 1) {
    try {
      const nextValue = decodeURIComponent(decoded);
      if (nextValue === decoded) break;
      decoded = nextValue;
    } catch {
      break;
    }
  }
  return decoded;
}

function normalizeRequestPart(value: string): string {
  return decodeRepeatedly(value).replace(/\\/g, "/").toLowerCase();
}

function hasPathTraversal(value: string): boolean {
  const normalized = normalizeRequestPart(value);
  if (normalized.includes("\0") || value.toLowerCase().includes("%00")) {
    return true;
  }

  const segments = normalized.split("/").filter(Boolean);
  if (segments.includes("..")) {
    return true;
  }

  const compact = normalized.replace(/\/\/+/, "/");
  return TRAVERSAL_TARGETS.some((target) => compact.includes(target));
}

function isSensitiveProbe(value: string): boolean {
  let normalized = normalizeRequestPart(value).split("?", 1)[0];
  if (!normalized.startsWith("/")) normalized = `/${normalized}`;

  const segments = normalized.split("/").filter(Boolean);
  if (segments.some((segment) => SENSITIVE_SEGMENTS.has(segment))) {
    return true;
  }

  if (SENSITIVE_PREFIXES.some((prefix) => normalized === prefix || normalized.startsWith(`${prefix}/`))) {
    return true;
  }

  return SENSITIVE_SUFFIXES.some((suffix) => normalized.endsWith(suffix));
}

function hasRiskyQueryProbe(search: string): boolean {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  for (const [key, value] of params.entries()) {
    if (!RISKY_QUERY_KEYS.has(key.toLowerCase())) continue;
    if (hasPathTraversal(value) || isSensitiveProbe(value)) return true;
  }
  return false;
}

export function inspectRequestTarget(
  method: string,
  pathname: string,
  search = ""
): RequestSecurityDecision | null {
  const normalizedPath = normalizeRequestPart(pathname);

  if (!["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase()) && ["", "/"].includes(normalizedPath)) {
    return { statusCode: 405, reason: "root_method_probe" };
  }

  if (hasPathTraversal(pathname) || hasRiskyQueryProbe(search)) {
    return { statusCode: 404, reason: "path_traversal" };
  }

  if (isSensitiveProbe(pathname) || hasRiskyQueryProbe(search)) {
    return { statusCode: 404, reason: "sensitive_path_probe" };
  }

  return null;
}
