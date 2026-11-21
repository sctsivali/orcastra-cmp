// The edition this tree builds, read from src/lib/edition.ts, and the optional pieces an edition
// branch adds under e2e/editions/<edition>/ (they exist only on that branch; Full adds none):
//   config.cjs  { testIgnore, webServerEnv }  CMP specs the edition does not ship, server env
//   routes.cjs  { answer(request) }           the edition's own API endpoints, see fixtures.cjs
const { existsSync, readFileSync } = require("node:fs");
const path = require("node:path");

const EDITION_SOURCE = path.join(__dirname, "..", "..", "src", "lib", "edition.ts");

function readEdition() {
  const match = readFileSync(EDITION_SOURCE, "utf8").match(
    /^export const EDITION(?::\s*string)?\s*=\s*"([a-z][a-z0-9_]*)";$/m,
  );
  if (!match) throw new Error(`${EDITION_SOURCE} no longer declares EDITION as a string literal`);
  return match[1];
}

const EDITION = readEdition();

function editionModule(name) {
  if (EDITION === "full") return null;
  const file = path.join(__dirname, "..", "editions", EDITION, `${name}.cjs`);
  return existsSync(file) ? require(file) : null;
}

module.exports = { EDITION, editionModule };
