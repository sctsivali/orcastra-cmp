// What the credentials panel says about the values and refusals the API returns.
//
// note: the server writes the refusal messages (services/guest_credentials/errors.py and flows.py)
// and they are shown as written. This file adds words only where a code carries more than its
// message: the key parser's reason, a missing tool's name, a wait, and an unconfirmed change.

import { APIError } from "@/types/api";
import {
  credentialErrorCode,
  isOutcomeUnknown,
  keyRejectionReason,
  missingTool,
  retryAfterSeconds,
  type AccountKind,
  type AddressLabel,
  type PasswordState,
  type SshServerFacts,
  type SudoVerdict,
} from "./api";

export const OUTCOME_UNKNOWN =
  "The change was sent, but the guest did not confirm it. It may or may not have happened. Reload the list and check the account before relying on it.";

const KEY_REASONS: Record<string, string> = {
  empty: "Paste a public key first.",
  too_long: "This is far longer than any public key. Paste the one line from the .pub file.",
  multiple_lines: "This is more than one line. Paste one key, on one line.",
  control_characters: "This holds invisible control characters. Copy the key again from the .pub file.",
  options_not_supported: "Options such as from= or command= in front of the key are not supported here. Paste the key alone.",
  certificate_refused: "This is an SSH certificate. Paste the plain public key instead.",
  unsupported_type: "This key type is not accepted. Use an Ed25519, ECDSA or RSA key.",
  not_a_public_key: "This does not look like a public key. A public key starts with its type, such as ssh-ed25519.",
  malformed_key: "The key is damaged or incomplete. Copy the whole line again.",
  type_mismatch: "The type written in front of the key does not match the key itself. Copy the whole line again.",
  dsa_refused: "DSA keys are refused because they are no longer safe. Create an Ed25519 key instead.",
  rsa_too_short: "This RSA key is shorter than 2048 bits, which is no longer safe. Create an Ed25519 key instead.",
  rsa_too_long: "This RSA key is longer than 16384 bits, which this panel does not accept.",
  comment_too_long: "The comment after the key is longer than 255 characters. Shorten it.",
  comment_invalid: "The comment after the key holds characters this panel does not accept. Use plain text.",
};

const WARNINGS: Record<string, string> = {
  rsa_below_3072: "This RSA key is shorter than 3072 bits. It works, but an Ed25519 key is stronger.",
  home_writable_by_others: "Other accounts can write to this home folder, so the SSH server may refuse key logins until that is fixed.",
  key_file_writable_by_others: "Other accounts can write to authorized_keys, so the SSH server may refuse key logins until that is fixed.",
  // After a lock, read from the guest's sshd: with PAM a key still lets the person in, without it
  // (OpenSSH built without PAM, as on Alpine) a locked password blocks every SSH login.
  key_logins_still_work: "SSH key logins for this account still work. Remove its keys as well to shut it out completely.",
  key_logins_blocked_too: "This guest's SSH server does not use PAM, so the lock also blocks every SSH login for this account, keys included.",
  key_logins_may_still_work: "The SSH server's settings could not be read, so SSH key logins for this account may still work. Remove its keys as well to be sure.",
};

/** What a lock did to SSH, from its warnings; an empty list means the guest has no SSH server. */
export function lockOutcomeText(warnings: string[]): string[] {
  return warnings.length ? warnings.map(warningText) : ["No SSH server is installed in this guest, so there are no SSH logins to shut off."];
}

export function warningText(code: string): string {
  return WARNINGS[code] ?? "The guest reported a condition this panel does not describe yet.";
}

function serverMessage(error: unknown): string | undefined {
  if (!(error instanceof APIError)) return error instanceof Error && error.message ? error.message : undefined;
  const detail = error.detail as { message?: unknown } | null | undefined;
  if (detail && typeof detail === "object" && typeof detail.message === "string") return detail.message;
  return error.message || undefined;
}

