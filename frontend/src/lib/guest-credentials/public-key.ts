// Checks on a pasted SSH key that run in the browser, before anything is sent.
//
// important: a private key pasted here must never leave the page. It is recognised on the raw text
// before any request, the field is cleared and the submit is blocked. The markers mirror the
// backend's (services/guest_credentials/ssh_keys.py _PRIVATE_MARKERS); keep the two in step.
//
// The other checks are hints shown while typing. The server parses the key strictly and stays the
// authority.

const PRIVATE_MARKERS: readonly RegExp[] = [
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/,
  /PuTTY-User-Key-File-\d/,
  /---- BEGIN SSH2 ENCRYPTED PRIVATE KEY ----/,
  // "openssh-key-v1\0" in base64: the body of an OpenSSH private key pasted without its armour.
  /b3BlbnNzaC1rZXktdjEA/,
];

export const ACCEPTED_KEY_TYPES: readonly string[] = [
  "ssh-ed25519",
  "ssh-rsa",
  "ecdsa-sha2-nistp256",
  "ecdsa-sha2-nistp384",
  "ecdsa-sha2-nistp521",
  "sk-ssh-ed25519@openssh.com",
  "sk-ecdsa-sha2-nistp256@openssh.com",
];

const RFC4716_BEGIN = "---- BEGIN SSH2 PUBLIC KEY ----";
const MAX_INPUT_BYTES = 16 * 1024;

export function looksLikePrivateKey(text: string): boolean {
  return PRIVATE_MARKERS.some((marker) => marker.test(text));
}

export type KeyPrecheck =
  | { state: "empty" }
  | { state: "private" }
  | { state: "hint"; hint: string }
  | { state: "plausible"; keyType: string };

/** What can be said about a paste without parsing it. Only `private` blocks a submit. */
export function precheckPublicKey(text: string): KeyPrecheck {
  const trimmed = text.trim();
  if (!trimmed) return { state: "empty" };
  if (looksLikePrivateKey(text)) return { state: "private" };
  if (new TextEncoder().encode(text).length > MAX_INPUT_BYTES) {
    return { state: "hint", hint: "This is far longer than any public key. Paste the one line from the .pub file." };
  }
  // PuTTYgen's "Save public key" writes this multi-line form, and the server reads it.
  if (trimmed.startsWith(RFC4716_BEGIN)) return { state: "plausible", keyType: "ssh2" };
  if (/[\r\n]/.test(trimmed)) {
    return { state: "hint", hint: "This is more than one line. Paste one key, on one line." };
  }
  const first = trimmed.split(/\s+/)[0] ?? "";
  if (ACCEPTED_KEY_TYPES.includes(first)) {
    return trimmed.split(/\s+/).length < 2
      ? { state: "hint", hint: "The key itself is missing after its type. Copy the whole line." }
      : { state: "plausible", keyType: first };
  }
  if (first === "ssh-dss") return { state: "hint", hint: "DSA keys are refused because they are no longer safe. Create an Ed25519 key instead." };
  if (first.endsWith("-cert-v01@openssh.com")) return { state: "hint", hint: "This is a certificate. Paste the plain public key instead." };
  if (trimmed.split(/\s+/).some((token) => ACCEPTED_KEY_TYPES.includes(token))) {
    return { state: "hint", hint: "Something comes before the key type. Options such as from= or command= are not supported here." };
  }
  return { state: "hint", hint: "A public key starts with its type, such as ssh-ed25519." };
}
