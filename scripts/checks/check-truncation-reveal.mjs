#!/usr/bin/env node
/**
 * A value the operator cannot read in full is a value the operator does not have.
 *
 * `truncateFingerprint` in lib/utils.ts returned `fingerprint.substring(0, 12) + "..."`, so 52
 * of an image fingerprint's 64 characters never reached the DOM and nothing anywhere in the app
 * could recover them. One of its four call sites fed the result into an accessible NAME, so the
 * aliases cell on /inventory/images announced "Show all 3 aliases on 1a4eed830630...".
 *
 * Measured over frontend/src before this file existed:
 *
 *   17  expressions cutting a string and gluing "..." onto it by hand, in six mutually
 *       incompatible shapes: 0,8 plus dots, 0,12 plus dots, 0,15 plus dots plus a tail of 10,
 *       0,18 plus a tail of 10, and 0,18 plus a tail of 18
 *    6  cutting an id-shaped value and appending NOTHING, so the operator could not tell the
 *       value had been cut at all
 *    4  truncateFingerprint call sites
 *    2  truncating expressions passed into an accessible name
 *    3  elements whose title= repeated their own text child, so the reveal revealed nothing
 *   74  elements carrying truncate, text-ellipsis or line-clamp-N with no title, no aria-label
 *       and no reveal control anywhere above them
 *
 * The replacement is components/ui/overflow-text.tsx for a scalar and components/ui/
 * overflow-cell.tsx for a collection. Both clip in CSS only, which leaves the whole value in
 * the DOM for selection, find-in-page and assistive tech, and both put the sighted operator's
 * reveal on a real button.
 *
 * important: regex only, no parser. The CI `guards` job runs checkout plus setup-node and no
 * `npm ci`, so importing typescript from frontend/node_modules would crash it there while
 * passing locally. check-semantic-colour.mjs and check-jsx-stray-attrs.mjs record the same
 * constraint.
 *
 * important: run this AFTER check-jsx-stray-attrs.mjs. Rules 2 and 4 read aria-label and title
 * out of opening tags, and an attribute that has fallen below its own `>` is a text child, so
 * both rules would credit an element with a name it does not carry. That leak is what
 * check-icon-button-names.mjs already had to be re-ordered around.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// The primitives themselves clip on purpose: that is the mechanism, not the defect.
const PRIMITIVES = new Set([
  'components/ui/overflow-text.tsx',
  'components/ui/overflow-cell.tsx',
  'components/ui/tooltip.tsx',
  'components/ui/table-link.tsx',
  'components/ui/table-text.tsx',
  'components/detail/copyable-text.tsx',
]);

// A reveal already owns anything inside one of these.
const OWNERS = ['OverflowText', 'CopyableText', 'OverflowCell', 'Tooltip', 'TooltipTrigger'];

// Ratchet: may fall, never rise. Recorded from this check's own output on the commit that
// introduced it, not carried over from the pre-change count, because converting a site often
// moves a tag between buckets rather than out of the population.
//
// A ratchet rather than a ban, and the reason is concrete rather than principled. Several of
// the survivors are correct: components/loading-lottie.tsx clips a transient phase message,
// which is not data and would be absurd behind a disclosure; components/sidebar/
// session-menu-items.tsx clips a nav label whose full text is one click away and which lives
// inside a Radix menu, where roving focus makes a nested button unreachable anyway;
// components/ui/select.tsx carries Radix's own trigger contract, and removing it makes every
// form row grow with its content. A hard ban here is a check that cannot pass, and a check that
// cannot pass is a check somebody deletes.
const CSS_TRUNCATION_BASELINE = 32;

// Array previews that show the first N and are not id-shaped, so rule 1e skips them. The four
// that remain are all legitimate: avatar initials from a name and from an email, the 63
// character LXD volume-name limit when deriving a suggested name, and the Used By kind
// breakdown, which NAMES its remainder rather than dropping it. Ratcheted so the next silent
// cap has to lower a number to land.
const ARRAY_PREVIEW_BASELINE = 4;

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === 'node_modules' || entry === '.next') continue;
      walk(full, out);
    } else if (entry.endsWith('.tsx') || entry.endsWith('.ts')) {
      out.push(full);
    }
  }
  return out;
}

const lineOf = (code, index) => code.slice(0, index).split('\n').length;

/**
 * Two levels of blanking, each with a different job, and keeping them separate is load-bearing.
 *
 * `blankComments` removes prose only. Every rule about code runs against this, because a
 * defect written in an example inside a doc comment is not a defect, and this file's own header
 * quotes the exact shapes it bans.
 *
 * `blankLiterals` additionally empties string and template bodies, and is used ONLY to decide
 * where a JSX tag STARTS. That is what stops a shell heredoc written inside a template literal
 * (`cat <<EOF | ...`) from being read as an opening tag whose body then swallows twelve thousand
 * characters of unrelated code, which is what two of this check's first-run findings were.
 *
 * important: do not run the string rules against the fully masked copy. `x.slice(0, 12) + "..."`
 * has its ellipsis inside a string, so masking it makes rule 1 blind to the exact shape this
 * whole check exists to ban. It was, for one commit.
 *
 * important: template `${...}` regions stay readable in both, because real JSX and real class
 * expressions live inside them.
 */