/** The refusal in words a person can act on. */
export function credentialErrorMessage(error: unknown, fallback: string): string {
  if (isOutcomeUnknown(error)) return OUTCOME_UNKNOWN;
  const code = credentialErrorCode(error);
  const message = serverMessage(error) ?? fallback;
  if (code === "invalid_public_key") return KEY_REASONS[keyRejectionReason(error) ?? ""] ?? message;
  if (code === "tool_missing") {
    const tool = missingTool(error);
    return tool ? `${message} It needs ${tool}.` : message;
  }
  if (code === "busy" || code === "rate_limited") {
    const wait = retryAfterSeconds(error);
    return wait ? `${message} Try again in ${wait}s.` : message;
  }
  return message;
}

export const KIND_TEXT: Record<AccountKind, string> = {
  root: "Root",
  root_equivalent: "Root equivalent (UID 0)",
  human: "User",
};

export const PASSWORD_TEXT: Record<PasswordState, string> = {
  set: "Set",
  none: "No password",
  locked: "Locked",
  empty: "Empty",
  unknown: "Unknown",
};

export const PASSWORD_DETAIL: Record<PasswordState, string | null> = {
  set: null,
  none: "Password login is off",
  locked: "Password login is off",
  empty: "Logs in on the console with no password",
  unknown: "The guest did not say",
};

export function sudoText(verdict: SudoVerdict | null, kind: AccountKind): string | null {
  if (kind !== "human") return null;
  switch (verdict) {
    case "yes": return "Yes";
    case "no": return "No";
    case "rootpw": return "Asks for root's password";
    case "absent": return "Not installed";
    default: return null;
  }
}

export const ADDRESS_TEXT: Record<AddressLabel, { name: string; reach: string }> = {
  public: { name: "Public", reach: "Reachable from the internet, if no firewall in between blocks port 22." },
  private: { name: "Private", reach: "Reachable only from inside that network, or over a VPN into it." },
  shared: { name: "Shared range", reach: "A carrier-grade NAT or Tailscale-range address. Reachable only through that network." },
  link_local: { name: "Link-local", reach: "Reachable only from the same network segment." },
  unknown: { name: "Unknown", reach: "The kind of address could not be told." },
};

export const NOTE_TEXT: Record<SshServerFacts["notes"][number], string> = {
  authorized_keys_not_read:
    "This SSH server does not read ~/.ssh/authorized_keys, so keys added here may not let anyone log in.",
  authorized_keys_command:
    "This SSH server also asks a program for keys (AuthorizedKeysCommand), so logins may accept keys that are not in this list.",
  pubkey_disabled:
    "Key logins are turned off on this SSH server (PubkeyAuthentication no). Keys added here do not work until they are turned on.",
};

/** The global settings shown, in the order a person checks them. */
export const EFFECTIVE_SETTINGS: { key: string; name: string; directive: string }[] = [
  { key: "port", name: "Port", directive: "Port" },
  { key: "pubkeyauthentication", name: "Key Logins", directive: "PubkeyAuthentication" },
  { key: "passwordauthentication", name: "Password Logins", directive: "PasswordAuthentication" },
  { key: "kbdinteractiveauthentication", name: "Keyboard-Interactive Logins", directive: "KbdInteractiveAuthentication" },
  { key: "permitrootlogin", name: "Root Login", directive: "PermitRootLogin" },
  { key: "authorizedkeysfile", name: "Key Files", directive: "AuthorizedKeysFile" },
  { key: "authorizedkeyscommand", name: "Key Command", directive: "AuthorizedKeysCommand" },
  { key: "usepam", name: "PAM", directive: "UsePAM" },
];

const ROOT_LOGIN_TEXT: Record<string, string> = {
  yes: "allowed with a password or a key",
  no: "not allowed",
  "prohibit-password": "keys only",
  "without-password": "keys only",
  "forced-commands-only": "keys with a forced command only",
};

/** A plain gloss beside the raw value, where the raw value is not obvious. */
export function effectiveGloss(key: string, value: string): string | null {
  if (key === "permitrootlogin") return ROOT_LOGIN_TEXT[value.toLowerCase()] ?? null;
  if (key === "authorizedkeyscommand" && value.toLowerCase() === "none") return "not used";
  return null;
}
