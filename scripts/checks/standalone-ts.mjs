// Compile one frontend/src/lib file on its own and import the result, for guards that exercise
// pure logic static analysis cannot see. Not a guard itself: check-guard-parity only counts
// files run as `node scripts/checks/<name>.mjs`.
//
// The file must stay alias-free with no runtime imports. Copying it out before compiling makes
// "standalone" literal: an alias or an app import fails here rather than silently resolving
// through the app's config.

import { execFileSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { basename, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

/**
 * Returns `{ module, cleanup }`, or exits the process with the compiler output when the file
 * does not compile on its own.
 */
export async function compileStandalone(root, libFile, label) {
  const frontend = join(root, 'frontend');
  const out = mkdtempSync(join(tmpdir(), `${label}-`));
  const cleanup = () => rmSync(out, { recursive: true, force: true });
  // note: tsc emits ESM, and node treats a bare .js in a directory with no package.json as
  // CommonJS. Without this the import fails with "Unexpected token 'export'".
  writeFileSync(join(out, 'package.json'), '{"type":"module"}');

  // important: `npx tsc` is not the TypeScript compiler. `tsc` is a separate, unrelated
  // package on npm, and npx fetches it whenever the workspace has no local binary. That is
  // exactly the Structural guards job, which is checkout plus setup-node with no install by
  // design, so the filter-url guard failed with "This is not the tsc command you are looking
  // for" on every run from ef847d1 onward. It passed locally the whole time because
  // frontend/node_modules/.bin/tsc exists there.
  //
  // The fallback is pinned to the major the app declares (package.json asks for ^5.3.3). An
  // unpinned `npx -p typescript` fetches whatever is newest, and the first attempt at this fix
  // failed in CI on error TS5112 while passing locally on the repo's 5.9. Compile the guard
  // with the same major the product compiles with, or the guard is testing a different
  // compiler than the one that ships the code.
  const localTsc = resolve(frontend, 'node_modules', '.bin', 'tsc');
  const localTypeScript = resolve(frontend, 'node_modules', 'typescript', 'lib', 'typescript.js');
  const [cmd, leadingArgs] = existsSync(localTsc)
    ? [localTsc, []]
    : ['npx', ['-p', 'typescript@5', 'tsc']];

  // important: compile from the temp directory, not from frontend/. TS5112 rejects naming a
  // file on the command line while a tsconfig.json sits in the working directory, and
  // frontend/ has one. `--ignoreConfig`, which the error text suggests, is not an option on
  // 5.x (TS5023), so moving the file is the fix that works on the pinned major.
  const name = basename(libFile);
  copyFileSync(resolve(frontend, 'src', 'lib', libFile), join(out, name));

  try {
    if (existsSync(localTypeScript)) {
      // Some hardened runners allow the current Node process but deny child-process creation.
      // Use the same installed compiler in-process so the guard still performs a real strict
      // typecheck and emit. CI checkouts without dependencies retain the pinned npx fallback.
      const require = createRequire(import.meta.url);
      const ts = require(localTypeScript);
      const options = {
        module: ts.ModuleKind.ESNext,
        target: ts.ScriptTarget.ES2020,
        moduleResolution: ts.ModuleResolutionKind.Bundler,
        strict: true,
        outDir: out,
      };
      const program = ts.createProgram([join(out, name)], options);
      const emit = program.emit();
      const diagnostics = [...ts.getPreEmitDiagnostics(program), ...emit.diagnostics];
      if (diagnostics.length > 0) {
        throw new Error(ts.formatDiagnosticsWithColorAndContext(diagnostics, {
          getCanonicalFileName: (file) => file,
          getCurrentDirectory: () => out,
          getNewLine: () => '\n',
        }));
      }
    } else {
      execFileSync(
        cmd,
        [...leadingArgs, name, '--outDir', '.', '--module', 'esnext', '--target', 'es2020',
         '--moduleResolution', 'bundler', '--strict'],
        { cwd: out, stdio: 'pipe' },
      );
    }
  } catch (error) {
    console.error(`${libFile} failed to compile standalone:`);
    console.error(error.stdout?.toString() || error.message);
    console.error(`\n${libFile} must stay alias-free with no runtime imports so this guard can run it.`);
    cleanup();
    process.exit(1);
  }

  const module = await import(pathToFileURL(join(out, name.replace(/\.ts$/, '.js'))).href);
  return { module, cleanup };
}