const blanker = (code, alsoLiterals) => {
  const out = code.split('');
  let i = 0;
  const blank = (from, to) => {
    for (let k = from; k < to && k < out.length; k += 1) if (out[k] !== '\n') out[k] = ' ';
  };
  while (i < code.length) {
    const c = code[i];
    if (c === '/' && code[i + 1] === '/') {
      const end = code.indexOf('\n', i);
      blank(i, end === -1 ? code.length : end);
      i = end === -1 ? code.length : end;
    } else if (c === '/' && code[i + 1] === '*') {
      const end = code.indexOf('*/', i + 2);
      blank(i, end === -1 ? code.length : end + 2);
      i = end === -1 ? code.length : end + 2;
    } else if (c === '"' || c === "'") {
      let j = i + 1;
      while (j < code.length && code[j] !== c) {
        if (code[j] === '\\') j += 1;
        if (code[j] === '\n') break;
        j += 1;
      }
      if (alsoLiterals) blank(i + 1, j);
      i = j + 1;
    } else if (c === '`') {
      let j = i + 1;
      let chunk = i + 1;
      while (j < code.length && code[j] !== '`') {
        if (code[j] === '\\') { j += 2; continue; }
        if (code[j] === '$' && code[j + 1] === '{') {
          if (alsoLiterals) blank(chunk, j);
          let depth = 1;
          j += 2;
          while (j < code.length && depth > 0) {
            if (code[j] === '{') depth += 1;
            else if (code[j] === '}') depth -= 1;
            j += 1;
          }
          chunk = j;
          continue;
        }
        j += 1;
      }
      if (alsoLiterals) blank(chunk, j);
      i = j + 1;
    } else {
      i += 1;
    }
  }
  return out.join('');
};

const blankComments = (code) => blanker(code, false);
const blankLiterals = (code) => blanker(code, true);

// A real JSX opening tag is short. A scan that runs past this is a `<` the masker did not
// account for, and letting it stand would hand rule 4 a "tag" holding hundreds of lines.
//
// Measured rather than guessed: across frontend/src the longest ACCEPTED opening tag carrying a
// truncating class is 185 characters, and the only three over-cap scans that contain one are a
// shell heredoc written as `cat <<EOF` inside a template literal, twice, and a <Controller>
// whose render prop swallowed the span three lines below it, which is counted on its own anyway.
const MAX_TAG = 800;

/**
 * A per-site waiver, on the same line or the one above. Per SITE, never per file: the file-wide
 * form is what made check-table-filters.mjs inert for eight columns at once.
 *
 * It exists so a deliberate exception is written down at the site rather than argued for by
 * weakening a regex until it stops seeing the whole shape.
 */
function waived(code, index) {
  const before = code.slice(0, index).split('\n');
  const here = code.split('\n')[before.length - 1] ?? '';
  const above = before[before.length - 2] ?? '';
  return /\/\/\s*truncation-reveal:/.test(here) || /\/\/\s*truncation-reveal:/.test(above);
}

/**
 * Opening tags with their body, brace- and quote-aware. Generalised from check-semantic-colour
 * from a fixed element name to any of them.
 *
 * `masked` is used to decide WHERE a tag starts, and `code` to read what it contains, so a
 * className inside the tag is still readable while a `<` inside a string is not a tag.
 */
