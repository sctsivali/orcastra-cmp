/** Guest paths as the browser handles them: always absolute, always `/`, never in a URL. */

export function joinPath(parent: string, name: string): string {
  return `${parent.replace(/\/+$/, "")}/${name}`;
}

export function parentOf(path: string): string {
  const cut = path.replace(/\/+$/, "").lastIndexOf("/");
  return cut <= 0 ? "/" : path.slice(0, cut);
}

export function baseName(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  return trimmed.slice(trimmed.lastIndexOf("/") + 1) || "/";
}

/** `/a/b` as ["/", "/a", "/a/b"], for the breadcrumb and for opening the tree down to a folder. */
export function ancestorsOf(path: string): string[] {
  const out = ["/"];
  let at = "";
  for (const part of path.split("/").filter(Boolean)) {
    at += `/${part}`;
    out.push(at);
  }
  return out;
}

export function normalizePath(value: string, from: string): string {
  const absolute = value.startsWith("/") ? value : joinPath(from, value);
  const parts: string[] = [];
  for (const part of absolute.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") parts.pop();
    else parts.push(part);
  }
  return `/${parts.join("/")}`;
}

const KERNEL_VIEWS = ["/proc", "/sys", "/dev"];

const SYSTEM_ROOTS = new Set([
  "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/libx32",
  "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var",
]);

// Paths a mistake here can stop a guest from booting. Removing one takes a typed confirmation.
const SYSTEM_TREES = ["/bin", "/boot", "/etc", "/lib", "/lib32", "/lib64", "/sbin", "/usr"];

export function isKernelView(path: string): boolean {
  return KERNEL_VIEWS.some((root) => path === root || path.startsWith(`${root}/`));
}

/** Refused outright. The backend enforces the same list: this only explains it sooner. */
export function isProtectedPath(path: string): boolean {
  return SYSTEM_ROOTS.has(path) || isKernelView(path);
}

export function isSystemPath(path: string): boolean {
  return SYSTEM_TREES.some((root) => path.startsWith(`${root}/`));
}

/** `name (1).ext`, keeping a double extension whole, for "Keep both". */
export function keepBothName(name: string, taken: ReadonlySet<string>): string {
  const match = /^(.+?)((?:\.tar)?\.[^.]+)?$/.exec(name);
  const stem = match?.[1] ?? name;
  const extension = match?.[2] ?? "";
  for (let index = 1; index < 10_000; index += 1) {
    const candidate = `${stem} (${index})${extension}`;
    if (!taken.has(candidate)) return candidate;
  }
  return `${stem} (${Date.now()})${extension}`;
}
