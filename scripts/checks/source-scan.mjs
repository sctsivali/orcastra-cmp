// Shared by the guards that read JSX and call arguments. Parser-free and dependency-free, for the
// reason check-jsx-stray-attrs.mjs gives: the CI guards job runs checkout and setup-node, no npm ci.

const PAIRS = { '(': ')', '[': ']', '{': '}' };

function endOfQuote(code, start) {
  for (let i = start + 1; i < code.length; i += 1) {
    if (code[i] === '\\') i += 1;
    else if (code[i] === code[start]) return i;
  }
  return -1;
}

/** Index of the backtick closing the template opened at `start`, -1 when it never closes. */
export function endOfTemplate(code, start) {
  for (let i = start + 1; i < code.length; i += 1) {
    if (code[i] === '\\') i += 1;
    else if (code[i] === '`') return i;
    else if (code[i] === '$' && code[i + 1] === '{') {
      i = closing(code, i + 1);
      if (i === -1) return -1;
    }
  }
  return -1;
}

/** Index of the bracket closing the one at `open`, past strings and templates. -1 when unbalanced. */
export function closing(code, open) {
  const stack = [PAIRS[code[open]]];
  for (let i = open + 1; i < code.length; i += 1) {
    const c = code[i];
    if (c === '"' || c === "'") i = endOfQuote(code, i);
    else if (c === '`') i = endOfTemplate(code, i);
    else if (PAIRS[c]) stack.push(PAIRS[c]);
    else if (c === stack[stack.length - 1]) {
      stack.pop();
      if (stack.length === 0) return i;
    }
    if (i === -1) return -1;
  }
  return -1;
}

/** The expressions interpolated into a template literal, in order. */
export function templateHoles(template) {
  const holes = [];
  for (let i = 1; i < template.length - 1; i += 1) {
    if (template[i] === '\\') i += 1;
    else if (template[i] === '$' && template[i + 1] === '{') {
      const end = closing(template, i + 1);
      holes.push(template.slice(i + 2, end).trim());
      i = end;
    }
  }
  return holes;
}

/** The text of the first argument in a call's argument list. */
export function firstArgument(args) {
  for (let i = 0; i < args.length; i += 1) {
    const c = args[i];
    if (c === ',') return args.slice(0, i).trim();
    if (c === '"' || c === "'") i = endOfQuote(args, i);
    else if (c === '`') i = endOfTemplate(args, i);
    else if (PAIRS[c]) i = closing(args, i);
    if (i === -1) break;
  }
  return args.trim();
}

/** True when `expression` is one call of `name` and nothing else. */
export function isWholeCall(expression, name) {
  return (
    expression.startsWith(`${name}(`) && closing(expression, name.length) === expression.length - 1
  );
}

/** Opening tags for `name` with their body, brace- and quote-aware. */
export function openingTags(source, name) {
  const found = [];
  const open = new RegExp(`<${name}\\b`, 'g');
  let m;
  while ((m = open.exec(source)) !== null) {
    let i = m.index + m[0].length;
    let depth = 0;
    let quote = null;
    for (; i < source.length; i += 1) {
      const c = source[i];
      if (quote) { if (c === quote) quote = null; continue; }
      if (c === '"' || c === "'" || c === '`') quote = c;
      else if (c === '{') depth += 1;
      else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) break;
    }
    found.push({ text: source.slice(m.index, i + 1), index: m.index });
  }
  return found;
}