function openingTags(code, masked, blind) {
  const found = [];
  const open = /<([A-Za-z][A-Za-z0-9.]*)/g;
  let m;
  while ((m = open.exec(masked)) !== null) {
    let i = m.index + m[0].length;
    let depth = 0;
    let quote = null;
    for (; i < code.length; i += 1) {
      const c = code[i];
      if (quote) {
        if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'" || c === '`') quote = c;
      else if (c === '{') depth += 1;
      else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) break;
    }
    if (i - m.index > MAX_TAG) {
      blind.overlong += 1;
      continue;
    }
    found.push({ name: m[1], tag: code.slice(m.index, i + 1), index: m.index });
  }
  return found;
}

/** Character ranges covered by any of the reveal owners, counting nesting. */
function ownerSpans(code) {
  const spans = [];
  for (const name of OWNERS) {
    const open = new RegExp(`<${name}\\b`, 'g');
    let m;
    while ((m = open.exec(code)) !== null) {
      const close = code.indexOf(`</${name}>`, m.index);
      if (close !== -1) spans.push([m.index, close]);
    }
  }
  return spans;
}

const RULE1 = [
  {
    id: 'slice plus a literal ellipsis',
    re: /\.(?:slice|substring|substr)\s*\([^()]*\)\s*\+\s*(?:"\.\.\."|'\.\.\.'|`\.\.\.`)/g,
  },
  {
    id: 'slice plus a conditional ellipsis',
    re: /\.(?:slice|substring|substr)\s*\([^()]*\)\s*\+\s*\([^()]*(?:"\.\.\."|'\.\.\.')/g,
  },
  {
    id: 'a template literal ending in three dots',
    re: /\$\{[^{}]*\.(?:slice|substring|substr)\s*\([^{}]*\}\.\.\./g,
  },
  {
    id: 'JSX where the dots are a sibling text node',
    re: /\{[^{}]*\.(?:slice|substring|substr)\s*\([^{}]*\}\s*\.\.\./g,
    jsxOnly: true,
  },
];

// A silent cut has no ellipsis to anchor on, so it needs three conditions at once or it fires on
// every parse in the codebase: a .tsx file (parsing lives in .ts under lib/, hooks/ and types/),
// a literal 0 start with a hardcoded small integer end (a parse slices at a computed offset, a
// named constant, or a negative index), and an id-shaped receiver.
const SILENT = /\b([A-Za-z_$][\w$]*(?:\s*\??\.\s*[\w$]+|\[[^\]]*\])*)\s*\??\.\s*(?:slice|substring)\s*\(\s*0\s*,\s*(\d{1,2})\s*\)/g;
const IDISH = /(fingerprint|image|^id$|Id$|_id$|uuid|hash|sha|digest|token|secret|key)$/i;

// Anything that cuts a collection to its first N. Not banned: some are correct. Counted, so a
// new silent cap has to lower a baseline to land.
const ARRAY_PREVIEW = /\.\s*slice\s*\(\s*0\s*,\s*(\d{1,2})\s*\)/g;

const RULE2 = /(?:aria-label|title|ownerLabel|itemLabel|recordIdLabel|label)=\{[^}]*(?:\.slice\(|\.substring\(|\.substr\(|truncateFingerprint)/g;

// An element whose title is the same identifier as its own text child. It adds nothing for any
// caller, truncating or not, and it is how resource-bar.tsx came to echo a string its own caller
// had already cut. Rule 1 meets this one from the opposite end without either knowing about the
// other.
const RULE3 = /title=\{([A-Za-z_$][\w$.?]*)\}[^<>]*>\s*\{\1\}/g;

const RULE5 = /^export\s+(?:async\s+)?(?:function|const)\s+((?:truncate|shorten|ellipsi|elide|abbrev|clip)[A-Za-z0-9_]*)/gm;

const TRUNCATING = /(?:^|[\s"`{('])(?:(?:sm|md|lg|xl|2xl|dark|group-hover|hover|focus):)?(truncate|text-ellipsis|line-clamp-\d+)(?=[\s"`)}',]|$)/;

const jsTruncation = [];
const silentCuts = [];
const namedByCut = [];
const echoes = [];
const helpers = [];
const bare = [];
let files = 0;
let tsxFiles = 0;
let truncatingTags = 0;
let ownedTags = 0;
let namedTags = 0;
let insideOwner = 0;
let arrayPreviews = 0;
let computedTruncate = 0;
let classesOutsideTags = 0;

const blind = { overlong: 0 };

for (const file of walk(src)) {
  const raw = readFileSync(file, 'utf8');
  const code = blankComments(raw);
  const masked = blankLiterals(code);
  const rel = relative(src, file).split('\\').join('/');
  const isTsx = file.endsWith('.tsx');
  files += 1;
  if (isTsx) tsxFiles += 1;

  // --- rule 1 -------------------------------------------------------------------------
  const seen = new Set();
  for (const { id, re, jsxOnly } of RULE1) {
    if (jsxOnly && !isTsx) continue;
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(code)) !== null) {
      if (waived(code, m.index)) continue;
      const line = lineOf(code, m.index);
      // The JSX shape's regex is a superset of the template shape's, because `${` contains `{`.
      // Reporting both would name one defect twice under two different explanations.
      if (seen.has(line)) continue;
      seen.add(line);
      jsTruncation.push({ file: rel, line, id, text: m[0].trim().slice(0, 80) });
    }
  }

  if (/truncateFingerprint\s*\(/.test(code) && !PRIMITIVES.has(rel)) {
    const i = code.indexOf('truncateFingerprint(');
    if (!waived(code, i)) {
      jsTruncation.push({
        file: rel,
        line: lineOf(code, i),
        id: 'the deleted truncateFingerprint helper',
        text: 'truncateFingerprint(...)',
      });
    }
  }

  // --- rule 1e ------------------------------------------------------------------------
  if (isTsx) {
    SILENT.lastIndex = 0;
    let m;
    while ((m = SILENT.exec(code)) !== null) {
      const segments = m[1].split(/[.?[\]]+/).filter(Boolean);
      const last = segments[segments.length - 1] ?? '';
      if (!IDISH.test(last)) continue;
      if (waived(code, m.index)) continue;
      silentCuts.push({ file: rel, line: lineOf(code, m.index), text: m[0].trim().slice(0, 80) });
    }

    if (!PRIMITIVES.has(rel)) {
      ARRAY_PREVIEW.lastIndex = 0;
      while ((m = ARRAY_PREVIEW.exec(code)) !== null) arrayPreviews += 1;
    }
  }

  // --- rule 2 -------------------------------------------------------------------------
  if (isTsx) {
    RULE2.lastIndex = 0;
    let m;
    while ((m = RULE2.exec(code)) !== null) {
      if (waived(code, m.index)) continue;
      namedByCut.push({ file: rel, line: lineOf(code, m.index), text: m[0].trim().slice(0, 80) });
    }
  }

  // --- rule 3 -------------------------------------------------------------------------
  if (isTsx) {
    RULE3.lastIndex = 0;
    let m;
    while ((m = RULE3.exec(code)) !== null) {
      if (waived(code, m.index)) continue;
      echoes.push({ file: rel, line: lineOf(code, m.index), name: m[1] });
    }
  }

  // --- rule 5 -------------------------------------------------------------------------
  if (rel.startsWith('lib/')) {
    RULE5.lastIndex = 0;
    let m;
    while ((m = RULE5.exec(code)) !== null) {
      helpers.push({ file: rel, line: lineOf(code, m.index), name: m[1] });
    }
  }

  // --- rule 4 -------------------------------------------------------------------------
  if (!isTsx) continue;
  computedTruncate += (code.match(/cva\([\s\S]*?truncate/g) ?? []).length;
  const spans = ownerSpans(masked);
  const tagList = openingTags(raw, masked, blind);
  for (const { name, tag, index } of tagList) {
    if (!TRUNCATING.test(tag)) continue;
    truncatingTags += 1;
    if (PRIMITIVES.has(rel) || OWNERS.includes(name)) {
      ownedTags += 1;
      continue;
    }
    if (/\b(?:title|aria-label|aria-labelledby)=/.test(tag)) {
      namedTags += 1;
      continue;
    }
    if (spans.some(([a, b]) => index > a && index < b)) {
      insideOwner += 1;
      continue;
    }
    if (waived(code, index)) continue;
    bare.push({ file: rel, line: lineOf(code, index) });
  }

  // A truncating class assigned to a const and spread onto an element later is invisible to the
  // tag scan above. It is where the primitives keep theirs, which is correct, but it is also
  // where a call site could hide one, so the count is printed rather than assumed to be zero.
  const tagRanges = tagList.map((t) => [t.index, t.index + t.tag.length]);
  for (const m of code.matchAll(/\b(?:truncate|text-ellipsis|line-clamp-\d+)\b/g)) {
    if (!tagRanges.some(([a, b]) => m.index >= a && m.index < b)) classesOutsideTags += 1;
  }
}

const offenders = [];

console.log(
  `truncation reveal: ${files} files (${tsxFiles} tsx), ${truncatingTags} tags carrying a truncating class`
);
console.log(
  `  JS display truncation: ${jsTruncation.length} (hard ban)` +
    `   silent id cuts: ${silentCuts.length} (hard ban)`
);
console.log(
  `  truncating expression in an accessible name: ${namedByCut.length} (hard ban)` +
    `   title echoing its own text child: ${echoes.length} (hard ban)`
);
console.log(
  `  shared truncation helper exported from lib/: ${helpers.length} (hard ban)` +
    `   CSS truncation with no reveal: ${bare.length} (baseline ${CSS_TRUNCATION_BASELINE})`
);
console.log(
  `  accounted for: ${ownedTags} inside a primitive, ${namedTags} already named, ` +
    `${insideOwner} inside a reveal`
);
console.log(
  `  blind spots this file cannot see: ${computedTruncate} truncate inside a cva variant, ` +
    `${classesOutsideTags} truncating classes held in a variable rather than on a tag, ` +
    `${blind.overlong} scans over the ${MAX_TAG} character tag cap, ` +
    `${arrayPreviews} array previews (baseline ${ARRAY_PREVIEW_BASELINE})`
);

const show = (rows, render) => {
  for (const r of rows.slice(0, 20)) console.log(`    ${render(r)}`);
  if (rows.length > 20) console.log(`    ... and ${rows.length - 20} more`);
};

if (jsTruncation.length) {
  console.log('\nA value cut in JavaScript never reaches the DOM, so no title, no aria-label');
  console.log('and no reveal can recover it. Clip in CSS and let OverflowText reveal it:');
  show(jsTruncation, (r) => `${r.file}:${r.line}  ${r.id}  ${r.text}`);
  offenders.push('JS display truncation');
}

if (silentCuts.length) {
  console.log('\nAn id cut to a fixed prefix with nothing appended does not look shortened, it');
  console.log('looks complete. Print the whole value and let CSS do the clipping:');
  show(silentCuts, (r) => `${r.file}:${r.line}  ${r.text}`);
  offenders.push('silent id cuts');
}

if (namedByCut.length) {
  console.log('\nAn accessible name built from a cut value names nothing. This is what made the');
  console.log('images aliases control announce "Show all 3 aliases on 1a4eed830630...":');
  show(namedByCut, (r) => `${r.file}:${r.line}  ${r.text}`);
  offenders.push('a cut value inside an accessible name');
}

if (echoes.length) {
  console.log('\nA title that repeats its own text child reveals nothing, and it hides the fact');
  console.log('that the element has no reveal at all:');
  show(echoes, (r) => `${r.file}:${r.line}  title={${r.name}}> {${r.name}}`);
  offenders.push('a title echoing its own text child');
}

if (helpers.length) {
  console.log('\nA shared display-truncation helper is how one call site became four. Deleted');
  console.log('rather than deprecated, because a helper that still exists is one somebody calls:');
  show(helpers, (r) => `${r.file}:${r.line}  ${r.name}`);
  offenders.push('a truncation helper exported from lib/');
}

if (bare.length > CSS_TRUNCATION_BASELINE) {
  console.log(
    `\nCSS truncation with no reveal rose from ${CSS_TRUNCATION_BASELINE} to ${bare.length}.`
  );
  console.log('Each of these clips a value with no title, no aria-label and no control:');
  show(bare, (r) => `${r.file}:${r.line}`);
  offenders.push(`CSS truncation with no reveal (${bare.length} > ${CSS_TRUNCATION_BASELINE})`);
}

if (arrayPreviews > ARRAY_PREVIEW_BASELINE) {
  console.log(
    `\nArray previews rose from ${ARRAY_PREVIEW_BASELINE} to ${arrayPreviews}. A collection cut`
  );
  console.log('to its first N belongs in OverflowCell, which names the remainder and opens it.');
  offenders.push('array previews');
}

// A scan over nothing passes for the wrong reason, and a Tailwind upgrade is exactly what
// silently breaks a class-name regex.
if (tsxFiles < 150) offenders.push(`only ${tsxFiles} .tsx files scanned, expected at least 150`);
if (truncatingTags < 30) {
  offenders.push(`only ${truncatingTags} truncating tags found, expected at least 30`);
}

if (offenders.length) {
  console.log(`\nFailing: ${offenders.join('; ')}`);
  console.log('\nA value the operator cannot read in full is a value the operator does not have.');
  console.log('  components/ui/overflow-text.tsx        one value, shown short, revealed in full');
  console.log('  components/ui/overflow-cell.tsx        a collection that does not fit a cell');
  console.log('  components/detail/copyable-text.tsx    a value they need to take somewhere else');
  console.log('See documentation/ui/value-reveal.md.');
  process.exit(1);
}

console.log('truncation reveal: nothing is cut where it cannot be read');
