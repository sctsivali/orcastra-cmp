// Guest password rules the browser can check, and the generator.
//
// The policy is the backend's (services/guest_credentials/passwords.py, after NIST SP 800-63B
// revision 4): at least 15 characters because a guest console login is a single factor, at most
// 128, printable ASCII only because the password has to be typed at a console whose keymap nobody
// here controls. No composition rules. The server also refuses common, repetitive and sequential
// passwords and ones built from the account or instance name; those are its decision alone.

export const PASSWORD_MIN = 15;
export const PASSWORD_MAX = 128;
/** bcrypt stops reading here, so a longer password would set less than the person typed. */
export const BCRYPT_MAX_BYTES = 72;

// important: 17 letters that sit on the same keys on US, QWERTZ and AZERTY layouts, so a generated
// password typed through the Graphics console (which sends US key codes) lands as written. Lower
// case only, so no letter needs Shift.
export const GENERATED_ALPHABET = "bcdefghjknprstuvx";
export const GENERATED_LENGTH = 22;
// The largest multiple of 17 a byte can hold. Bytes at or above it are dropped, so every letter is
// equally likely; taking `byte % 17` over the whole byte range would favour the first letter.
const ACCEPT_BELOW = GENERATED_ALPHABET.length * 15;

type RandomFill = (bytes: Uint8Array) => Uint8Array;

const cryptoFill: RandomFill = (bytes) => crypto.getRandomValues(bytes);

/** 22 letters from a 17-letter alphabet: about 90 bits, drawn by rejection sampling. */
export function generatePassword(fill: RandomFill = cryptoFill): string {
  let out = "";
  while (out.length < GENERATED_LENGTH) {
    for (const byte of fill(new Uint8Array(32))) {
      if (byte >= ACCEPT_BELOW) continue;
      out += GENERATED_ALPHABET[byte % GENERATED_ALPHABET.length];
      if (out.length === GENERATED_LENGTH) break;
    }
  }
  return out;
}

/** Groups of four for reading aloud or typing; the value itself is never stored grouped. */
export function passwordGroups(value: string): string[] {
  const groups: string[] = [];
  for (let index = 0; index < value.length; index += 4) groups.push(value.slice(index, index + 4));
  return groups;
}

export function isPrintableAscii(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code < 0x20 || code > 0x7e) return false;
  }
  return true;
}

export interface PasswordPolicy {
  min: number;
  max: number;
  /** The guest's default hash scheme, when the overview read it. */
  scheme?: string | null;
}

/** Why the browser already knows this password will be refused, or null when only the server can tell. */
export function passwordProblem(value: string, policy: PasswordPolicy): string | null {
  if (value.length < policy.min) return `Use at least ${policy.min} characters.`;
  if (value.length > policy.max) return `Use at most ${policy.max} characters.`;
  if (!isPrintableAscii(value)) return "Use letters, digits, spaces and symbols found on a US keyboard.";
  if (policy.scheme === "bcrypt" && value.length > BCRYPT_MAX_BYTES) {
    return `This guest's password scheme reads only ${BCRYPT_MAX_BYTES} characters.`;
  }
  return null;
}
